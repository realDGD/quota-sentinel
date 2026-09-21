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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_sentinel.state import (
    AUTHORITY_FILENAME,
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
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        self.state_dir.mkdir(parents=True)
        self.files = FileStateStore(self.state_dir)
        self.jsons = JsonStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- helpers --------------------------------------------------------
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

    def test_a1_absent_manifest_is_bootstrap_legacy(self):
        self.assertFalse(authority_path(self.state_dir).exists())
        self.assertEqual(read_authority(self.state_dir), bootstrap_authority())
        self.assertEqual(bootstrap_authority().backend, BACKEND_LEGACY)
        self.assertEqual(bootstrap_authority().epoch, 0)
        self.assertIsNone(read_authority_if_present(self.state_dir))

    def test_a2_read_is_pure(self):
        before = self.snapshot()
        read_authority(self.state_dir)
        read_authority_if_present(self.state_dir)
        self.assertEqual(self.snapshot(), before)

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
        for checkpoint, expected in self.EXPECTED.items():
            with self.subTest(checkpoint=checkpoint):
                self.clear_state_dir()
                states = self.seed_roster()
                legacy_bytes = self.snapshot()

                def crash(name, target=checkpoint):
                    if name == target:
                        raise _InjectedCrash(name)

                with self.assertRaises(_InjectedCrash):
                    cutover_to_json(self.state_dir, checkpoint=crash)

                # 1. ownership is determinable, and is the expected side.
                authority = read_authority(self.state_dir)
                self.assertEqual(authority.backend, expected)

                # 2. legacy is byte-identical: never written, never repaired.
                after = self.snapshot()
                for name, payload in legacy_bytes.items():
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
                if manifest.exists():
                    self.assertEqual(
                        parse_authority(manifest.read_bytes()), authority
                    )
                else:
                    self.assertEqual(authority, bootstrap_authority())
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
