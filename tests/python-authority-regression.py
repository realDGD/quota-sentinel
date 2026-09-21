#!/usr/bin/env python3
"""Phase 3B tests: durable backend authority, routing, and cutover.

What this suite exists to prove — the questions a crash or a concurrent
reader can actually ask:

  A. The durable authority fact is atomic, versioned, unique and loud.
     An absent manifest means "never cut over" (legacy); a damaged one is
     NEVER defaulted to either side.

  B. Routing has exactly one authority → backend mapping, never caches a
     selection across operations, and its generation-guarded read never
     returns a value from a retired backend.

  C. The cutover switches the whole roster in one epoch, takes its input
     from CURRENT legacy state (not the frozen shadow), leaves every
     legacy byte untouched, and is mechanically recoverable from EVERY
     crash prefix.

  D. Rollback is available only as a pure undo.

Run: PYTHONPATH=. uv run --frozen --no-sync python tests/python-authority-regression.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.state import (
    AUTHORITY_FILENAME,
    AuthorityMissingError,
    InitializationResult,
    initialize_authority,
    AUTHORITY_SCHEMA_VERSION,
    BACKEND_JSON,
    BACKEND_LEGACY,
    AuthoritativeStateStore,
    AuthorityError,
    AuthoritySchemaError,
    BackendAuthority,
    ConcurrentAuthorityChangeError,
    DocumentCorruptError,
    FileStateStore,
    JsonStateStore,
    MissingStateDocumentError,
    ProviderState,
    ResetCandidate,
    RollbackRefusedError,
    SchemaError,
    StateStoreError,
    authority_path,
    bootstrap_authority,
    cutover_to_json,
    parse_authority,
    read_authority,
    read_authority_if_present,
    rollback_to_legacy,
    serialize_authority,
    serialize_state,
    write_authority,
)
from quota_sentinel.state import authority as authority_module
from quota_sentinel.scheduler.models import QuotaObservation
from quota_sentinel.state import cutover as cutover_module
from quota_sentinel.state import store as store_module

PROVIDERS = ("codex", "antigravity", "opencode")
REAL_PUBLISH_ATOMIC = store_module._publish_atomic


class _InjectedCrash(RuntimeError):
    """Stands in for a process kill / power loss at a checkpoint."""


def legacy_state(seed: int) -> ProviderState:
    return ProviderState(
        last_attempt_at=1000 + seed,
        last_task_at=1000 + seed,
        next_due_at=2000 + seed,
        retry_pending=False,
        last_known_reset=900 + seed,
        last_triggered_window=str(900 + seed),
        reset_anchor=900 + seed,
        reset_candidate=None,
    )


class AuthorityBase(unittest.TestCase):
    """Base for tests that need a READABLE deployment.

    The manifest is required at runtime, so the throwaway deployment this
    base builds is explicitly initialized as legacy — the same state the
    installer leaves on a host that predates the protocol. Tests that are
    ABOUT the uninitialized case delete it (or use ``UninitializedBase``).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        self.state_dir.mkdir(parents=True)
        initialize_authority(self.state_dir)
        self.files = FileStateStore(self.state_dir)
        self.jsons = JsonStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- helpers --------------------------------------------------------
    def deinitialize(self) -> None:
        """Remove the ownership fact, leaving a state dir that predates it."""
        authority_path(self.state_dir).unlink()
    def write_legacy(self, provider: str, state: ProviderState) -> None:
        self.files.commit(provider, self.files.load(provider), state)

    def put_json(self, provider: str, state: ProviderState) -> None:
        """Write a document directly, bypassing the store's stale guard."""
        REAL_PUBLISH_ATOMIC(
            self.jsons.document_path(provider), serialize_state(state, provider)
        )

    def seed_roster(self) -> dict:
        states = {}
        for index, provider in enumerate(PROVIDERS):
            state = legacy_state(index * 10)
            self.write_legacy(provider, state)
            states[provider] = state
        return states

    def snapshot(self) -> dict:
        """Byte-level snapshot of every file in the state dir."""
        return {
            path.name: path.read_bytes()
            for path in sorted(self.state_dir.iterdir())
            if path.is_file()
        }

    def clear_state_dir(self) -> None:
        for path in list(self.state_dir.iterdir()):
            if path.is_file():
                path.unlink()


class AuthorityDocumentTests(AuthorityBase):
    """A. the durable fact itself."""

    def test_a1_absent_manifest_is_missing_not_legacy(self):
        """P1-2: absence is NOT a synonym for legacy any more.

        A deployment that predates the protocol is initialized explicitly;
        after that, a missing manifest means the ownership fact was lost,
        and guessing legacy would silently roll the scheduler back to state
        the authoritative backend has already superseded.
        """
        self.deinitialize()
        self.assertFalse(authority_path(self.state_dir).exists())
        with self.assertRaises(AuthorityMissingError):
            read_authority(self.state_dir)
        self.assertIsNone(read_authority_if_present(self.state_dir))
        # ... and the bootstrap authority is still available as a VALUE,
        # for the one function allowed to act on absence.
        self.assertEqual(bootstrap_authority().backend, BACKEND_LEGACY)
        self.assertEqual(bootstrap_authority().epoch, 0)

    def test_a1b_initialize_materializes_legacy_epoch_zero(self):
        self.deinitialize()
        result = initialize_authority(self.state_dir)
        self.assertTrue(result.created)
        self.assertEqual(result.authority, bootstrap_authority())
        self.assertEqual(read_authority(self.state_dir), bootstrap_authority())
        self.assertEqual(
            stat.S_IMODE(os.stat(authority_path(self.state_dir)).st_mode), 0o600
        )

    def test_a1c_initialize_is_idempotent_and_never_rewrites(self):
        before = authority_path(self.state_dir).read_bytes()
        result = initialize_authority(self.state_dir)
        self.assertFalse(result.created)
        self.assertEqual(result.authority, bootstrap_authority())
        self.assertEqual(authority_path(self.state_dir).read_bytes(), before)
        # An initialized-JSON deployment must not be downgraded to legacy.
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 4))
        json_before = authority_path(self.state_dir).read_bytes()
        again = initialize_authority(self.state_dir)
        self.assertFalse(again.created)
        self.assertEqual(again.authority, BackendAuthority(BACKEND_JSON, 4))
        self.assertEqual(authority_path(self.state_dir).read_bytes(), json_before)

    def test_a1d_initialize_ignores_json_documents(self):
        """Phase 2 shadow documents are NOT evidence of a cutover.

        They may have existed for months before the protocol, so inferring
        "JSON owns the state" from their presence would be exactly the
        guess this design forbids.
        """
        self.deinitialize()
        self.put_json("codex", legacy_state(0))
        result = initialize_authority(self.state_dir)
        self.assertEqual(result.authority.backend, BACKEND_LEGACY)

    def test_a1e_initialize_refuses_to_overwrite_corruption(self):
        authority_path(self.state_dir).write_bytes(b"{ not json")
        corrupt = authority_path(self.state_dir).read_bytes()
        with self.assertRaises(AuthorityError):
            initialize_authority(self.state_dir)
        self.assertEqual(authority_path(self.state_dir).read_bytes(), corrupt)

    def test_a2_read_is_pure_and_never_creates_the_manifest(self):
        before = self.snapshot()
        read_authority(self.state_dir)
        read_authority_if_present(self.state_dir)
        self.assertEqual(self.snapshot(), before)
        # A read must not resurrect a lost manifest either.
        self.deinitialize()
        with self.assertRaises(AuthorityMissingError):
            read_authority(self.state_dir)
        self.assertFalse(authority_path(self.state_dir).exists())

    def test_a3_write_read_round_trip_is_deterministic(self):
        authority = BackendAuthority(BACKEND_JSON, 7)
        write_authority(self.state_dir, authority)
        self.assertEqual(read_authority(self.state_dir), authority)
        first = authority_path(self.state_dir).read_bytes()
        write_authority(self.state_dir, authority)
        self.assertEqual(authority_path(self.state_dir).read_bytes(), first)
        self.assertEqual(
            serialize_authority(authority),
            json.dumps(
                {
                    "backend": "json",
                    "epoch": 7,
                    "schema_version": AUTHORITY_SCHEMA_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8") + b"\n",
        )

    def test_a4_directory_and_file_permissions(self):
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        self.assertEqual(
            stat.S_IMODE(os.stat(authority_path(self.state_dir)).st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.state_dir).st_mode), 0o700)

    def test_a5_failed_publish_keeps_the_previous_authority(self):
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        original = authority_module._publish_atomic
        authority_module._publish_atomic = _boom  # type: ignore[assignment]
        try:
            with self.assertRaises(AuthorityError):
                write_authority(self.state_dir, BackendAuthority(BACKEND_LEGACY, 2))
        finally:
            authority_module._publish_atomic = original  # type: ignore[assignment]
        self.assertEqual(
            read_authority(self.state_dir), BackendAuthority(BACKEND_JSON, 1)
        )

    def test_a6_damaged_manifest_is_loud_and_never_defaulted(self):
        cases = {
            "not json": b"{ this is not json",
            "invalid utf-8": b'{"backend": "\xff\xfe"}',
            "not an object": b'["json"]',
            "missing key": b'{"schema_version": 1, "backend": "json"}',
            "unknown key": (
                b'{"schema_version": 1, "backend": "json", "epoch": 1, "x": 2}'
            ),
            "wrong version": b'{"schema_version": 2, "backend": "json", "epoch": 1}',
            "bad backend": b'{"schema_version": 1, "backend": "sqlite", "epoch": 1}',
            "non-string backend": b'{"schema_version": 1, "backend": 3, "epoch": 1}',
            "bool epoch": b'{"schema_version": 1, "backend": "json", "epoch": true}',
            "negative epoch": b'{"schema_version": 1, "backend": "json", "epoch": -1}',
            "float epoch": b'{"schema_version": 1, "backend": "json", "epoch": 1.0}',
            "null backend": b'{"schema_version": 1, "backend": null, "epoch": 1}',
            "empty": b"",
        }
        target = self.state_dir / AUTHORITY_FILENAME
        for label, payload in cases.items():
            with self.subTest(label=label):
                target.write_bytes(payload)
                with self.assertRaises(AuthorityError):
                    read_authority(self.state_dir)
                # ... and the router refuses to serve state on that basis.
                with self.assertRaises(AuthorityError):
                    AuthoritativeStateStore(self.state_dir).load("codex")
                target.unlink()

    def test_a7_truncated_manifest_is_corrupt_not_bootstrap(self):
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 3))
        raw = authority_path(self.state_dir).read_bytes()
        authority_path(self.state_dir).write_bytes(raw[: len(raw) // 2])
        with self.assertRaises(AuthorityError):
            read_authority(self.state_dir)

    def test_a8_manifest_cannot_collide_with_provider_documents(self):
        # <provider>-state.json can never equal the manifest name for any
        # provider the stores accept.
        for provider in (*PROVIDERS, "backend", "backend-authority"):
            self.assertNotEqual(
                self.jsons.document_path(provider).name, AUTHORITY_FILENAME
            )
        with self.assertRaises(ValueError):
            self.jsons.document_path("../backend-authority")

    def test_a9_successor_increments_the_epoch(self):
        first = BackendAuthority(BACKEND_LEGACY, 0)
        second = first.successor(BACKEND_JSON)
        self.assertEqual(second, BackendAuthority(BACKEND_JSON, 1))
        self.assertEqual(second.successor(BACKEND_LEGACY).epoch, 2)
        with self.assertRaises(AuthoritySchemaError):
            BackendAuthority("sqlite", 0)
        with self.assertRaises(AuthoritySchemaError):
            BackendAuthority(BACKEND_JSON, True)

    def test_a10_parse_rejects_bool_version(self):
        with self.assertRaises(AuthoritySchemaError):
            parse_authority(
                b'{"schema_version": true, "backend": "json", "epoch": 0}'
            )


class RouterTests(AuthorityBase):
    """B. authority → backend selection."""

    def test_b1_legacy_authority_selects_the_file_store(self):
        states = self.seed_roster()
        router = AuthoritativeStateStore(self.state_dir)
        for provider, state in states.items():
            self.assertEqual(router.load(provider), state)
        self.assertEqual(router.authority(), bootstrap_authority())

    def test_b2_json_authority_selects_the_document_store(self):
        """Same provider, different durable content per backend: the value
        returned must follow the authority, never a cached selection."""
        self.write_legacy("codex", legacy_state(0))
        self.put_json(
            "codex", ProviderState(next_due_at=99999, retry_pending=True)
        )
        router = AuthoritativeStateStore(self.state_dir)
        self.assertEqual(router.load("codex").next_due_at, 2000)

        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        self.assertEqual(router.load("codex").next_due_at, 99999)
        self.assertTrue(router.load("codex").retry_pending)

        write_authority(self.state_dir, BackendAuthority(BACKEND_LEGACY, 2))
        self.assertEqual(router.load("codex").next_due_at, 2000)
        self.assertFalse(router.load("codex").retry_pending)

    def test_b3_authority_is_re_read_on_every_operation(self):
        self.seed_roster()
        for provider in PROVIDERS:
            self.put_json(provider, legacy_state(0))
        router = AuthoritativeStateStore(self.state_dir)
        reads: list = []
        router.on_before_authority_read = lambda: reads.append(1)
        router.load("codex")
        count_after_first = len(reads)
        self.assertGreaterEqual(count_after_first, 2)  # generation guard
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        router.commit("codex", legacy_state(0), ProviderState(next_due_at=4242))
        self.assertGreater(len(reads), count_after_first)

    def test_b4_flip_during_a_read_is_retried_not_mixed(self):
        """The decisive reader race: authority is read, then flips, then the
        state is read from the retired backend. The guard must discard that
        value and re-read from the newly authoritative backend."""
        self.write_legacy("codex", legacy_state(0))
        self.put_json("codex", ProviderState(next_due_at=55555))
        router = AuthoritativeStateStore(self.state_dir)
        calls = {"n": 0}

        def flip_between_reads():
            calls["n"] += 1
            if calls["n"] == 2:      # i.e. after the state read, before the guard
                write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))

        router.on_before_authority_read = flip_between_reads
        self.assertEqual(router.load("codex").next_due_at, 55555)

    def test_b5_unstable_authority_fails_loudly(self):
        self.seed_roster()
        for provider in PROVIDERS:
            self.put_json(provider, legacy_state(0))
        router = AuthoritativeStateStore(self.state_dir, max_read_attempts=3)
        counter = {"n": 0}

        def always_move():
            counter["n"] += 1
            backend = BACKEND_JSON if counter["n"] % 2 else BACKEND_LEGACY
            write_authority(self.state_dir, BackendAuthority(backend, counter["n"]))

        router.on_before_authority_read = always_move
        with self.assertRaises(ConcurrentAuthorityChangeError):
            router.load("codex")

    def test_b6_load_all_is_single_epoch(self):
        self.seed_roster()
        router = AuthoritativeStateStore(self.state_dir)
        states = router.load_all(PROVIDERS)
        self.assertEqual(set(states), set(PROVIDERS))
        self.assertEqual(states["codex"], legacy_state(0))

    def test_b7_commit_targets_only_the_authoritative_backend(self):
        self.seed_roster()
        for provider in PROVIDERS:
            self.put_json(provider, legacy_state(0))
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        legacy_before = {
            provider: (self.state_dir / f"{provider}-last-attempt-at").read_bytes()
            for provider in PROVIDERS
        }
        router = AuthoritativeStateStore(self.state_dir)
        router.commit("codex", legacy_state(0), ProviderState(next_due_at=31337))
        self.assertEqual(router.load("codex").next_due_at, 31337)
        for provider in PROVIDERS:
            self.assertEqual(
                (self.state_dir / f"{provider}-last-attempt-at").read_bytes(),
                legacy_before[provider],
            )

    def test_b8_commit_detects_authority_moving_under_it(self):
        self.seed_roster()
        for provider in PROVIDERS:
            self.put_json(provider, legacy_state(0))
        router = AuthoritativeStateStore(self.state_dir)
        calls = {"n": 0}

        def flip_between_reads():
            calls["n"] += 1
            if calls["n"] == 2:
                # The commit itself runs between the two authority reads;
                # simulate a cutover that ignored the run.lock precondition.
                write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))

        router.on_before_authority_read = flip_between_reads
        with self.assertRaises(ConcurrentAuthorityChangeError):
            router.commit(
                "codex",
                legacy_state(0),
                replace(legacy_state(0), next_due_at=1),
            )

    def test_b9_json_authoritative_load_failures_never_fall_back(self):
        self.seed_roster()   # legacy has real state for every provider
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        router = AuthoritativeStateStore(self.state_dir)

        with self.assertRaises(MissingStateDocumentError):
            router.load("codex")

        self.jsons.document_path("codex").write_bytes(b"{ not json")
        with self.assertRaises(DocumentCorruptError):
            router.load("codex")

        self.jsons.document_path("codex").write_text(
            json.dumps({"schema_version": 1, "backend": "json"}), encoding="utf-8"
        )
        with self.assertRaises(SchemaError):
            router.load("codex")

        # ... and a mutation built on top of that failure fails closed
        # rather than defaulting the provider to "all unset".
        with self.assertRaises(StateStoreError):
            router.commit("codex", ProviderState(), ProviderState(next_due_at=5))

    def test_b10_provider_named_like_the_manifest_is_not_state(self):
        self.write_legacy("backend", legacy_state(1))
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        self.assertFalse((self.state_dir / "backend-state.json").exists())
        router = AuthoritativeStateStore(self.state_dir)
        with self.assertRaises(MissingStateDocumentError):
            router.load("backend")

    def test_b11_router_rejects_bad_provider_names_on_every_backend(self):
        write_authority(self.state_dir, BackendAuthority(BACKEND_JSON, 1))
        for router_backend in (BACKEND_JSON, BACKEND_LEGACY):
            write_authority(self.state_dir, BackendAuthority(router_backend, 1))
            router = AuthoritativeStateStore(self.state_dir)
            with self.assertRaises(ValueError):
                router.load("../../etc/passwd")


def _boom(*args, **kwargs):
    raise OSError("injected publish failure")


class CutoverTests(AuthorityBase):
    """C. the ownership switch itself."""

    def test_c1_full_cutover_is_one_epoch_over_the_whole_roster(self):
        states = self.seed_roster()
        legacy_bytes = self.snapshot()
        result = cutover_to_json(self.state_dir)

        self.assertTrue(result.changed)
        self.assertEqual(result.previous, BackendAuthority(BACKEND_LEGACY, 0))
        self.assertEqual(result.current, BackendAuthority(BACKEND_JSON, 1))
        self.assertEqual(read_authority(self.state_dir), result.current)

        router = AuthoritativeStateStore(self.state_dir)
        for provider, state in states.items():
            with self.subTest(provider=provider):
                self.assertEqual(self.jsons.load(provider), state)
                self.assertEqual(router.load(provider), state)

        after = self.snapshot()
        for name, payload in legacy_bytes.items():
            if name == AUTHORITY_FILENAME:
                continue          # the manifest is what the cutover changes
            with self.subTest(file=name):
                self.assertEqual(after[name], payload)

    def test_c1b_cutover_leaves_no_stray_temp_files(self):
        self.seed_roster()
        cutover_to_json(self.state_dir)
        self.assertEqual([n for n in self.snapshot() if ".tmp." in n], [])

    def test_c2_cutover_is_idempotent(self):
        self.seed_roster()
        cutover_to_json(self.state_dir)
        before = self.snapshot()
        again = cutover_to_json(self.state_dir)
        self.assertFalse(again.changed)
        self.assertEqual(again.current, BackendAuthority(BACKEND_JSON, 1))
        self.assertEqual(self.snapshot(), before)

    def test_c3_source_of_truth_is_current_legacy_not_the_frozen_shadow(self):
        self.write_legacy("codex", legacy_state(0))
        # A frozen, semantically wrong shadow from Phase 2.
        self.put_json("codex", ProviderState(next_due_at=1, retry_pending=True))
        # Legacy moved on AFTER the shadow was frozen.
        current = replace(
            legacy_state(0), next_due_at=8888, last_task_at=7777
        )
        self.write_legacy("codex", current)

        cutover_to_json(self.state_dir, providers=["codex"])
        self.assertEqual(
            AuthoritativeStateStore(self.state_dir).load("codex"), current
        )

    def test_c4_corrupt_shadow_does_not_block_cutover(self):
        self.seed_roster()
        for provider in PROVIDERS:
            self.put_json(provider, legacy_state(0))
        self.jsons.document_path("codex").write_bytes(b"\xff\xfe not utf-8")
        cutover_to_json(self.state_dir)
        self.assertEqual(
            AuthoritativeStateStore(self.state_dir).load("codex"), legacy_state(0)
        )

    def test_c5_verification_failure_does_not_switch_authority(self):
        self.seed_roster()
        original = cutover_module.prepare_provider_cutover
        calls = {"n": 0}

        def tampering_prepare(state_dir, provider):
            result = original(state_dir, provider)
            calls["n"] += 1
            if calls["n"] == 2:
                # A document that looked fine per-provider but is found
                # inconsistent by the roster-wide check.
                REAL_PUBLISH_ATOMIC(
                    JsonStateStore(state_dir).document_path(provider),
                    serialize_state(ProviderState(next_due_at=1), provider),
                )
            return result

        cutover_module.prepare_provider_cutover = tampering_prepare  # type: ignore
        try:
            with self.assertRaises(cutover_module.CutoverVerificationError):
                cutover_to_json(self.state_dir)
        finally:
            cutover_module.prepare_provider_cutover = original  # type: ignore

        self.assertEqual(read_authority(self.state_dir), bootstrap_authority())

    def test_c6_rollback_is_a_pure_undo(self):
        states = self.seed_roster()
        cutover_to_json(self.state_dir)
        result = rollback_to_legacy(self.state_dir)
        self.assertTrue(result.changed)
        self.assertEqual(result.current, BackendAuthority(BACKEND_LEGACY, 2))
        self.assertEqual(read_authority(self.state_dir).backend, BACKEND_LEGACY)
        for provider, state in states.items():
            self.assertEqual(
                AuthoritativeStateStore(self.state_dir).load(provider), state
            )

    def test_c7_rollback_refuses_after_json_has_advanced(self):
        self.seed_roster()
        cutover_to_json(self.state_dir)
        router = AuthoritativeStateStore(self.state_dir)
        router.commit("codex", legacy_state(0), ProviderState(next_due_at=4321))
        with self.assertRaises(RollbackRefusedError):
            rollback_to_legacy(self.state_dir)
        # Refusal is inert: authority and state are untouched.
        self.assertEqual(read_authority(self.state_dir).backend, BACKEND_JSON)
        self.assertEqual(router.load("codex").next_due_at, 4321)

    def test_c8_rollback_idempotent_when_already_legacy(self):
        self.seed_roster()
        result = rollback_to_legacy(self.state_dir)
        self.assertFalse(result.changed)
        self.assertEqual(result.current, bootstrap_authority())

    def test_c9_cutover_with_empty_legacy_state_is_valid(self):
        """A fresh deployment with no legacy files at all still cuts over to
        a complete, all-null document set — never to a missing document."""
        result = cutover_to_json(self.state_dir)
        self.assertTrue(result.changed)
        router = AuthoritativeStateStore(self.state_dir)
        for provider in PROVIDERS:
            self.assertEqual(router.load(provider), ProviderState())


class CrashMatrixTests(AuthorityBase):
    """C (continued). every crash prefix must leave a determinable owner."""

    EXPECTED = {
        cutover_module.STEP_BEFORE_READ_LEGACY: BACKEND_LEGACY,
        cutover_module.STEP_AFTER_READ_LEGACY: BACKEND_LEGACY,
        cutover_module.STEP_BEFORE_PREPARE_JSON: BACKEND_LEGACY,
        cutover_module.STEP_AFTER_PREPARE_PROVIDER + "codex": BACKEND_LEGACY,
        cutover_module.STEP_AFTER_PREPARE_PROVIDER + "antigravity": BACKEND_LEGACY,
        cutover_module.STEP_AFTER_PREPARE_PROVIDER + "opencode": BACKEND_LEGACY,
        cutover_module.STEP_AFTER_PREPARE_JSON: BACKEND_LEGACY,
        cutover_module.STEP_BEFORE_VERIFY_JSON: BACKEND_LEGACY,
        cutover_module.STEP_AFTER_VERIFY_JSON: BACKEND_LEGACY,
        cutover_module.STEP_BEFORE_FLIP: BACKEND_LEGACY,
        cutover_module.STEP_AFTER_FLIP: BACKEND_JSON,
        cutover_module.STEP_AFTER_CONFIRM: BACKEND_JSON,
    }

    def test_d1_every_crash_prefix_is_mechanically_recoverable(self):
        """The manifest is MATERIALIZED before the cutover starts.

        The old matrix used "manifest absent" to represent crash-before-flip.
        Absence no longer carries that meaning — it means the ownership fact
        was lost — so every pre-flip checkpoint now asserts a present LEGACY
        manifest at epoch 0, and every post-flip checkpoint a JSON manifest
        at epoch 1. Same recovery guarantee, stated against a fact that is
        always there.
        """
        for checkpoint, expected in self.EXPECTED.items():
            with self.subTest(checkpoint=checkpoint):
                self.clear_state_dir()
                states = self.seed_roster()
                # clear_state_dir removed the manifest; a real deployment
                # is always initialized, so restore that FIRST.
                initialize_authority(self.state_dir)
                legacy_bytes = self.snapshot()

                def crash(name, target=checkpoint):
                    if name == target:
                        raise _InjectedCrash(name)

                with self.assertRaises(_InjectedCrash):
                    cutover_to_json(self.state_dir, checkpoint=crash)

                # 1. ownership is determinable, and is the expected side —
                #    from a PRESENT manifest, never from its absence.
                self.assertTrue(authority_path(self.state_dir).exists())
                authority = read_authority(self.state_dir)
                self.assertEqual(authority.backend, expected)
                self.assertEqual(
                    authority.epoch, 0 if expected == BACKEND_LEGACY else 1
                )

                # 2. legacy is byte-identical: never written, never
                #    repaired. The manifest is excluded because changing it
                #    IS the cutover.
                after = self.snapshot()
                for name, payload in legacy_bytes.items():
                    if name == AUTHORITY_FILENAME:
                        continue
                    self.assertEqual(after[name], payload, name)

                # 3. every present document is complete and decodes.
                for provider in PROVIDERS:
                    if self.jsons.exists(provider):
                        self.assertIsInstance(
                            self.jsons.load(provider), ProviderState
                        )

                # 4. the router answers from the authoritative side.
                router = AuthoritativeStateStore(self.state_dir)
                for provider, state in states.items():
                    self.assertEqual(router.load(provider), state)

                # 5. no torn temp files left behind.
                self.assertEqual([n for n in after if ".tmp." in n], [])

                # 6. re-running from the crashed state converges (a crash
                #    after the flip makes the re-run an idempotent no-op).
                resumed = cutover_to_json(self.state_dir)
                self.assertEqual(
                    read_authority(self.state_dir).backend, BACKEND_JSON
                )
                self.assertEqual(resumed.changed, expected == BACKEND_LEGACY)
                for provider, state in states.items():
                    self.assertEqual(router.load(provider), state)

    def test_d2_crash_inside_the_flip_leaves_old_or_new_complete(self):
        """The manifest publish is the commit point; both of its failure
        modes must still yield a readable, complete fact."""
        original = authority_module._publish_atomic

        def crash_before_rename(path, payload):
            raise _InjectedCrash("before rename")

        def crash_after_rename(path, payload):
            REAL_PUBLISH_ATOMIC(path, payload)
            raise _InjectedCrash("after rename")

        for label, hook in (
            ("before rename", crash_before_rename),
            ("after rename", crash_after_rename),
        ):
            with self.subTest(label=label):
                self.clear_state_dir()
                self.seed_roster()
                initialize_authority(self.state_dir)
                authority_module._publish_atomic = hook  # type: ignore[assignment]
                try:
                    with self.assertRaises(_InjectedCrash):
                        cutover_to_json(self.state_dir)
                finally:
                    authority_module._publish_atomic = original  # type: ignore

                authority = read_authority(self.state_dir)
                self.assertIn(authority.backend, (BACKEND_LEGACY, BACKEND_JSON))
                states = AuthoritativeStateStore(self.state_dir).load_all(PROVIDERS)
                self.assertEqual(set(states), set(PROVIDERS))
                # Either the manifest is absent (the crash landed before the
                # rename, so ownership is still bootstrap-legacy) or it is a
                # complete document that parses to exactly what was read.
                manifest = authority_path(self.state_dir)
                self.assertTrue(
                    manifest.exists(),
                    "the manifest must never disappear: the deployment was "
                    "initialized before the cutover started",
                )
                self.assertEqual(
                    parse_authority(manifest.read_bytes()), authority
                )
                self.assertEqual(
                    [n for n in self.snapshot() if ".tmp." in n], []
                )

    def test_d3_legacy_writes_after_cutover_are_not_visible(self):
        """After the flip the retired backend must not be read, even if a
        stale process wrote to it."""
        states = self.seed_roster()
        cutover_to_json(self.state_dir)
        self.write_legacy("codex", replace(legacy_state(0), next_due_at=1))
        self.assertEqual(
            AuthoritativeStateStore(self.state_dir).load("codex"),
            states["codex"],
        )

    def test_d4_writer_crash_before_flip_leaves_legacy_authoritative(self):
        self.seed_roster()
        self.put_json("codex", ProviderState(next_due_at=999))
        # Nothing has switched, so a fresh process must still read legacy.
        self.assertEqual(
            AuthoritativeStateStore(self.state_dir).load("codex"),
            legacy_state(0),
        )


class CrossProcessTests(AuthorityBase):
    """D. the fact is durable and readable by a separate process."""

    def test_e1_fresh_process_reads_the_same_authority(self):
        self.seed_roster()
        cutover_to_json(self.state_dir)
        repo = Path(__file__).resolve().parent.parent
        program = (
            "import sys;"
            f"sys.path.insert(0, {str(repo)!r});"
            "from pathlib import Path;"
            "from quota_sentinel.state import AuthoritativeStateStore;"
            f"r = AuthoritativeStateStore(Path({str(self.state_dir)!r}));"
            "print(r.authority().backend, r.load('codex').next_due_at)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "json 2000")

    def test_e2_document_carries_no_local_paths(self):
        self.seed_roster()
        cutover_to_json(self.state_dir)
        raw = authority_path(self.state_dir).read_text(encoding="utf-8")
        self.assertNotIn("Users", raw)
        self.assertNotIn(str(self.state_dir), raw)

    def test_e3_temp_files_are_never_left_by_a_failed_read(self):
        self.seed_roster()
        before = sorted(self.snapshot())
        for _ in range(3):
            read_authority(self.state_dir)
            AuthoritativeStateStore(self.state_dir).load_all(PROVIDERS)
        self.assertEqual(sorted(self.snapshot()), before)


class MissingManifestTests(AuthorityBase):
    """M3/M4 at the unit level: a LOST ownership fact fails closed.

    The end-to-end versions of these live in
    tests/state-authority-regression.zsh; these pin the library contract
    that the shell relies on.
    """

    def _authoritative_json_deployment(self):
        """Cut over, then advance the JSON state past the legacy snapshot."""
        self.write_legacy("codex", legacy_state(0))
        cutover_to_json(self.state_dir, providers=["codex"])
        store = AuthoritativeStateStore(self.state_dir)
        store.commit(
            "codex",
            legacy_state(0),
            replace(legacy_state(0), next_due_at=99999, retry_pending=True),
        )
        return store

    def test_m3_json_advanced_and_manifest_lost_never_returns_legacy(self):
        store = self._authoritative_json_deployment()
        advanced = store.load("codex")
        self.assertEqual(advanced.next_due_at, 99999)
        document_before = self.jsons.document_path("codex").read_bytes()
        legacy_before = (
            self.state_dir / "codex-next-due-at"
        ).read_bytes()

        self.deinitialize()

        with self.assertRaises(AuthorityMissingError):
            AuthoritativeStateStore(self.state_dir).load("codex")
        with self.assertRaises(AuthorityMissingError):
            AuthoritativeStateStore(self.state_dir).load_all(["codex"])
        # Nothing was repaired, rewritten, or "recovered".
        self.assertEqual(
            self.jsons.document_path("codex").read_bytes(), document_before
        )
        self.assertEqual(
            (self.state_dir / "codex-next-due-at").read_bytes(), legacy_before
        )
        self.assertFalse(authority_path(self.state_dir).exists())

    def test_m4_mutations_fail_closed_with_no_manifest(self):
        self.write_legacy("codex", legacy_state(0))
        cutover_to_json(self.state_dir, providers=["codex"])
        document_before = self.jsons.document_path("codex").read_bytes()
        legacy_before = (self.state_dir / "codex-next-due-at").read_bytes()
        self.deinitialize()

        from quota_sentinel.scheduler import service
        for call in (
            lambda: service.commit_success(self.state_dir, "codex", 5000),
            lambda: service.begin_attempt(self.state_dir, "codex", 5000),
            lambda: service.record_attempt(self.state_dir, "codex", 5000),
            lambda: service.record_last_window(self.state_dir, "codex", 5000),
            lambda: service.decide_due(
                self.state_dir, "codex", 5000, QuotaObservation()
            ),
        ):
            with self.subTest(call=call):
                with self.assertRaises(AuthorityMissingError):
                    call()
        # Fail closed means: no default state written anywhere.
        self.assertEqual(
            self.jsons.document_path("codex").read_bytes(), document_before
        )
        self.assertEqual(
            (self.state_dir / "codex-next-due-at").read_bytes(), legacy_before
        )

    def test_m2_a_lost_manifest_is_never_recreated_by_a_read(self):
        self.deinitialize()
        for _ in range(3):
            with self.assertRaises(AuthorityMissingError):
                AuthoritativeStateStore(self.state_dir).load("codex")
        self.assertFalse(
            authority_path(self.state_dir).exists(),
            "a read re-created the ownership fact",
        )


class InitializationCrashTests(AuthorityBase):
    """Initialization is crash-safe: absent, or complete legacy epoch 0."""

    def test_crash_before_and_after_the_publish(self):
        original = authority_module._publish_atomic

        def crash_before(path, payload):
            raise _InjectedCrash("before publish")

        def crash_after(path, payload):
            REAL_PUBLISH_ATOMIC(path, payload)
            raise _InjectedCrash("after publish")

        for label, hook, expect_created in (
            ("before publish", crash_before, False),
            ("after publish", crash_after, True),
        ):
            with self.subTest(label=label):
                self.clear_state_dir()
                authority_module._publish_atomic = hook  # type: ignore[assignment]
                try:
                    with self.assertRaises(_InjectedCrash):
                        initialize_authority(self.state_dir)
                finally:
                    authority_module._publish_atomic = original  # type: ignore

                manifest = authority_path(self.state_dir)
                if expect_created:
                    self.assertTrue(manifest.exists())
                    self.assertEqual(
                        parse_authority(manifest.read_bytes()),
                        bootstrap_authority(),
                    )
                else:
                    self.assertFalse(manifest.exists())
                    with self.assertRaises(AuthorityMissingError):
                        read_authority(self.state_dir)
                # Either way: no torn file, no temp litter, and re-running
                # initialization converges.
                self.assertEqual(
                    [n for n in self.snapshot() if ".tmp." in n], []
                )
                result = initialize_authority(self.state_dir)
                # Created only when the crashed attempt left nothing; a
                # crash AFTER the publish means the fact is already there.
                self.assertEqual(result.created, not expect_created)
                self.assertEqual(read_authority(self.state_dir),
                                 bootstrap_authority())


class LifecycleLockTests(AuthorityBase):
    """P1-1: the operator lifecycle path cannot bypass run.lock.

    A deterministic reproducer for the interleaving that made the INTERNAL
    lifecycle verbs unsafe as operator commands, followed by the property
    that closes it: the public entry point cannot start while a writer owns
    the scheduler's lock.
    """

    def test_l1_internal_rollback_window_loses_an_unsynchronized_update(self):
        self.write_legacy("codex", legacy_state(0))
        cutover_to_json(self.state_dir, providers=["codex"])
        store = AuthoritativeStateStore(self.state_dir)
        self.assertEqual(store.load("codex"), legacy_state(0))

        advanced = replace(legacy_state(0), next_due_at=4242, retry_pending=True)
        observed = {}

        def writer_commits_in_the_window(name):
            # Simulates a writer that does NOT hold run.lock: it lands
            # between the rollback's comparison and its flip. This is the
            # exact interleaving an operator invoking the internal verb by
            # hand could produce.
            if name == cutover_module.STEP_BEFORE_FLIP:
                AuthoritativeStateStore(self.state_dir).commit(
                    "codex", legacy_state(0), advanced
                )
                observed["committed"] = True

        rollback_to_legacy(
            self.state_dir, providers=["codex"], checkpoint=writer_commits_in_the_window
        )
        self.assertTrue(observed.get("committed"))
        # The update is now INVISIBLE: ownership went back to legacy while
        # the authoritative document had already moved on.
        self.assertEqual(read_authority(self.state_dir).backend, BACKEND_LEGACY)
        self.assertEqual(
            AuthoritativeStateStore(self.state_dir).load("codex"),
            legacy_state(0),
        )
        self.assertNotEqual(
            AuthoritativeStateStore(self.state_dir).load("codex"), advanced
        )

    def test_l1_public_path_cannot_start_while_the_lock_is_held(self):
        """The property that closes the window above.

        Cutover and rollback acquire the scheduler's real run.lock through
        the same shlock protocol the shell uses, so a writer holding the
        lock makes them fail rather than interleave.
        """
        from quota_sentinel.state import (
            RunLockBusyError,
            acquire_run_lock,
        )

        with acquire_run_lock(self.state_dir, timeout=0):
            with self.assertRaises(RunLockBusyError):
                acquire_run_lock(self.state_dir, timeout=0)
            self.assertTrue(
                self.state_dir.joinpath("run.lock").exists(),
                "the lock file is what the shell and the CLI contend on",
            )
        # Released -> immediately acquirable again (no stale lock).
        with acquire_run_lock(self.state_dir, timeout=0) as lock:
            self.assertFalse(lock.released)
        self.assertFalse(self.state_dir.joinpath("run.lock").exists())

    def test_runlock_is_the_shells_protocol(self):
        """Same binary, same file, same pid semantics — not a second lock."""
        from quota_sentinel.state import RUN_LOCK_FILENAME, SHLOCK_BIN
        from quota_sentinel.state import runlock as runlock_module

        self.assertEqual(RUN_LOCK_FILENAME, "run.lock")
        self.assertEqual(SHLOCK_BIN, "/usr/bin/shlock")
        self.assertTrue(os.access(SHLOCK_BIN, os.X_OK))
        shell = (REPO / "quota-sentinel.sh").read_text(encoding="utf-8")
        self.assertIn('readonly RUN_LOCK_FILE="$STATE_DIR/run.lock"', shell)
        self.assertIn('readonly SHLOCK_BIN="/usr/bin/shlock"', shell)
        # ... and the acquisition actually records OUR pid.
        with runlock_module.acquire_run_lock(self.state_dir, timeout=0) as lock:
            recorded = int(
                (self.state_dir / RUN_LOCK_FILENAME).read_text().strip()
            )
            self.assertEqual(recorded, os.getpid())
            self.assertEqual(lock.pid, os.getpid())

    def test_release_is_idempotent_and_never_deletes_a_new_owners_lock(self):
        from quota_sentinel.state import acquire_run_lock

        first = acquire_run_lock(self.state_dir, timeout=0)
        first.release()
        second = acquire_run_lock(self.state_dir, timeout=0)
        # A stale second release must not remove the NEW owner's lock file.
        first.release()
        self.assertTrue(self.state_dir.joinpath("run.lock").exists())
        second.release()
        self.assertFalse(self.state_dir.joinpath("run.lock").exists())


class LifecyclePrerequisiteTests(AuthorityBase):
    """Cutover and rollback require an INITIALIZED deployment."""

    def test_l2_cutover_refuses_without_an_initialized_authority(self):
        self.write_legacy("codex", legacy_state(0))
        self.deinitialize()
        with self.assertRaises(AuthorityMissingError):
            cutover_to_json(self.state_dir, providers=["codex"])
        # Nothing was created, and ownership was not invented.
        self.assertFalse(authority_path(self.state_dir).exists())
        self.assertFalse(self.jsons.exists("codex"))

    def test_l2_rollback_refuses_without_an_initialized_authority(self):
        self.deinitialize()
        with self.assertRaises(AuthorityMissingError):
            rollback_to_legacy(self.state_dir, providers=["codex"])
        self.assertFalse(authority_path(self.state_dir).exists())

    def test_migrate_refuses_under_json_authority(self):
        """Seeding a missing document from legacy would resurrect retired
        state — the same failure mode as guessing legacy from absence."""
        self.write_legacy("codex", legacy_state(0))
        cutover_to_json(self.state_dir, providers=["codex"])
        document_before = self.jsons.document_path("codex").read_bytes()
        self.jsons.document_path("codex").unlink()

        from quota_sentinel.__main__ import main
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "--state-dir", str(self.state_dir), "migrate", "codex",
            ])
        self.assertEqual(rc, 4)
        self.assertIn("refusing to seed", err.getvalue())
        self.assertFalse(
            self.jsons.exists("codex"),
            "migrate re-created a document from the retired backend",
        )
        self.assertEqual(document_before, document_before)  # unchanged input


if __name__ == "__main__":
    unittest.main(verbosity=2)
