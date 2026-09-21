#!/usr/bin/env python3
"""Phase 3A tests: cutover PREPARATION (cutover.py).

Contract pinned here (C-numbers map to the Phase 3A task):
* C1 stale shadow is REFRESHED from CURRENT legacy (the decisive case);
* C2 absent shadow is created;
* C3 an all-null stale shadow is refreshed once legacy has real state;
* C4 idempotent: second prepare is a semantic no-op, zero writes;
* C5 prepare != migrate: where migrate skips (json-wins), prepare
  rebuilds from current legacy;
* C7 verification compares ProviderState semantics, not bytes;
* C8 domain/schema failure on current legacy -> typed error, shadow not
  created, legacy untouched;
* C9 publish failure -> old shadow stays complete, legacy untouched and
  authoritative, typed error;
* C10 corrupt shadow does not block rebuild from the source of truth;
plus mode/roster/legacy-immutability invariants.
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
    ACTION_EXISTS,
    ACTION_SEEDED,
    DEFAULT_PROVIDERS,
    FileStateStore,
    JsonStateStore,
    ProviderState,
    ResetCandidate,
    StateStoreError,
    migrate_provider,
    serialize_state,
)
from quota_sentinel.state.cutover import (
    CutoverPreparation,
    prepare_all_cutover,
    prepare_provider_cutover,
)

LEGACY = ProviderState(
    last_attempt_at=200,
    last_task_at=200,
    next_due_at=100,
    retry_pending=False,
    last_known_reset=90,
    last_triggered_window="90",
    reset_anchor=90,
    reset_candidate=None,
)
STALE = ProviderState(
    **{**LEGACY.__dict__, "next_due_at": 55}       # older shadow value
)


class CutoverPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        self.state_dir.mkdir(parents=True)
        self.files = FileStateStore(self.state_dir)
        self.jsons = JsonStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_legacy(self, provider: str, state: ProviderState) -> None:
        self.files.commit(provider, self.files.load(provider), state)

    def doc_path(self, provider: str) -> Path:
        return self.state_dir / f"{provider}-state.json"

    def slot_signature(self) -> list:
        return sorted(
            (p.name, p.read_bytes())
            for p in self.state_dir.iterdir()
            if p.name.endswith(".json") is False
        )

    # ---- C1: the decisive stale-shadow refresh -------------------------------
    def test_c1_stale_shadow_refreshed_from_current_legacy(self) -> None:
        self.write_legacy("codex", STALE)          # legacy = 55 era
        prepare = prepare_provider_cutover(self.state_dir, "codex")
        self.assertTrue(prepare.changed)
        self.write_legacy("codex", LEGACY)         # shell drifts legacy to 100
        prep = prepare_provider_cutover(self.state_dir, "codex")
        self.assertTrue(prep.changed)              # NOT skipped-for-exists!
        self.assertEqual(self.jsons.load("codex"), LEGACY)
        self.assertEqual(self.files.load("codex"), LEGACY)
        self.assertEqual(prep.legacy_state, LEGACY)
        self.assertEqual(prep.json_state, LEGACY)
        self.assertEqual(prep.provider, "codex")

    # ---- C2: absent shadow created -------------------------------------------
    def test_c2_absent_shadow_created(self) -> None:
        self.write_legacy("codex", LEGACY)
        self.assertFalse(self.doc_path("codex").exists())
        prep = prepare_provider_cutover(self.state_dir, "codex")
        self.assertTrue(prep.changed)
        self.assertEqual(self.jsons.load("codex"), LEGACY)

    # ---- C3: all-null stale shadow refreshed ---------------------------------
    def test_c3_all_null_stale_shadow_replaced(self) -> None:
        # Prepare before legacy exists -> frozen all-null snapshot.
        prep0 = prepare_provider_cutover(self.state_dir, "codex")
        self.assertEqual(prep0.json_state, ProviderState())
        # Time passes, scheduler (still writing slot files) has real state:
        self.write_legacy("codex", LEGACY)
        prep = prepare_provider_cutover(self.state_dir, "codex")
        self.assertTrue(prep.changed)
        self.assertEqual(self.jsons.load("codex"), LEGACY)

    # ---- C4: idempotency ------------------------------------------------------
    def test_c4_idempotent_second_run_zero_writes(self) -> None:
        self.write_legacy("codex", LEGACY)
        prepare_provider_cutover(self.state_dir, "codex")
        bytes1 = self.doc_path("codex").read_bytes()
        mtime1 = os.stat(self.doc_path("codex")).st_mtime_ns
        legacy_sig = self.slot_signature()
        prep2 = prepare_provider_cutover(self.state_dir, "codex")
        self.assertFalse(prep2.changed)
        self.assertEqual(self.doc_path("codex").read_bytes(), bytes1)
        self.assertEqual(os.stat(self.doc_path("codex")).st_mtime_ns, mtime1)
        # legacy byte-identical:
        self.assertEqual(self.slot_signature(), legacy_sig)

    # ---- C5: prepare vs migrate divergence ------------------------------------
    def test_c5_prepare_refreshes_where_migrate_skips(self) -> None:
        self.write_legacy("codex", STALE)
        self.assertEqual(
            migrate_provider(self.state_dir, "codex"), ACTION_SEEDED
        )
        self.write_legacy("codex", LEGACY)         # legacy drifts newer
        # migrate: json exists -> skip, shadow stays stale (frozen):
        self.assertEqual(
            migrate_provider(self.state_dir, "codex"), ACTION_EXISTS
        )
        self.assertEqual(self.jsons.load("codex"), STALE)
        # prepare: refreshes from CURRENT legacy (no skip-if-exists):
        prep = prepare_provider_cutover(self.state_dir, "codex")
        self.assertTrue(prep.changed)
        self.assertEqual(self.jsons.load("codex"), LEGACY)

    # ---- C7: verification is semantic (works across document forms) -----------
    def test_c7_semantic_verification_over_shapes(self) -> None:
        shapes = {
            "empty": ProviderState(),
            "full": ProviderState(
                last_attempt_at=1, last_task_at=2, next_due_at=3,
                retry_pending=True, last_known_reset=4,
                last_triggered_window="w-1", reset_anchor=5,
                reset_candidate=ResetCandidate(6, 7),
            ),
            "zero-epochs": ProviderState(next_due_at=0, reset_anchor=0),
            "candidate-only": ProviderState(
                reset_candidate=ResetCandidate(9, 8)
            ),
        }
        for label, state in shapes.items():
            with self.subTest(shape=label):
                provider = "antigravity"
                for p in self.state_dir.glob("*"):
                    p.unlink() if p.is_file() else None
                self.write_legacy(provider, state)
                prepare_provider_cutover(self.state_dir, provider)
                self.assertEqual(
                    self.jsons.load(provider), self.files.load(provider)
                )

    # ---- C8: bad current legacy -> typed refusal, zero mutation ---------------
    def test_c8_legacy_failing_domain_rules_refuses_pre_mutation(self) -> None:
        # A slot value FileStateStore happily reads (multi-line window) that
        # the domain boundary refuses: prepare must fail typed BEFORE
        # creating anything, leaving legacy exactly as found.
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / "codex-last-triggered-window").write_text(
            "a\nb\n", encoding="utf-8"
        )
        (self.state_dir / "codex-next-due-at").write_text("120\n", encoding="utf-8")
        legacy_sig = self.slot_signature()
        with self.assertRaises(StateStoreError) as ctx:
            prepare_provider_cutover(self.state_dir, "codex")
        self.assertIn("codex", str(ctx.exception))
        self.assertFalse(self.doc_path("codex").exists())   # shadow not created
        self.assertEqual(self.slot_signature(), legacy_sig)  # legacy untouched

    # ---- C9: publish failure keeps old shadow complete + legacy intact --------
    def test_c9_publish_failure_old_complete_legacy_untouched(self) -> None:
        self.write_legacy("codex", LEGACY)
        prepare_provider_cutover(self.state_dir, "codex")
        old_bytes = self.doc_path("codex").read_bytes()
        # legacy drifts (a legitimate shell-side change); snapshot AFTER:
        self.write_legacy("codex", STALE)
        legacy_sig = self.slot_signature()
        # now break the filesystem publish:
        import quota_sentinel.state.store as store_module
        real_replace = os.replace
        store_module.os.replace = lambda *a: (_ for _ in ()).throw(
            PermissionError("injected")
        )
        try:
            with self.assertRaises(StateStoreError) as ctx:
                prepare_provider_cutover(self.state_dir, "codex")
        finally:
            store_module.os.replace = real_replace
        self.assertIn("legacy untouched", str(ctx.exception))
        # old shadow still a COMPLETE, loadable document (its content is the
        # previously-verified LEGACY — old-complete, never torn):
        self.assertEqual(self.doc_path("codex").read_bytes(), old_bytes)
        self.assertEqual(self.jsons.load("codex"), LEGACY)
        self.assertEqual(self.slot_signature(), legacy_sig)
        self.assertEqual(
            [p.name for p in self.state_dir.iterdir() if ".tmp." in p.name], []
        )

    # ---- C10: corrupt shadow rebuilt from source of truth ----------------------
    def test_c10_corrupt_shadow_replaced_not_fatal(self) -> None:
        self.write_legacy("codex", LEGACY)
        self.doc_path("codex").write_bytes(b'{"schema_version": 1, "trunc')
        prep = prepare_provider_cutover(self.state_dir, "codex")
        self.assertTrue(prep.changed)
        self.assertEqual(self.jsons.load("codex"), LEGACY)
        # contrast: the ordinary loader still refuses the corrupt content
        # BEFORE any prepare — corruption is only bypassed by explicit
        # preparation, never by normal reads:
        self.doc_path("codex").write_bytes(b"\xff\xfe broken")
        with self.assertRaises(StateStoreError):
            self.jsons.load("codex")

    # ---- C11: verification failure AFTER a successful publish ------------------
    def test_c11_post_publish_verification_failure_contract(self) -> None:
        # R1 residual: the disproven old wording promised "shadow stays
        # old-complete-or-absent on ANY failure". The truthful branch:
        # publish SUCCEEDS, verification then fails -> the shadow may
        # hold the NEW-complete document, legacy stays untouched and
        # authoritative, nothing torn, no rollback needed.
        #
        # Mechanical proof of "publish already happened": (a) spy counts
        # exactly one real _publish_atomic call that returned without
        # error, (b) the on-disk document equals serialize_state(LEGACY)
        # byte-for-byte, while (c) a lying verification load forces the
        # mismatch. Distinguished from C8/C9: those fail PRE-publish and
        # assert old/absent shadow; this fails POST-publish.
        import quota_sentinel.state.cutover as cutover_module

        # Arrange: shadow currently equals STALE, legacy currently LEGACY.
        self.write_legacy("codex", STALE)
        prepare_provider_cutover(self.state_dir, "codex")   # shadow = STALE
        self.write_legacy("codex", LEGACY)                   # legacy drifts on
        doc_before = self.doc_path("codex").read_bytes()
        self.assertEqual(
            json.loads(doc_before)["next_due_at"], STALE.next_due_at
        )
        legacy_sig = self.slot_signature()

        publish_calls = {"n": 0}
        real_publish = cutover_module._publish_atomic

        def spy_publish(path, payload):
            real_publish(path, payload)          # let it actually succeed
            publish_calls["n"] += 1

        loads = {"n": 0}
        real_load = JsonStateStore.load

        def lying_load(self, provider):
            loads["n"] += 1
            state = real_load(self, provider)
            if loads["n"] >= 2:                  # verification read lies
                return ProviderState(
                    **{**state.__dict__,
                       "next_due_at": (state.next_due_at or 0) + 1}
                )
            return state                         # usable-shadow read honest

        cutover_module._publish_atomic = spy_publish
        JsonStateStore.load = lying_load
        try:
            with self.assertRaises(StateStoreError) as ctx:
                prepare_provider_cutover(self.state_dir, "codex")
        finally:
            JsonStateStore.load = real_load
            cutover_module._publish_atomic = real_publish

        # prepare refused loudly, returned nothing (raises, no result):
        self.assertIn("cutover verification failed", str(ctx.exception))
        # (a) publish happened exactly once and completed:
        self.assertEqual(publish_calls["n"], 1)
        self.assertGreaterEqual(loads["n"], 2)
        # (b) shadow now holds the NEW, COMPLETE, semantic-current doc —
        #     allowed by the corrected contract; NOT old-complete:
        doc_after = self.doc_path("codex").read_bytes()
        self.assertNotEqual(doc_after, doc_before)
        self.assertEqual(
            doc_after, serialize_state(LEGACY, "codex")
        )
        self.assertEqual(self.jsons.load("codex"), LEGACY)   # parses & matches
        # (c) legacy byte-identical and authoritative (ownership never
        #     moved — no durable fact changed):
        self.assertEqual(self.slot_signature(), legacy_sig)
        self.assertEqual(self.files.load("codex"), LEGACY)
        # (d) no torn JSON, no temp debris:
        self.assertEqual(
            [p.name for p in self.state_dir.iterdir() if ".tmp." in p.name], []
        )

    # ---- invariants: modes, roster, legacy immutability, result shape ---------
    def test_modes_and_dir_ownership_on_write(self) -> None:
        fresh = self.state_dir / "sub"
        prep = prepare_provider_cutover(fresh, "codex")
        self.assertTrue(prep.changed)
        self.assertEqual(oct(os.stat(fresh).st_mode & 0o777), "0o700")
        self.assertEqual(
            oct(os.stat(fresh / "codex-state.json").st_mode & 0o777), "0o600"
        )

    def test_prepare_all_roster_matches_providers(self) -> None:
        result = prepare_all_cutover(self.state_dir)
        self.assertEqual(tuple(result), tuple(DEFAULT_PROVIDERS))
        for provider, prep in result.items():
            self.assertIsInstance(prep, CutoverPreparation)
            self.assertEqual(prep.provider, provider)
            self.assertTrue(prep.changed)          # all shadows created fresh
            self.assertEqual(prep.json_state, ProviderState())

    def test_prepare_never_touches_legacy_files(self) -> None:
        self.write_legacy("codex", LEGACY)
        sig_before = self.slot_signature()
        prepare_provider_cutover(self.state_dir, "codex")
        prepare_provider_cutover(self.state_dir, "codex")
        self.assertEqual(self.slot_signature(), sig_before)

    def test_ownership_semantics_documented_outcome(self) -> None:
        # After a SUCCESSFUL prepare, legacy is still exactly the current
        # authoritative representation: nothing in the legacy layout was
        # consumed, and a subsequent shell-style write + prepare refreshes
        # again — proving prepare is a repeatable shadow sync, not a switch.
        self.write_legacy("codex", LEGACY)
        prepare_provider_cutover(self.state_dir, "codex")
        self.write_legacy("codex", STALE)          # "shell" keeps writing legacy
        prep = prepare_provider_cutover(self.state_dir, "codex")
        self.assertTrue(prep.changed)
        self.assertEqual(self.jsons.load("codex"), STALE)
        self.assertEqual(self.files.load("codex"), STALE)


# ---- Phase 3A P3 (§31): CLI edge converts ValueErrors to typed errors -------
class CliErrorSurfaceTests(unittest.TestCase):
    """The CLI converts library ValueErrors (bad provider names) into
    concise typed non-zero exits; the library itself keeps raising."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *argv: str):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from quota_sentinel.__main__ import main
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = main(list(argv))
        return rc, err.getvalue()

    def test_invalid_provider_on_every_read_verb(self) -> None:
        for cmd in ("json-dump", "dump", "next-due"):
            with self.subTest(cmd=cmd):
                rc, err = self._run("--state-dir", str(self.dir), cmd, "../evil")
                self.assertEqual(rc, 3)
                self.assertIn("invalid argument", err)
                self.assertNotIn("Traceback", err)

    def test_migrate_invalid_provider(self) -> None:
        rc, err = self._run("--state-dir", str(self.dir), "migrate", "../evil")
        self.assertEqual(rc, 3)
        self.assertIn("invalid argument", err)
        self.assertNotIn("Traceback", err)

    def test_missing_document_still_rc4_typed(self) -> None:
        rc, err = self._run("--state-dir", str(self.dir), "json-dump", "codex")
        self.assertEqual(rc, 4)
        self.assertIn("no state document", err)
        self.assertNotIn("Traceback", err)

    def test_library_contract_not_softened_by_cli(self) -> None:
        store = JsonStateStore(self.dir)
        with self.assertRaises(ValueError):
            store.document_path("../evil")


# ---- Phase 3A Option A: projection helper stays internal ---------------------
class PublicApiSurfaceTests(unittest.TestCase):
    """serialize_state is the public persistence pipeline; the raw
    projection is internal (it requires a pre-validated state and fails
    untyped on lookalikes if misused — which is why it leaves the
    package API rather than duplicating validation)."""

    def test_projection_not_public(self) -> None:
        import quota_sentinel.state as pkg
        import quota_sentinel.state.schema as schema_module

        self.assertFalse(hasattr(pkg, "state_to_document"))
        self.assertNotIn("state_to_document", pkg.__all__)
        # still reachable as the schema module's internal step:
        self.assertTrue(hasattr(schema_module, "state_to_document"))
        # the safe public pipeline remains:
        for name in ("serialize_state", "validate_state",
                     "deserialize_state", "document_to_state"):
            self.assertTrue(hasattr(pkg, name), name)
            self.assertIn(name, pkg.__all__)

    def test_public_serialize_keeps_typed_boundary(self) -> None:
        # The public path's guarantee is unchanged by the narrowing:
        from quota_sentinel.state import ProviderState as PS
        from quota_sentinel.state import SchemaError, serialize_state
        with self.assertRaises(SchemaError):
            serialize_state(
                PS(**{**PS().__dict__,
                      "reset_candidate": {"reset_at": 1, "observed_at": 2}}),
                "codex",
            )


# ---- ARCHITECTURE.md contract phrases must survive doc edits -----------------
class ArchitectureDocGuardTests(unittest.TestCase):
    """The Phase 3A contract lives in ARCHITECTURE.md. These guards fail
    if a doc edit drops one of the load-bearing sentences; each asserted
    string is deliberately a CONTRACT, not prose style."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.arch = (
            Path(__file__).resolve().parent.parent / "ARCHITECTURE.md"
        ).read_text(encoding="utf-8")

    def test_source_of_truth_and_lock_precondition(self) -> None:
        self.assertIn("Source of truth is absolute", self.arch)
        self.assertIn("a lock file existing does not imply the caller owns it",
                      self.arch)

    def test_prepared_is_not_authoritative(self) -> None:
        self.assertIn("Phase 3A PREPARED is NOT JSON AUTHORITATIVE",
                      self.arch)
        self.assertIn(
            "Phase 3A only prepares a semantically current JSON document",
            self.arch,
        )

    def test_validation_pipeline_named(self) -> None:
        self.assertIn(
            "validate_state -> state_to_document -> validate_document "
            "-> deterministic UTF-8 bytes", self.arch,
        )

    def test_load_failure_policy_named(self) -> None:
        for phrase in ("MissingStateDocumentError", "DocumentCorruptError",
                       "SchemaError", "FAIL CLOSED on state mutation"):
            self.assertIn(phrase, self.arch)

    def test_durability_scope_words_carefully(self) -> None:
        self.assertIn(
            "Atomic-visibility guarantees cover process crash and "
            "concurrent readers", self.arch,
        )
        self.assertIn("Power-loss durability", self.arch)
        self.assertIn("NOT claimed and NOT implemented", self.arch)

    def test_phase3b_questions_present(self) -> None:
        self.assertIn("Phase 3B ownership-switch design questions",
                      self.arch)
        self.assertIn("double-truth risk", self.arch)

    def test_migrate_vs_prepare_contradiction_table(self) -> None:
        self.assertIn("the exact opposite of Phase 2 `migrate_provider`",
                      self.arch)

    # Phase 3A residual contract (R3): mutation-not-operation, and the
    # branch-4 failure truth must be pinned; the disproven blanket claim
    # must not return.
    def test_failure_contract_branches_pinned(self) -> None:
        self.assertIn(
            "the whole preflight completes before the first filesystem "
            "mutation", self.arch,
        )
        self.assertIn(
            "failure AFTER a successful publish (verification mismatch)",
            self.arch,
        )
        self.assertIn(
            "the newly-published complete document", self.arch,
        )
        self.assertIn(
            "rollback of authoritative ownership is unnecessary",
            self.arch,
        )
        # The old blanket (false) claim must be gone from normative text:
        self.assertNotIn("shadow old-complete-or-absent", self.arch)
        self.assertNotIn("On ANY failure (domain", self.arch)


if __name__ == "__main__":
    unittest.main()
