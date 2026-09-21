#!/usr/bin/env python3
"""Behavioural tests for quota_sentinel.state (ProviderState + FileStateStore).

The store must behave like the shell getters/writers over the same files
for every value the project's writers can produce (registered pathological
divergences are enumerated by tests/state-store-parity-regression.zsh).
Locked down here:

* load purity: no dir creation, no repair; a bad slot (unparsable,
  corrupt encoding) unsets ITSELF and never poisons the other seven;
* transient reset_candidate semantics (None == definitely no candidate);
* commit is plan-then-execute: every change is validated AND serialized
  to its final UTF-8 bytes during plan build, so no failure — illegal
  clear, bad type, unencodable text — can occur after the first
  filesystem mutation (T1/T2/R1/R2);
* the old_state check is defensive misuse detection, NOT a CAS
  (test_stale_check_is_defensive_not_cas demonstrates why production
  writers must serialize externally via run.lock);
* persistence validation survives python -O (no assert in store paths)
  and refuses bool-masquerading-as-int epochs;
* state_dir ownership: commit creates/normalizes mode 0700; load never
  does; a no-op commit creates nothing;
* crash prefixes of the SUCCESS transition stay at-least-once
  directional (next_due_at published last). Other transitions are not
  yet migrated and carry no blanket guarantee.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_sentinel.state import (
    FileStateStore,
    ProviderState,
    ResetCandidate,
    StateStoreError,
    StaleStateError,
    initialize_authority,
)


def write_slot(state_dir: Path, provider: str, suffix: str, value: str) -> Path:
    path = state_dir / f"{provider}-{suffix}"
    path.write_text(value + "\n", encoding="utf-8")
    return path


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = FileStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_dir_reads_all_unset(self) -> None:
        store = FileStateStore(self.state_dir / "does-not-exist")
        state = store.load("codex")
        self.assertEqual(
            state,
            ProviderState(
                last_attempt_at=None,
                last_task_at=None,
                next_due_at=None,
                retry_pending=False,
                last_known_reset=None,
                last_triggered_window=None,
                reset_anchor=None,
                reset_candidate=None,
            ),
        )
        # load is pure: it must not have created anything.
        self.assertFalse((self.state_dir / "does-not-exist").exists())

    def test_populated_slots_typed(self) -> None:
        write_slot(self.state_dir, "codex", "last-attempt-at", "100")
        write_slot(self.state_dir, "codex", "last-task-at", "90")
        write_slot(self.state_dir, "codex", "next-due-at", "200")
        write_slot(self.state_dir, "codex", "retry-pending", "1")
        write_slot(self.state_dir, "codex", "last-known-reset-at", "250")
        write_slot(self.state_dir, "codex", "last-triggered-window", "250")
        write_slot(self.state_dir, "codex", "reset-anchor", "250")
        write_slot(self.state_dir, "codex", "reset-candidate", "900:800")
        state = self.store.load("codex")
        self.assertEqual(state.last_attempt_at, 100)
        self.assertEqual(state.next_due_at, 200)
        self.assertTrue(state.retry_pending)
        self.assertEqual(state.reset_candidate, ResetCandidate(900, 800))

    def test_unparsable_epoch_reads_unset_never_repairs(self) -> None:
        path = write_slot(self.state_dir, "codex", "next-due-at", "not-a-number")
        state = self.store.load("codex")
        self.assertIsNone(state.next_due_at)
        # The bad file is still exactly the bad file: no silent rewrite.
        self.assertEqual(path.read_text(encoding="utf-8"), "not-a-number\n")

    def test_retry_pending_variants(self) -> None:
        write_slot(self.state_dir, "codex", "retry-pending", "0")
        self.assertFalse(self.store.load("codex").retry_pending)
        write_slot(self.state_dir, "codex", "retry-pending", "1")
        self.assertTrue(self.store.load("codex").retry_pending)
        write_slot(self.state_dir, "codex", "retry-pending", "banana")
        self.assertFalse(self.store.load("codex").retry_pending)

    def test_candidate_compound_variants(self) -> None:
        write_slot(self.state_dir, "codex", "reset-candidate", "5:6")
        self.assertEqual(self.store.load("codex").reset_candidate, ResetCandidate(5, 6))
        for bad in ("5", "5:", ":6", "a:b", ""):
            write_slot(self.state_dir, "codex", "reset-candidate", bad)
            self.assertIsNone(self.store.load("codex").reset_candidate, bad)

    def test_last_window_is_raw_string(self) -> None:
        write_slot(self.state_dir, "codex", "last-triggered-window", "250")
        self.assertEqual(self.store.load("codex").last_triggered_window, "250")

    def test_invalid_provider_name_rejected(self) -> None:
        for bad in ("../evil", "codex x", "", "a/b"):
            with self.assertRaises(ValueError):
                self.store.load(bad)


class CommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = FileStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_changed_slots_published_mode_600_no_debris(self) -> None:
        old = ProviderState()
        new = ProviderState(
            last_attempt_at=100,
            last_task_at=100,
            next_due_at=500,
            retry_pending=False,
        )
        # load()==old here means "disk already matches old"; start from old
        # by committing from a matching disk (all-absent) state instead:
        old_on_disk = self.store.load("codex")
        self.store.commit("codex", old_on_disk, new)
        loaded = self.store.load("codex")
        self.assertEqual(loaded.next_due_at, 500)
        self.assertEqual(loaded.last_task_at, 100)
        # retry_pending False with no prior file: must NOT create the file
        # (the shell's "absent == 0" reading is preserved).
        self.assertFalse((self.state_dir / "codex-retry-pending").exists())
        self.assertEqual(old, old_on_disk)
        for path in self.state_dir.iterdir():
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            self.assertNotIn(".tmp.", path.name)

    def test_only_changed_slots_are_written(self) -> None:
        base = ProviderState(last_task_at=100, next_due_at=500, retry_pending=True)
        write_slot(self.state_dir, "codex", "last-task-at", "100")
        write_slot(self.state_dir, "codex", "next-due-at", "500")
        write_slot(self.state_dir, "codex", "retry-pending", "1")
        before = {
            path.name: os.stat(path).st_mtime_ns
            for path in self.state_dir.iterdir()
        }
        new = ProviderState(
            last_task_at=100, next_due_at=501, retry_pending=True
        )
        self.store.commit("codex", base, new)
        after = {
            path.name: os.stat(path).st_mtime_ns
            for path in self.state_dir.iterdir()
        }
        changed = {name for name in after if after[name] != before[name]}
        self.assertEqual(changed, {"codex-next-due-at"})

    def test_pending_true_to_false_writes_zero_not_delete(self) -> None:
        old = ProviderState(retry_pending=True)
        write_slot(self.state_dir, "codex", "retry-pending", "1")
        self.store.commit("codex", self.store.load("codex"),
                          ProviderState(retry_pending=False))
        self.assertEqual(
            (self.state_dir / "codex-retry-pending").read_text().strip(), "0"
        )

    def test_transient_slots_delete_on_none(self) -> None:
        write_slot(self.state_dir, "codex", "reset-candidate", "900:800")
        write_slot(self.state_dir, "codex", "reset-anchor", "400")
        old = self.store.load("codex")
        new = ProviderState(
            last_attempt_at=old.last_attempt_at,
            last_task_at=old.last_task_at,
            next_due_at=old.next_due_at,
            retry_pending=old.retry_pending,
            last_known_reset=old.last_known_reset,
            last_triggered_window=old.last_triggered_window,
            reset_anchor=None,
            reset_candidate=None,
        )
        self.store.commit("codex", old, new)
        self.assertFalse((self.state_dir / "codex-reset-candidate").exists())
        self.assertFalse((self.state_dir / "codex-reset-anchor").exists())

    def test_clearing_nontransient_slot_is_rejected(self) -> None:
        write_slot(self.state_dir, "codex", "next-due-at", "500")
        old = self.store.load("codex")
        with self.assertRaises(StateStoreError):
            self.store.commit("codex", old, ProviderState())
        # rejection happens before ANY write (attempt/task unchanged):
        self.assertEqual(
            (self.state_dir / "codex-next-due-at").read_text().strip(), "500"
        )

    def test_stale_commit_is_refused_without_writes(self) -> None:
        write_slot(self.state_dir, "codex", "next-due-at", "500")
        old = self.store.load("codex")
        # someone else moved the disk underneath us:
        write_slot(self.state_dir, "codex", "next-due-at", "999")
        with self.assertRaises(StaleStateError):
            self.store.commit(
                "codex", old, ProviderState(next_due_at=600)
            )
        self.assertEqual(
            (self.state_dir / "codex-next-due-at").read_text().strip(), "999"
        )

    def test_crash_prefixes_stay_at_least_once(self) -> None:
        """Success-commit: debt cleared + deadline pushed forward.

        SCOPE NOTE: this proves the crash-prefix at-least-once property
        for the SUCCESS transition only (the one whose write order the
        canonical SLOTS list mirrors). Other transitions — generation
        init, far-reset promotion, debt creation, candidate lifecycle —
        must be crash-tested individually before being moved behind
        commit(); do not read this test as a global guarantee.

        For EVERY crash point between slot publishes, the on-disk residue
        must still demand work: either retry_pending is still 1, or
        next_due_at is still the old matured (past) value.
        """
        matured = 1_000  # already in the past
        old = ProviderState(
            last_attempt_at=900,
            last_task_at=900,
            next_due_at=matured,
            retry_pending=True,
            last_known_reset=800,
            reset_anchor=800,
            reset_candidate=ResetCandidate(900, 950),
        )
        write_slot(self.state_dir, "codex", "last-attempt-at", "900")
        write_slot(self.state_dir, "codex", "last-task-at", "900")
        write_slot(self.state_dir, "codex", "next-due-at", str(matured))
        write_slot(self.state_dir, "codex", "retry-pending", "1")
        write_slot(self.state_dir, "codex", "last-known-reset-at", "800")
        write_slot(self.state_dir, "codex", "reset-anchor", "800")
        write_slot(self.state_dir, "codex", "reset-candidate", "900:950")
        new = ProviderState(
            last_attempt_at=2_000,
            last_task_at=2_000,
            next_due_at=20_000,
            retry_pending=False,
            last_known_reset=800,
            reset_anchor=None,
            reset_candidate=None,
        )

        changed_count = sum(
            1
            for attribute in [
                "last_attempt_at", "last_task_at", "retry_pending",
                "reset_candidate", "reset_anchor", "next_due_at",
            ]
            if getattr(old, attribute) != getattr(new, attribute)
        )
        self.assertGreater(changed_count, 1)

        # Crash points are strictly BETWEEN publishes. Countdown semantics:
        # iteration k raises after exactly (k + 1) slots were published.
        for k in range(changed_count - 1):
            with self.subTest(crash_after_n_published_slots=k + 1):
                for path in self.state_dir.glob("codex-*"):
                    path.unlink()
                for suffix, value in [
                    ("last-attempt-at", "900"), ("last-task-at", "900"),
                    ("next-due-at", str(matured)), ("retry-pending", "1"),
                    ("last-known-reset-at", "800"), ("reset-anchor", "800"),
                    ("reset-candidate", "900:950"),
                ]:
                    write_slot(self.state_dir, "codex", suffix, value)
                store = FileStateStore(self.state_dir)
                countdown = {"left": k}

                def crash_after(attribute: str) -> None:
                    if countdown["left"] == 0:
                        raise RuntimeError("injected crash")
                    countdown["left"] -= 1

                store.on_slot_committed = crash_after
                with self.assertRaises(RuntimeError):
                    store.commit("codex", store.load("codex"), new)
                partial = store.load("codex")
                work_still_demanded = (
                    partial.retry_pending
                    or partial.next_due_at == matured  # deadline not yet pushed
                )
                self.assertTrue(
                    work_still_demanded,
                    f"crash after {k + 1} publishes silently lost the due task",
                )
        # Full commit then lands cleanly:
        store = FileStateStore(self.state_dir)
        store.commit("codex", store.load("codex"), new)
        final = store.load("codex")
        self.assertEqual(final, new)
        # (no crash-injection, on_slot_committed is None by default)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        # The CLI requires an initialized authority manifest.
        initialize_authority(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *argv: str) -> int:
        from quota_sentinel.__main__ import main
        return main(["--state-dir", str(self.state_dir), *argv])

    def test_next_due_exit_codes(self) -> None:
        import io
        from contextlib import redirect_stdout
        self.assertEqual(self._run("next-due", "codex"), 1)
        write_slot(self.state_dir, "codex", "next-due-at", "777")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run("next-due", "codex"), 0)

    def test_dump_compound_shape(self) -> None:
        write_slot(self.state_dir, "codex", "reset-candidate", "900:100")
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self._run("dump", "codex")
        self.assertIn("reset_candidate=900:100", buffer.getvalue())
        self.assertIn("next_due_at=unset", buffer.getvalue())


# ======================================================================
# Phase 1.1 hardening contracts
# ======================================================================

def disk_signature(state_dir: Path) -> list:
    """(name, bytes, mode) for every file — the 'untouched' yardstick."""
    entries = []
    for path in sorted(state_dir.iterdir()):
        entries.append((path.name, path.read_bytes(), os.stat(path).st_mode & 0o777))
    return entries


class PlanBeforePersistenceTests(unittest.TestCase):
    """T1/T2/T3: no filesystem mutation may happen until the ENTIRE
    mutation plan has been validated and encoded."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = FileStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_full_valid(self) -> ProviderState:
        for suffix, value in [
            ("last-attempt-at", "100"), ("last-task-at", "100"),
            ("next-due-at", "500"), ("retry-pending", "0"),
            ("last-known-reset-at", "400"), ("last-triggered-window", "400"),
            ("reset-anchor", "400"),
        ]:
            write_slot(self.state_dir, "codex", suffix, value)
        return self.store.load("codex")

    def test_t1_invalid_late_slot_aborts_earlier_valid_writes(self) -> None:
        old = self._seed_full_valid()
        before = disk_signature(self.state_dir)
        new = ProviderState(
            last_attempt_at=999,          # valid, would publish FIRST
            last_task_at=old.last_task_at,
            next_due_at=old.next_due_at,
            retry_pending=old.retry_pending,
            last_known_reset=old.last_known_reset,
            last_triggered_window=old.last_triggered_window,
            reset_anchor=old.reset_anchor,
            reset_candidate=None,
        )
        # An ILLEGAL deletion of a persistent slot late in the plan
        # (next_due_at=None is ordered last): must abort pre-mutation.
        new_illegal = ProviderState(
            last_attempt_at=999, last_task_at=old.last_task_at,
            next_due_at=None,               # illegal clear
            retry_pending=old.retry_pending,
            last_known_reset=old.last_known_reset,
            last_triggered_window=old.last_triggered_window,
            reset_anchor=old.reset_anchor, reset_candidate=None,
        )
        with self.assertRaises(StateStoreError):
            self.store.commit("codex", old, new_illegal)
        self.assertEqual(
            disk_signature(self.state_dir), before,
            "illegal late mutation leaked earlier valid writes to disk",
        )

    def test_t2_encoding_failure_zero_mutation(self) -> None:
        old = self._seed_full_valid()
        before = disk_signature(self.state_dir)
        new = ProviderState(
            last_attempt_at=999,           # valid change, canonical FIRST
            last_task_at=old.last_task_at,
            next_due_at=old.next_due_at,
            retry_pending=old.retry_pending,
            last_known_reset=old.last_known_reset,
            last_triggered_window="un\\nrepresentable\nvalue",  # illegal raw
            reset_anchor=old.reset_anchor,
            reset_candidate=None,
        )
        with self.assertRaises(StateStoreError):
            self.store.commit("codex", old, new)
        self.assertEqual(disk_signature(self.state_dir), before)

    def test_r1_late_utf8_failure_leaves_disk_untouched(self) -> None:
        # Lone surrogate: a perfectly valid Python str that passes the raw
        # codec's text-level checks but CANNOT be UTF-8 encoded. If the
        # UTF-8 step ran inside the publish phase, last_attempt_at (valid,
        # canonical-first) would already be on disk when it fails.
        old = self._seed_full_valid()
        before = disk_signature(self.state_dir)
        new = ProviderState(
            last_attempt_at=999,               # valid, planned FIRST
            last_task_at=old.last_task_at,
            next_due_at=old.next_due_at,
            retry_pending=old.retry_pending,
            last_known_reset=old.last_known_reset,
            last_triggered_window="\ud800",    # encodable? no: late failure
            reset_anchor=old.reset_anchor,
            reset_candidate=None,
        )
        with self.assertRaises(StateStoreError) as ctx:
            self.store.commit("codex", old, new)
        # The refusal must be typed + contextual, not a raw UnicodeError:
        message = str(ctx.exception)
        self.assertIn("codex", message)
        self.assertIn("last_triggered_window", message)
        # Zero mutation: even the valid earlier slot never reached disk,
        # and not even a temp file was ever created (signature covers
        # the full dir listing incl. any debris):
        self.assertEqual(disk_signature(self.state_dir), before)
        self.assertEqual(
            (self.state_dir / "codex-last-attempt-at").read_text().strip(), "100"
        )

    def test_r2_unencodable_value_fails_before_first_filesystem_touch(self) -> None:
        # Plan-stage proof: the two primitives the publish stage uses to
        # create/replace slot files (os.open, os.replace) are spied; any
        # call through them while committing an unencodable value is a
        # contract violation. (Slot DELETIONS go through unlink and are
        # out of scope for this spy — no deletion is planned in this
        # transition, and R1's whole-directory signature already covers
        # debris from any primitive.)
        old = self._seed_full_valid()
        new = ProviderState(
            last_attempt_at=999,
            last_task_at=old.last_task_at,
            next_due_at=old.next_due_at,
            retry_pending=old.retry_pending,
            last_known_reset=old.last_known_reset,
            last_triggered_window="\ud800",
            reset_anchor=old.reset_anchor,
            reset_candidate=None,
        )
        fs_touches = []
        real_open = os.open
        real_replace = os.replace

        def spy_open(*args, **kwargs):
            fs_touches.append(("open", args[0]))
            return real_open(*args, **kwargs)

        os.open = spy_open
        os.replace = lambda *a: (fs_touches.append(("replace", a[0])), real_replace(*a))[1]
        try:
            with self.assertRaises(StateStoreError):
                self.store.commit("codex", old, new)
        finally:
            os.open = real_open
            os.replace = real_replace
        # No state file (or temp under any name) was ever opened/replaced:
        offenders = [t for t in fs_touches
                     if isinstance(t[1], (str, Path)) and "codex-" in str(t[1])]
        self.assertEqual(offenders, [])

    def test_t3_bool_can_not_masquerade_as_epoch(self) -> None:
        # type() checks in _encode must reject bool in every int slot,
        # including inside ResetCandidate — even though bool is an int
        # subclass and True >= 0.
        old = self._seed_full_valid()
        before = disk_signature(self.state_dir)
        offenders = [
            ProviderState(**{**old.__dict__, "last_attempt_at": True}),
            ProviderState(**{**old.__dict__, "next_due_at": False}),
            ProviderState(**{**old.__dict__, "reset_anchor": True}),
            ProviderState(**{**old.__dict__, "last_known_reset": False}),
            ProviderState(**{**old.__dict__,
                             "reset_candidate": ResetCandidate(True, 5)}),
            ProviderState(**{**old.__dict__,
                             "reset_candidate": ResetCandidate(5, False)}),
            ProviderState(**{**old.__dict__, "retry_pending": 1}),  # int-as-bool
        ]
        for bad in offenders:
            with self.subTest(state=bad):
                with self.assertRaises(StateStoreError):
                    self.store.commit("codex", old, bad)
        self.assertEqual(disk_signature(self.state_dir), before)

    def test_t4_validation_survives_python_O(self) -> None:
        # Run invalid commits under `python3 -O`, where any assert-based
        # validation would have evaporated. Must still raise AND mutate
        # nothing — including the mixed case whose FIRST slot change is
        # valid while a LATER slot is unencodable (the pre-encode leak:
        # under str-plan builds that would put attempt=999 on disk before
        # failing). The child snapshots the dir around the whole sequence
        # and fails if any byte moved.
        script = (
            "import sys, tempfile, os\n"
            "sys.path.insert(0, %r)\n"
            "from quota_sentinel.state import (FileStateStore, ProviderState, "
            "ResetCandidate, StateStoreError)\n"
            "d = tempfile.mkdtemp()\n"
            "s = FileStateStore(__import__('pathlib').Path(d))\n"
            "open(os.path.join(d, 'codex-next-due-at'), 'w').write('500\\n')\n"
            "open(os.path.join(d, 'codex-retry-pending'), 'w').write('0\\n')\n"
            "old = s.load('codex')\n"
            "bad = [old.__class__(**{**old.__dict__, 'next_due_at': True}),\n"
            "       old.__class__(**{**old.__dict__, 'last_attempt_at': -1}),\n"
            "       old.__class__(**{**old.__dict__, 'last_task_at': 3.5}),\n"
            "       old.__class__(**{**old.__dict__, 'reset_candidate': ResetCandidate(True, 1)}),\n"
            "       old.__class__(**{**old.__dict__, 'retry_pending': 1}),\n"
            "       old.__class__(**{**old.__dict__, 'last_triggered_window': \"\\ud800\"}),\n"
            # Mixed: VALID earlier change (attempt) + unencodable later
            # (window) — the exact late-failure leak shape.
            "       old.__class__(**{**old.__dict__, 'last_attempt_at': 999, "
            "'last_triggered_window': \"\\ud800\"})]\n"
            "def snapshot():\n"
            "    return sorted((name, open(os.path.join(d, name), 'rb').read())\n"
            "                  for name in os.listdir(d))\n"
            "before = snapshot()\n"
            "for candidate in bad:\n"
            "    try:\n"
            "        s.commit('codex', old, candidate)\n"
            "    except StateStoreError:\n"
            "        continue\n"
            "    sys.exit('ACCEPTED under -O: ' + repr(candidate))\n"
            "if snapshot() != before:\n"
            "    sys.exit('MUTATED under -O')\n"
            "print('OK')\n"
            % str(Path(__file__).resolve().parent.parent)
        )
        import subprocess
        result = subprocess.run(
            [sys.executable, "-O", "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "OK")


class CorruptSlotIsolationTests(unittest.TestCase):
    """T5 + §P2-1: one corrupt slot unsets itself, never poisons the load."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = FileStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_invalid_utf8_slot_degrades_independently(self) -> None:
        write_slot(self.state_dir, "codex", "next-due-at", "1756000000")
        write_slot(self.state_dir, "codex", "last-task-at", "123")
        (self.state_dir / "codex-last-known-reset-at").write_bytes(b"\xff\xfe\x00garbage")
        (self.state_dir / "codex-reset-candidate").write_bytes(b"\xc3(\xc3(\xc3(")
        state = self.store.load("codex")
        self.assertIsNone(state.last_known_reset)      # corrupt → unset
        self.assertIsNone(state.reset_candidate)       # corrupt → unset
        self.assertEqual(state.next_due_at, 1756000000)  # siblings intact
        self.assertEqual(state.last_task_at, 123)

    def test_load_is_pure_even_against_corrupt_slots(self) -> None:
        (self.state_dir / "codex-last-attempt-at").write_bytes(b"\xed\xa0\x80")
        self.store.load("codex")
        self.assertEqual(
            (self.state_dir / "codex-last-attempt-at").read_bytes(), b"\xed\xa0\x80"
        )


class StateDirOwnershipTests(unittest.TestCase):
    """T6: the store's write path owns the persistence-dir invariant."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_commit_creates_missing_dir_0700_and_files_0600(self) -> None:
        target = self.root / "nested" / "state"
        store = FileStateStore(target)
        old = store.load("codex")
        store.commit("codex", old, ProviderState(next_due_at=555))
        self.assertTrue(target.is_dir())
        self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o700")
        self.assertEqual(
            oct(os.stat(target / "codex-next-due-at").st_mode & 0o777), "0o600"
        )

    def test_commit_normalizes_loose_existing_dir(self) -> None:
        target = self.root / "loose"
        target.mkdir()
        os.chmod(target, 0o755)
        store = FileStateStore(target)
        store.commit("codex", store.load("codex"), ProviderState(next_due_at=555))
        self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o700")

    def test_noop_commit_creates_nothing(self) -> None:
        target = self.root / "absent"
        store = FileStateStore(target)
        empty = store.load("codex")          # pure read of missing dir
        store.commit("codex", empty, empty)  # empty plan
        self.assertFalse(target.exists())

    def test_unwritable_parent_surfaces_as_state_store_error(self) -> None:
        # A 0500 target dir would self-heal (owner may chmod own dir —
        # that is the ensure step working, not a failure). The genuine
        # unrecoverable case is an unwritable PARENT: mkdir raises
        # PermissionError, which must surface as StateStoreError with
        # context, never as a bare OSError.
        parent = self.root / "locked-parent"
        parent.mkdir()
        os.chmod(parent, 0o500)
        try:
            store = FileStateStore(parent / "child-state")
            store.load("codex")                      # pure read still works
            with self.assertRaises(StateStoreError):
                store.commit("codex", store.load("codex"),
                             ProviderState(next_due_at=555))
        finally:
            os.chmod(parent, 0o700)


class StaleCheckScopeTests(unittest.TestCase):
    """T7: the old_state comparison is defensive detection, NOT a CAS."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = FileStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_matured_debt(self) -> ProviderState:
        for suffix, value in [
            ("last-attempt-at", "900"), ("last-task-at", "900"),
            ("next-due-at", "1000"), ("retry-pending", "1"),
            ("last-known-reset-at", "800"), ("reset-anchor", "800"),
        ]:
            write_slot(self.state_dir, "codex", suffix, value)
        return self.store.load("codex")

    def test_stale_commit_is_refused_before_mutation(self) -> None:
        old = self._seed_matured_debt()
        write_slot(self.state_dir, "codex", "next-due-at", "999")  # moved already
        with self.assertRaises(StaleStateError):
            self.store.commit("codex", old, ProviderState(next_due_at=600))
        self.assertEqual(
            (self.state_dir / "codex-next-due-at").read_text().strip(), "999"
        )

    def test_stale_check_is_defensive_not_cas(self) -> None:
        # An external write AFTER the stale check but BEFORE the final
        # publish is silently overwritten. This is exactly why production
        # writers must serialize on run.lock — the store does not and
        # cannot fix this, and the test pins that as the contract.
        old = self._seed_matured_debt()
        tripped = {"done": False}

        def external_write(_attribute: str) -> None:
            if not tripped["done"]:
                tripped["done"] = True
                write_slot(self.state_dir, "codex", "next-due-at", "2222")

        self.store.on_slot_committed = external_write
        new = ProviderState(
            last_attempt_at=5000, last_task_at=5000, next_due_at=20000,
            retry_pending=False, last_known_reset=800,
            last_triggered_window=None, reset_anchor=None, reset_candidate=None,
        )
        self.store.commit("codex", old, new)   # NOT an error…
        self.assertEqual(self.store.load("codex").next_due_at, 20000)
        # …and the concurrent writer's 2222 was clobbered. Proof: no CAS.


class MalformedValueParityTests(unittest.TestCase):
    """§11: every shell-vs-store behavior on non-canonical (pathological) content is a
    REGISTERED decision (007 is not itself illegal — it is merely non-canonical). Cases marked DIVERGENCE are deliberate strictness
    against content no project writer produces; the shell column records
    what the shell getter does (cross-checked by the zsh parity suite).

    (raw-file-content, python_result, shell_result)
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = FileStateStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _epoch(self, content: str) -> Optional[int]:
        for path in self.state_dir.glob("codex-*"):
            path.unlink()
        write_slot(self.state_dir, "codex", "next-due-at", content)
        return self.store.load("codex").next_due_at

    def test_epoch_table(self) -> None:
        cases = [
            ("", None, "unset"),
            ("garbage", None, "unset"),
            ("42", 42, "42"),
            ("42\n43", None, "unset"),        # embedded newline (2-line file)
            (" 42", None, "unset"),           # leading space
            ("42 ", None, "unset"),           # trailing space
            ("42\r", None, "unset"),          # CRLF residue
            ("+5", None, "unset"),
            ("-5", None, "unset"),
            ("5_0", None, "unset"),
            ("\u0664\u0662", None, "unset"),  # non-ASCII digits (٤٢)
            ("007", 7, "007"),                # DIVERGENCE: python canonicalizes,
                                              # shell echoes raw; numerically equal,
                                              # both pass the shell's own regex reads
            ("42", 42, "42"),
        ]
        for content, expected, shell_note in cases:
            with self.subTest(content=content, shell=shell_note):
                self.assertEqual(self._epoch(content), expected)

    def test_compound_table(self) -> None:
        def compound(content: str) -> Optional[ResetCandidate]:
            for path in self.state_dir.glob("codex-*"):
                path.unlink()
            write_slot(self.state_dir, "codex", "reset-candidate", content)
            return self.store.load("codex").reset_candidate

        self.assertEqual(compound("5:6"), ResetCandidate(5, 6))
        self.assertIsNone(compound("5:6:7"))   # DIVERGENCE: shell slices 5:7; strict here
        self.assertIsNone(compound("5"))
        self.assertIsNone(compound(" 5:6 "))
        self.assertIsNone(compound("5:"))
        self.assertIsNone(compound(":6"))
        self.assertIsNone(compound("a:b"))
        self.assertIsNone(compound(""))

    def test_pending_table(self) -> None:
        def pending(content: str) -> bool:
            for path in self.state_dir.glob("codex-*"):
                path.unlink()
            write_slot(self.state_dir, "codex", "retry-pending", content)
            return self.store.load("codex").retry_pending

        self.assertTrue(pending("1"))
        for bad in ("0", " 1", "1 ", "11", "true", ""):
            with self.subTest(content=bad):
                self.assertFalse(pending(bad))

    def test_raw_window_preserves_content_except_line_breaks(self) -> None:
        for path in self.state_dir.glob("codex-*"):
            path.unlink()
        write_slot(self.state_dir, "codex", "last-triggered-window", "w-2026-08-29")
        self.assertEqual(
            self.store.load("codex").last_triggered_window, "w-2026-08-29"
        )


class ScheduleStateContractSpecTests(unittest.TestCase):
    """The unification this class was written to anticipate has happened:
    task_orchestrator.ScheduleState now reads through the authority router
    instead of parsing legacy slot files itself.

    Two things are pinned here: the aggregate contract that had to SURVIVE
    the unification (it belongs to the orchestrator, not the store), and
    the one parser divergence the unification RESOLVED rather than
    preserved.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        initialize_authority(self.state_dir)
        self.store = FileStateStore(self.state_dir)
        import task_orchestrator
        self.ScheduleState = task_orchestrator.ScheduleState

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_aggregate_contract_must_survive_unification(self) -> None:
        # min over non-pending providers, exclude pending debts, snapshot
        # shape, and the provider roster all belong to the orchestrator
        # layer — NOT to the store (store stays per-provider pure read).
        write_slot(self.state_dir, "codex", "next-due-at", "300")
        write_slot(self.state_dir, "antigravity", "next-due-at", "100")
        write_slot(self.state_dir, "opencode", "next-due-at", "500")
        schedule = self.ScheduleState(self.state_dir)
        self.assertEqual(schedule.next_due(), 100)
        self.assertEqual(schedule.snapshot(),
                         {"codex": 300, "antigravity": 100, "opencode": 500})
        # pending debt excluded from wake math (watchdog repays it instead)
        write_slot(self.state_dir, "antigravity", "retry-pending", "1")
        self.assertEqual(self.ScheduleState(self.state_dir).next_due(), 300)
        # all pending → None
        for p in ("codex", "opencode"):
            write_slot(self.state_dir, p, "retry-pending", "1")
        self.assertIsNone(self.ScheduleState(self.state_dir).next_due())

    def test_parser_divergence_is_resolved_by_the_store(self) -> None:
        # Before unification ScheduleState did int(raw.strip()) and read
        # "5_0" as 50 while the shell's regex and the store read it as
        # unset — two readers, two answers for the same byte. There is now
        # one reader, so the divergence is RESOLVED in favour of the
        # stricter, shell-matching parser: a malformed slot is unset, not a
        # number no producer would ever have written.
        write_slot(self.state_dir, "codex", "next-due-at", "5_0")
        for p in ("antigravity", "opencode"):
            (self.state_dir / f"{p}-next-due-at").write_text("999999999\n")
        self.assertIsNone(
            self.ScheduleState(self.state_dir).snapshot()["codex"]
        )
        self.assertIsNone(self.store.load("codex").next_due_at)
        # The well-formed providers are unaffected...
        self.assertEqual(
            self.ScheduleState(self.state_dir).next_due(), 999999999
        )


if __name__ == "__main__":
    unittest.main()
