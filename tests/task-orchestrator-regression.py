#!/usr/bin/env python3
"""Behavioural tests for the local task orchestrator.

The shell remains the scheduling-policy adapter. These tests lock down only
the orchestration contract that replaces the two legacy scheduling LaunchAgents:
startup check, quarter-hour watchdog, precise deadline wake, sleep recovery,
backoff, SQLite history, and externally-triggered /usage recording.
"""

from __future__ import annotations

import plistlib
import tempfile
import threading
import unittest
from pathlib import Path

from task_orchestrator import (
    CommandResult,
    ScheduleState,
    TaskOrchestrator,
    TaskStore,
)


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
