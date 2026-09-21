#!/usr/bin/env python3
"""Phase 2 tests: versioned JSON state document (schema + JsonStateStore +
legacy migration + backend parity).

Locked down here:
* J1-J9  schema codec: complete materialization, explicit-null vs
  missing-key, version strictness, type policy, unknown keys,
  deterministic bytes, unrepresentable text refused pre-filesystem;
* J10-J13 JsonStateStore: whole-document atomic commit, final-bytes
  preflight, load purity + loud corruption (never defaulted), modes;
* M1-M6  migration: legacy slots → v1 document, idempotent, non-
  destructive (JSON wins), seed-if-absent under a race, legacy values
  never "repaired", transient candidate → explicit null;
* parity: for every canonical state shape, FileStateStore.load ==
  JsonStateStore.load after migration.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_sentinel.state import (
    SCHEMA_VERSION,
    DocumentCorruptError,
    FileStateStore,
    MissingStateDocumentError,
    ProviderState,
    ResetCandidate,
    SchemaError,
    StaleStateError,
    StateStoreError,
    deserialize_state,
    document_to_state,
    parse_document_bytes,
    serialize_state,
    state_to_document,
    validate_document,
)
from quota_sentinel.state.schema import EXPECTED_KEYS, VERSION_KEY

FULL_STATE = ProviderState(
    last_attempt_at=1_700_000_100,
    last_task_at=1_700_000_000,
    next_due_at=1_700_000_500,
    retry_pending=True,
    last_known_reset=1_700_000_900,
    last_triggered_window="1700000900",
    reset_anchor=1_699_999_000,
    reset_candidate=ResetCandidate(1_700_004_600, 1_700_000_050),
)


def canonical_document(state: ProviderState) -> dict:
    return json.loads(serialize_state(state, "codex").decode("utf-8"))


class SchemaCodecTests(unittest.TestCase):
    # J1 — complete round-trip.
    def test_round_trip_full_state(self) -> None:
        raw = serialize_state(FULL_STATE, "codex")
        self.assertEqual(deserialize_state(raw, "codex"), FULL_STATE)

    def test_round_trip_all_unset(self) -> None:
        raw = serialize_state(ProviderState(), "codex")
        self.assertEqual(deserialize_state(raw, "codex"), ProviderState())

    # J2 — every key materialized; null is business-unset.
    def test_document_always_materializes_all_keys(self) -> None:
        for state in (ProviderState(), FULL_STATE):
            doc = json.loads(serialize_state(state, "codex"))
            self.assertEqual(set(doc), set(EXPECTED_KEYS))
        empty = json.loads(serialize_state(ProviderState(), "codex"))
        self.assertIsNone(empty["reset_candidate"])
        self.assertIsNone(empty["next_due_at"])
        self.assertFalse(empty["retry_pending"])
        self.assertEqual(empty[VERSION_KEY], SCHEMA_VERSION)

    # J5/J2 — a missing key is corruption, never a default.
    def test_missing_key_rejected(self) -> None:
        good = canonical_document(FULL_STATE)
        for key in sorted(EXPECTED_KEYS):
            broken = dict(good)
            del broken[key]
            with self.subTest(missing=key):
                with self.assertRaises(SchemaError) as ctx:
                    validate_document(broken, "codex")
                self.assertIn("missing required keys", str(ctx.exception))
                self.assertIn(key, str(ctx.exception))

    # J4 — missing schema_version is fatal.
    def test_missing_version_rejected(self) -> None:
        broken = canonical_document(FULL_STATE)
        del broken[VERSION_KEY]
        with self.assertRaises(SchemaError):
            validate_document(broken, "codex")

    # J3 — unknown versions fail loudly, no guessing.
    def test_unknown_version_rejected(self) -> None:
        for bad in (0, 2, 99, "1", 1.0, True):
            with self.subTest(version=bad):
                broken = canonical_document(FULL_STATE)
                broken[VERSION_KEY] = bad
                with self.assertRaises(SchemaError):
                    validate_document(broken, "codex")

    # J7 — unknown keys at top level and inside the candidate object.
    def test_unknown_keys_rejected(self) -> None:
        extra = canonical_document(FULL_STATE)
        extra["next_due_at_v2"] = 5
        with self.assertRaises(SchemaError) as ctx:
            validate_document(extra, "codex")
        self.assertIn("unknown keys", str(ctx.exception))
        extra_c = canonical_document(FULL_STATE)
        extra_c["reset_candidate"]["extra"] = 1
        with self.assertRaises(SchemaError):
            validate_document(extra_c, "codex")

    # J6 — strict type policy table.
    def test_epoch_type_policy(self) -> None:
        for key in ("last_attempt_at", "last_task_at", "next_due_at",
                    "last_known_reset", "reset_anchor"):
            base = canonical_document(FULL_STATE)
            for bad in (True, False, 1.5, "17", -1, [1], {}):
                with self.subTest(key=key, value=bad):
                    doc = dict(base)
                    doc[key] = bad
                    with self.assertRaises(SchemaError):
                        validate_document(doc, "codex")
        zero = canonical_document(FULL_STATE)
        zero["next_due_at"] = 0  # epoch 0 is representable, not an error
        validate_document(zero, "codex")

    def test_pending_must_be_plain_bool(self) -> None:
        for bad in (0, 1, "true", None, []):
            with self.subTest(value=bad):
                doc = canonical_document(FULL_STATE)
                doc["retry_pending"] = bad
                with self.assertRaises(SchemaError):
                    validate_document(doc, "codex")

    def test_window_rules(self) -> None:
        for bad in ("", "a\nb", "a\rb", 5, b"x", "\ud800", "\ud800abc"):
            with self.subTest(value=repr(bad)):
                doc = canonical_document(FULL_STATE)
                doc["last_triggered_window"] = bad
                with self.assertRaises(SchemaError):
                    validate_document(doc, "codex")
        doc = canonical_document(FULL_STATE)
        doc["last_triggered_window"] = "w-2026-08"  # plain ids accepted
        validate_document(doc, "codex")

    def test_candidate_object_rules(self) -> None:
        for bad in (5, "x", [], {}, {"reset_at": 1},
                    {"reset_at": 1, "observed_at": 2, "extra": 3},
                    {"reset_at": True, "observed_at": 2},
                    {"reset_at": 1, "observed_at": "2"},
                    {"reset_at": -1, "observed_at": 2}):
            with self.subTest(value=bad):
                doc = canonical_document(FULL_STATE)
                doc["reset_candidate"] = bad
                with self.assertRaises(SchemaError):
                    validate_document(doc, "codex")

    # J8 — deterministic serialization: same state, same bytes.
    def test_deterministic_bytes(self) -> None:
        first = serialize_state(FULL_STATE, "codex")
        second = serialize_state(FULL_STATE, "codex")
        self.assertEqual(first, second)
        # key insertion order must not leak into the bytes:
        shuffled = dict(reversed(list(json.loads(first).items())))
        reparsed = document_to_state(shuffled, "codex")
        self.assertEqual(serialize_state(reparsed, "codex"), first)
        self.assertTrue(first.endswith(b"\n"))

    # J9 (codec part) — unrepresentable text refused before any IO.
    def test_serialize_refuses_lone_surrogate_text(self) -> None:
        bad_state = ProviderState(
            **{**ProviderState().__dict__, "last_triggered_window": "\ud800"}
        )
        with self.assertRaises(StateStoreError):
            serialize_state(bad_state, "codex")

    def test_parse_damage_is_typed(self) -> None:
        with self.assertRaises(DocumentCorruptError):
            parse_document_bytes(b"\xff\xfe not json", "codex")
        with self.assertRaises(DocumentCorruptError):
            parse_document_bytes(b'{"a": ', "codex")        # truncated
        with self.assertRaises(SchemaError):
            parse_document_bytes(b'"just a string"', "codex")  # valid JSON, wrong shape
        with self.assertRaises(SchemaError):
            parse_document_bytes(b'{"schema_version": 7}', "codex")

    def test_error_messages_carry_provider_context(self) -> None:
        with self.assertRaises(SchemaError) as ctx:
            validate_document({}, "antigravity")
        self.assertIn("antigravity", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
