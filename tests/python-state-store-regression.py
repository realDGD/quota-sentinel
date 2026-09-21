#!/usr/bin/env python3
"""Behavioural tests for quota_sentinel.state (ProviderState + FileStateStore).

The store must behave slot-for-slot like the shell getters/writers over the
same files (that agreement is proven end-to-end by
tests/state-store-parity-regression.zsh). Locked down here:

* load purity: no dir creation, no repair, unparsable reads as unset;
* transient reset_candidate semantics (None == definitely no candidate);
* commit = optimistic-concurrency guard + only-changed-slot publish;
* commit crash prefixes stay at-least-once directional
  (next_due_at is published last, so any partial commit leaves the
  provider looking due-again, never silently done).
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

        For EVERY crash point between slot publishes, the on-disk residue
        must still demand work: either retry_pending is still 1, or
        next_due_at is still the old matured (past) value. Silently-done
        residue (pending 0 AND future deadline) may only exist after the
        complete commit.
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

        # Crash points are strictly BETWEEN publishes: k runs over the first
        # changed_count-1 prefixes. A crash after the last publish would be
        # a completed commit, verified separately below.
        for k in range(changed_count - 1):
            with self.subTest(crash_after_slot=k):
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
                    f"crash after slot {k} silently lost the due task",
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


if __name__ == "__main__":
    unittest.main()
