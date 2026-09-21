#!/usr/bin/env python3
"""Behavioural tests for the local task orchestrator.

The shell remains the scheduling-policy adapter. These tests lock down only
the orchestration contract that replaces the two legacy scheduling LaunchAgents:
startup check, quarter-hour watchdog, precise deadline wake, sleep recovery,
backoff, SQLite history, and externally-triggered /usage recording.
"""

from __future__ import annotations

import os
import plistlib
import signal
import sqlite3
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from task_orchestrator import (
    CHECK_COMMAND_TIMEOUT_SECONDS,
    CommandResult,
    ScheduleState,
    SubprocessRunner,
    TaskOrchestrator,
    TaskStore,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


class TrackedConnection(sqlite3.Connection):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.result = CommandResult(exit_code=0, timed_out=False, elapsed=0.25)

    def run(self, args: tuple[str, ...], timeout: float) -> CommandResult:
        self.calls.append((args, timeout))
        return self.result

    def cancel(self) -> None:
        return None


class FailOnceRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.recovered = threading.Event()

    def run(self, args: tuple[str, ...], timeout: float) -> CommandResult:
        self.calls.append((args, timeout))
        if len(self.calls) == 1:
            raise OSError("synthetic process launch failure")
        self.recovered.set()
        return self.result


class TaskOrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.clock = FakeClock(100.0)
        self.runner = FakeRunner()
        self.store = TaskStore(self.state_dir / "tasks.sqlite3")
        self.state = ScheduleState(self.state_dir)
        self.engine = TaskOrchestrator(
            script_path=Path("/example/quota-sentinel.sh"),
            schedule_state=self.state,
            store=self.store,
            runner=self.runner,
            clock=self.clock,
            watchdog_interval=900,
            deadline_backoff=60,
            external_recheck=60,
        )

    def tearDown(self) -> None:
        self.engine.stop()
        self.temp.cleanup()

    def write_due(self, provider: str, epoch: int) -> None:
        (self.state_dir / f"{provider}-next-due-at").write_text(f"{epoch}\n")

    def write_pending(self, provider: str, value: int) -> None:
        (self.state_dir / f"{provider}-retry-pending").write_text(f"{value}\n")

    def test_startup_and_quarter_hour_watchdog_preserve_launchd_cadence(self) -> None:
        self.engine.run_startup()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(
            self.runner.calls[0][0],
            ("/bin/zsh", "/example/quota-sentinel.sh", "check"),
        )

        self.clock.value = 899
        self.assertFalse(self.engine.run_ready_once())
        self.clock.value = 900
        self.assertTrue(self.engine.run_ready_once())
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.store.recent_runs(1)[0]["trigger"], "watchdog")

    def test_earliest_provider_deadline_triggers_one_check(self) -> None:
        self.write_due("codex", 500)
        self.write_due("antigravity", 800)
        self.engine.run_startup()
        self.runner.calls.clear()

        self.clock.value = 499
        self.assertFalse(self.engine.run_ready_once())
        self.clock.value = 500
        self.assertTrue(self.engine.run_ready_once())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.store.recent_runs(1)[0]["trigger"], "deadline")

    def test_opencode_deadline_triggers_one_check(self) -> None:
        self.write_due("codex", 800)
        self.write_due("opencode", 500)
        self.engine.run_startup()
        self.runner.calls.clear()

        self.clock.value = 499
        self.assertFalse(self.engine.run_ready_once())
        self.clock.value = 500
        self.assertTrue(self.engine.run_ready_once())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.store.recent_runs(1)[0]["trigger"], "deadline")

    def test_opencode_pending_debt_is_not_a_precision_deadline(self) -> None:
        self.write_due("opencode", 150)
        self.write_pending("opencode", 1)

        self.assertEqual(self.state.snapshot()["opencode"], 150)
        self.assertIsNone(self.state.next_due())

    def test_deadline_and_watchdog_at_same_instant_coalesce(self) -> None:
        self.write_due("codex", 900)
        self.engine.run_startup()
        self.runner.calls.clear()

        self.clock.value = 900
        self.assertTrue(self.engine.run_ready_once())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(
            self.store.recent_runs(1)[0]["trigger"], "deadline+watchdog"
        )

    def test_sleep_recovery_runs_an_overdue_deadline_immediately(self) -> None:
        self.write_due("antigravity", 500)
        self.engine.run_startup()
        self.runner.calls.clear()

        self.clock.value = 2_000
        self.assertTrue(self.engine.run_ready_once())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.store.recent_runs(1)[0]["scheduled_for"], 500)

    def test_past_deadline_uses_existing_60_second_retry_backoff(self) -> None:
        self.write_due("codex", 500)
        self.engine.run_startup()
        self.runner.calls.clear()

        self.clock.value = 1_000
        self.assertTrue(self.engine.run_ready_once())
        self.clock.value = 1_001
        self.assertFalse(self.engine.run_ready_once())
        self.clock.value = 1_060
        self.assertTrue(self.engine.run_ready_once())
        self.assertEqual(len(self.runner.calls), 2)

    def test_next_wake_keeps_external_state_recheck_compatibility(self) -> None:
        self.write_due("codex", 500)
        self.engine.run_startup()
        self.clock.value = 200
        self.assertEqual(self.engine.next_wake_at(), 260)

        self.write_due("codex", 230)
        self.assertEqual(self.engine.next_wake_at(), 230)

    def test_pending_provider_is_not_a_precision_deadline(self) -> None:
        self.write_due("codex", 150)
        self.write_pending("codex", 1)
        self.write_due("antigravity", 800)

        # History retains the raw state, while the precision timer ignores the
        # stale overdue deadline until the watchdog repays the pending debt.
        self.assertEqual(self.state.snapshot()["codex"], 150)
        self.assertEqual(self.state.next_due(), 800)

        self.write_pending("antigravity", 1)
        self.assertIsNone(self.state.next_due())

    def test_nonzero_check_is_recorded_without_crashing_engine(self) -> None:
        self.runner.result = CommandResult(
            exit_code=7, timed_out=False, elapsed=1.5
        )
        self.engine.run_startup()
        row = self.store.recent_runs(1)[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["exit_code"], 7)

    def test_external_usage_action_is_recorded_and_returns_value(self) -> None:
        result = self.engine.run_external_task(
            "usage", "feishu:msg-123", lambda: "sent"
        )
        self.assertEqual(result, "sent")
        row = self.store.recent_runs(1)[0]
        self.assertEqual(row["task_name"], "usage")
        self.assertEqual(row["trigger"], "feishu:msg-123")
        self.assertEqual(row["status"], "succeeded")

    def test_abandoned_running_rows_are_recovered_on_store_open(self) -> None:
        run_id = self.store.begin_run("check", "test", 100, 100)
        self.assertGreater(run_id, 0)
        recovered_store = TaskStore(self.state_dir / "tasks.sqlite3")
        row = recovered_store.recent_runs(1)[0]
        self.assertEqual(row["status"], "interrupted")

    def test_background_loop_recovers_instead_of_silently_stopping(self) -> None:
        runner = FailOnceRunner()
        engine = TaskOrchestrator(
            script_path=Path("/example/quota-sentinel.sh"),
            schedule_state=self.state,
            store=self.store,
            runner=runner,
            clock=self.clock,
            loop_error_backoff=0.01,
        )
        try:
            engine.start()
            self.assertTrue(runner.recovered.wait(timeout=1))
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(engine._thread is not None and engine._thread.is_alive())
        finally:
            engine.stop()


class TaskStoreConnectionLifetimeTest(unittest.TestCase):
    def test_every_short_transaction_closes_its_connection(self) -> None:
        real_connect = sqlite3.connect
        connections: list[TrackedConnection] = []

        def tracked_connect(*args: object, **kwargs: object) -> TrackedConnection:
            kwargs["factory"] = TrackedConnection
            connection = real_connect(*args, **kwargs)
            assert isinstance(connection, TrackedConnection)
            connections.append(connection)
            return connection

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            try:
                with patch("task_orchestrator.sqlite3.connect", side_effect=tracked_connect):
                    store = TaskStore(db_path)
                    run_id = store.begin_run("check", "test", 100, 100)
                    store.finish_run(run_id, "succeeded", 101, exit_code=0)
                    store.record_snapshot(run_id, {"codex": 500}, 101)
                    self.assertEqual(len(store.recent_runs(1)), 1)

                self.assertGreater(len(connections), 0)
                self.assertTrue(
                    all(connection.was_closed for connection in connections),
                    "TaskStore left SQLite connections open after their transaction",
                )
            finally:
                for connection in connections:
                    if not connection.was_closed:
                        connection.close()


class SubprocessRunnerLifetimeTest(unittest.TestCase):
    def test_outer_timeout_kills_detached_nested_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pid_file = root / "nested.pid"
            child_code = (
                "import os,pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(30)"
            )
            parent = root / "parent.py"
            parent.write_text(
                textwrap.dedent(
                    f"""\
                    import subprocess, sys
                    subprocess.run([
                        sys.executable,
                        {str(REPO_ROOT / 'run_with_timeout.py')!r},
                        "--timeout", "30", "--kill-grace", "1", "--",
                        sys.executable, "-c", {child_code!r}, {str(pid_file)!r},
                    ])
                    """
                )
            )

            runner = SubprocessRunner()
            result = runner.run((sys.executable, str(parent)), timeout=1)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)
            self.assertTrue(pid_file.exists(), "nested child never started")
            child_pid = int(pid_file.read_text().strip())

            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("detached nested process survived the outer timeout")
            finally:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class LaunchAgentConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_root = Path(__file__).resolve().parent.parent

    def load_plist(self, filename: str) -> dict[str, object]:
        with (self.project_root / filename).open("rb") as handle:
            return plistlib.load(handle)

    def test_listener_hosts_the_task_orchestrator(self) -> None:
        job = self.load_plist("com.example.quota-sentinel.feishu-listener.plist")
        environment = job.get("EnvironmentVariables", {})
        self.assertIsInstance(environment, dict)
        self.assertEqual(environment.get("QUOTA_SENTINEL_ORCHESTRATOR_ENABLED"), "1")
        self.assertTrue(job.get("RunAtLoad"))
        self.assertTrue(job.get("KeepAlive"))
        self.assertEqual(job.get("Umask"), 0o077)

    def test_outer_check_timeout_covers_the_legal_retry_path(self) -> None:
        self.assertGreaterEqual(CHECK_COMMAND_TIMEOUT_SECONDS, 2_000)

    def test_legacy_watchdog_and_timer_are_disabled_rollback_artifacts(self) -> None:
        watchdog = self.load_plist("com.example.quota-sentinel.plist")
        timer = self.load_plist("com.example.quota-sentinel.timer.plist")

        self.assertTrue(watchdog.get("Disabled"))
        self.assertFalse(watchdog.get("RunAtLoad"))
        self.assertTrue(timer.get("Disabled"))
        self.assertFalse(timer.get("RunAtLoad"))
        self.assertFalse(timer.get("KeepAlive"))


if __name__ == "__main__":
    unittest.main()
