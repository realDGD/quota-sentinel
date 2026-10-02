#!/usr/bin/env python3
"""Behavioural tests for the local task orchestrator.

The shell remains the scheduling-policy adapter. These tests lock down only
the orchestration contract that replaces the two legacy scheduling LaunchAgents:
startup check, quarter-hour watchdog, precise deadline wake, sleep recovery,
backoff, SQLite history, and externally-triggered /usage recording.
"""

from __future__ import annotations

import os
import inspect
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_sentinel.app import AppConfig
from quota_sentinel.quota.adapters import PROVIDERS
from quota_sentinel.runtime import agy_exec, codex_exec, models, probe_budget
from quota_sentinel.runtime.models import ModelRunnerConfig
from quota_sentinel.state import bootstrap_legacy_authority
from quota_sentinel.state.migration import DEFAULT_PROVIDERS

from task_orchestrator import (
    CHECK_COMMAND_TIMEOUT_SECONDS,
    CHECK_TIMEOUT_SAFETY_FRACTION,
    PI_PREPARE_BURSTS_PER_CHECK,
    QUOTA_PROBE_PHASES_PER_CHECK,
    CommandResult,
    ScheduleState,
    SubprocessRunner,
    TaskOrchestrator,
    TaskStore,
    check_command_timeout,
    pi_auth_timeout_seconds,
    worst_case_attempt_seconds,
    worst_case_burst_seconds,
    worst_case_check_seconds,
    worst_case_prepare_seconds,
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
        # A real deployment always has an authority manifest. The throwaway
        # one this suite builds is an initialized LEGACY deployment —
        # exactly what the installer leaves on an upgraded host — so the
        # router selects the legacy slot files this suite writes.
        bootstrap_legacy_authority(self.state_dir)
        self.clock = FakeClock(100.0)
        self.runner = FakeRunner()
        self.store = TaskStore(self.state_dir / "tasks.sqlite3")
        self.state = ScheduleState(self.state_dir)
        self.engine = TaskOrchestrator(
            scheduler_command=("/example/python", "-m", "quota_sentinel", "check"),
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

    def test_default_roster_is_the_package_roster(self):
        """The precise-timer roster must not be a frozen local copy.

        This engine runs for days. A roster captured as a literal keeps it
        waking only for the providers that existed when it started: ClinePass
        was added to the package and the live engine, started a day earlier,
        kept firing it on the fifteen-minute grid instead of at its
        reset+240s deadline, which is exactly the drift the buffer exists to
        prevent.
        """
        self.assertEqual(DEFAULT_PROVIDERS, PROVIDERS)
        self.assertEqual(ScheduleState(self.state_dir).providers, PROVIDERS)
        # Pin the DEFAULT itself, not just an instance: TaskOrchestrator()
        # builds `schedule_state or ScheduleState()`, so a literal default is
        # the exact shape this regression exists for.
        self.assertEqual(
            inspect.signature(ScheduleState.__init__).parameters["providers"].default,
            PROVIDERS,
        )

    def test_startup_and_quarter_hour_watchdog_preserve_launchd_cadence(self) -> None:
        self.engine.run_startup()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(
            self.runner.calls[0][0],
            ("/example/python", "-m", "quota_sentinel", "check"),
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

    def test_reads_the_authoritative_backend_after_a_cutover(self) -> None:
        """The listener must follow the authority fact.

        The legacy slot files are a frozen rollback artifact after a
        cutover. Computing wake times from them would leave the listener
        sleeping until a deadline that already moved — or waking for one
        the scheduler has superseded. This is the regression for exactly
        that stale read.
        """
        from quota_sentinel.state import cutover_to_json, write_authority, BackendAuthority, BACKEND_JSON

        self.write_due("codex", 500)
        # The whole roster switches in one epoch, so the deployment stays
        # readable as a unit (a per-provider cutover would leave the other
        # providers without documents, which is itself a loud condition).
        cutover_to_json(self.state_dir)
        self.assertEqual(self.state.next_due(), 500)

        # A legacy write after the flip must be INVISIBLE to the listener.
        self.write_due("codex", 999)
        self.assertEqual(
            self.state.snapshot()["codex"], 500,
            "the listener read the retired legacy backend",
        )
        self.assertEqual(self.state.next_due(), 500)

        # Only a transition through the authoritative backend moves it.
        from quota_sentinel.scheduler import service
        service.commit_success(self.state_dir, "codex", 1000)
        self.assertEqual(self.state.next_due(), 1000 + 18060)

    def test_uninitialized_authority_yields_no_deadline_and_logs(self) -> None:
        """A missing manifest is loud and safe: no deadlines, watchdog
        grid still runs check, and the retired backend is never read."""
        from quota_sentinel.state import AUTHORITY_FILENAME

        self.write_due("codex", 500)
        (self.state_dir / AUTHORITY_FILENAME).unlink()
        with self.assertLogs("task_orchestrator", level="ERROR") as captured:
            self.assertIsNone(self.state.next_due())
        self.assertIsNone(self.state.snapshot()["codex"])
        self.assertTrue(
            any("authoritative backend" in line for line in captured.output)
        )

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
            scheduler_command=("/example/python", "-m", "quota_sentinel", "check"),
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
        template = (self.project_root / (filename + ".template")).read_text()
        rendered = template.replace("__REPO_DIR__", str(self.project_root)).replace("__LOG_DIR__", str(self.project_root / "logs"))
        return plistlib.loads(rendered.encode())

    def test_listener_hosts_the_task_orchestrator(self) -> None:
        job = self.load_plist("quota-sentinel.feishu-listener.plist")
        environment = job.get("EnvironmentVariables", {})
        self.assertIsInstance(environment, dict)
        self.assertEqual(environment.get("QUOTA_SENTINEL_ORCHESTRATOR_ENABLED"), "1")
        self.assertTrue(job.get("RunAtLoad"))
        self.assertTrue(job.get("KeepAlive"))
        self.assertEqual(job.get("Umask"), 0o077)

    def test_outer_check_timeout_covers_the_legal_retry_path(self) -> None:
        # The real invariant, not a magic floor: the shipped default has to sit
        # above the worst case the channels themselves allow, and that worst case
        # has to be above the literal this pin used to compare against — which is
        # exactly what the codex/agy transports broke.
        bound = worst_case_check_seconds()
        self.assertGreater(CHECK_COMMAND_TIMEOUT_SECONDS, bound)
        self.assertGreater(
            bound, 2_100,
            "the retired 2100s literal is no longer above a legal check",
        )

    def test_legacy_watchdog_and_timer_are_disabled_rollback_artifacts(self) -> None:
        watchdog = self.load_plist("quota-sentinel.plist")
        timer = self.load_plist("quota-sentinel.timer.plist")

        self.assertTrue(watchdog.get("Disabled"))
        self.assertFalse(watchdog.get("RunAtLoad"))
        self.assertTrue(timer.get("Disabled"))
        self.assertFalse(timer.get("RunAtLoad"))
        self.assertFalse(timer.get("KeepAlive"))


class CheckTimeoutDerivationTest(unittest.TestCase):
    """The outer `check` bound is DERIVED, not a literal that can go stale.

    History matters here: 2100s was hand-computed for a Pi-only world as
    `2x310 + 3x310 + 2x207 ≈ 1964s`. The codex and agy channels changed the
    per-attempt budget — agy alone may run `AGY_TRANSIENT_RETRIES + 1` turns, a
    guard with the same timeout, and then hand the attempt to Pi — so the old
    literal stopped covering a legal check and would have killed it mid-run.
    These tests pin the two halves of the fix: the bound follows the live
    constants, and the default sits above the bound unless an operator overrides
    it.
    """

    # A budget with no environment in it: `{}` means "defaults only", so a
    # developer's exported QUOTA_SENTINEL_* cannot move these assertions.
    ENV: dict[str, str] = {}

    def test_default_exceeds_the_derived_worst_case(self) -> None:
        bound = worst_case_check_seconds(self.ENV)
        self.assertGreater(bound, 2_100, "the retired literal under-bounds a check")
        # The margin is what makes the default strictly exceed the bound.
        derived = bound * (1.0 + CHECK_TIMEOUT_SAFETY_FRACTION)
        self.assertEqual(check_command_timeout(self.ENV), derived)
        self.assertGreater(check_command_timeout(self.ENV), bound)
        # The module constant is that default unless an operator overrode it in
        # this process's environment; with no override the shipped value must
        # clear the bound.
        if "QUOTA_SENTINEL_CHECK_TIMEOUT" not in os.environ:
            self.assertEqual(CHECK_COMMAND_TIMEOUT_SECONDS, derived)
            self.assertGreater(CHECK_COMMAND_TIMEOUT_SECONDS, bound)

    def test_attempt_bound_covers_the_agy_turns_and_the_pi_fallback(self) -> None:
        """The agy channel is the worst single attempt, and it is read live."""
        turns = agy_exec.AGY_TRANSIENT_RETRIES + 1
        fallback = ModelRunnerConfig.from_env(self.ENV)
        self.assertGreaterEqual(
            worst_case_attempt_seconds(self.ENV),
            turns * agy_exec.AGY_EXEC_TIMEOUT_SECONDS
            + fallback.timeout + fallback.kill_grace,
        )

    def test_codex_primary_attempt_includes_its_pi_credential_refresh(self) -> None:
        env = {
            "QUOTA_SENTINEL_TRANSPORT": "codex=codex",
            "QUOTA_SENTINEL_PI_AUTH_TIMEOUT": "10000",
        }
        # One failed Codex turn (120+10), Pi prepare (10000+10),
        # then its terminal Pi turn (300+10), all inside ONE attempt.
        self.assertGreaterEqual(worst_case_attempt_seconds(env), 10450.0)
        self.assertGreaterEqual(worst_case_check_seconds(env), 5 * 10450.0)

    def test_codex_primary_auth_budget_counts_every_fallback_and_burst(self) -> None:
        env = {
            "QUOTA_SENTINEL_TRANSPORT": "codex=codex",
            "QUOTA_SENTINEL_PI_AUTH_TIMEOUT": "10000",
        }
        longer_auth = dict(env, QUOTA_SENTINEL_PI_AUTH_TIMEOUT="10007")
        limits = AppConfig(initial_attempts=3, watchdog_attempts=2, retry_interval=30)
        delta = worst_case_check_seconds(longer_auth, limits) - worst_case_check_seconds(env, limits)
        # Five possible failed turns refresh Pi before fallback; both bursts
        # also retain their separate conservative prepare allowance.
        self.assertAlmostEqual(delta, 7 * 7.0)

    def test_burst_bound_counts_the_sleeps_between_rounds(self) -> None:
        self.assertEqual(worst_case_burst_seconds(3, 100.0, 30.0), 360.0)
        self.assertEqual(worst_case_burst_seconds(1, 100.0, 30.0), 100.0)

    def test_bound_follows_a_raised_channel_timeout(self) -> None:
        """Raise the agy turn timeout: the bound moves by the turns it bounds.

        The turn count is the initial turn, every transient retry, and the
        agent-listing guard (which runs under the same timeout); the multiplier
        is every attempt round a check can run (watchdog + initial).
        """
        limits = AppConfig()
        rounds = limits.watchdog_attempts + limits.initial_attempts
        turns = agy_exec.AGY_TRANSIENT_RETRIES + 2
        baseline = worst_case_check_seconds(self.ENV)
        raised_to = agy_exec.AGY_EXEC_TIMEOUT_SECONDS + 240
        with patch.object(agy_exec, "AGY_EXEC_TIMEOUT_SECONDS", raised_to):
            raised = worst_case_check_seconds(self.ENV)
        self.assertAlmostEqual(raised - baseline, rounds * turns * 240.0)

    def test_bound_follows_a_raised_retry_count(self) -> None:
        limits = AppConfig()
        rounds = limits.watchdog_attempts + limits.initial_attempts
        turn = agy_exec.AGY_EXEC_TIMEOUT_SECONDS + 10  # timeout + kill grace
        baseline = worst_case_check_seconds(self.ENV)
        with patch.object(
            agy_exec, "AGY_TRANSIENT_RETRIES", agy_exec.AGY_TRANSIENT_RETRIES + 2
        ):
            raised = worst_case_check_seconds(self.ENV)
        self.assertAlmostEqual(raised - baseline, rounds * 2 * turn)

    def test_bound_follows_the_other_channels_and_the_attempt_limits(self) -> None:
        baseline = worst_case_check_seconds(self.ENV)
        with patch.object(codex_exec, "CODEX_EXEC_TIMEOUT_SECONDS", 900):
            codex_raised = worst_case_check_seconds(self.ENV)
        self.assertGreater(codex_raised, baseline)
        # AppConfig's limits are the Application's own, so an operator override
        # of them has to move the bound as well.
        more_rounds = dict(self.ENV, QUOTA_SENTINEL_INITIAL_ATTEMPTS="9")
        self.assertGreater(worst_case_check_seconds(more_rounds), baseline)
        longer_gap = dict(self.ENV, QUOTA_SENTINEL_RETRY_INTERVAL="300")
        self.assertGreater(worst_case_check_seconds(longer_gap), baseline)

    # ---- the probe term: derived from the collector, never a literal -------

    def test_probe_phase_bound_keeps_the_retired_300s_floor(self) -> None:
        """The new derivation can only ever RAISE the bound it replaced.

        300s was the fixed per-phase allowance. It is now a documented floor
        under a bound derived from the collector's own structure, so a phase
        bound that is somehow cheaper than the old literal is a bug, not a
        tightening.
        """
        self.assertEqual(probe_budget.PROBE_PHASE_FLOOR_SECONDS, 300.0)
        bound = probe_budget.worst_case_probe_phase_seconds(self.ENV)
        self.assertGreaterEqual(bound, probe_budget.PROBE_PHASE_FLOOR_SECONDS)
        # On the shipped defaults the floor is a NET, not the answer: if it had
        # to do the work, the structural growth measured below would be hidden
        # by it (a raised budget would move the sum without moving the bound).
        self.assertGreater(bound, probe_budget.PROBE_PHASE_FLOOR_SECONDS)

    def test_bound_follows_a_raised_codexbar_timeout(self) -> None:
        """The exact override that broke the retired fixed allowance.

        `QUOTA_SENTINEL_CODEXBAR_TIMEOUT` configures codex's CodexBar budget,
        and `QuotaCollector._codexbar` walks TWO sources for codex — `cli`
        then `oauth` — so one phase pays the raised budget twice, while a check
        runs two phases. With the old 300s literal this environment left the
        derived bound at 5490s even though one codexbar call could legally run
        for 4000s.
        """
        spec = probe_budget.PROBE_TIMEOUTS_BY_OPTION["codexbar_timeout"]
        calls = probe_budget.codexbar_calls("codex")
        self.assertEqual(calls, 2, "codex tries cli and oauth; recount _codexbar")
        before = probe_budget.quota_probe_timeouts(self.ENV)[spec.option]
        added = (4000.0 - float(before)) * calls       # growth of ONE phase
        env = dict(self.ENV, **{spec.env: "4000"})
        baseline = worst_case_check_seconds(self.ENV)
        raised = worst_case_check_seconds(env)
        delta = raised - baseline
        self.assertGreaterEqual(delta, QUOTA_PROBE_PHASES_PER_CHECK * added)
        self.assertAlmostEqual(delta, QUOTA_PROBE_PHASES_PER_CHECK * added)
        # The default cap follows the bound: the operator's own value must never
        # be capped by a number derived for the DEFAULT budgets.
        self.assertGreater(check_command_timeout(env), raised)

    def test_bound_follows_a_raised_native_timeout(self) -> None:
        """One native helper per provider per phase: +300s moves a phase +300s."""
        spec = probe_budget.PROBE_TIMEOUTS_BY_OPTION["opencode_native_timeout"]
        before = probe_budget.quota_probe_timeouts(self.ENV)[spec.option]
        added = 300.0
        env = dict(self.ENV, **{spec.env: str(float(before) + added)})
        delta = worst_case_check_seconds(env) - worst_case_check_seconds(self.ENV)
        self.assertAlmostEqual(delta, QUOTA_PROBE_PHASES_PER_CHECK * added)

    def test_bound_follows_a_raised_codexbar_kill_grace(self) -> None:
        """The kill grace is paid by EVERY CodexBar call in the phase."""
        spec = probe_budget.PROBE_TIMEOUTS_BY_OPTION["codexbar_kill_grace"]
        before = probe_budget.quota_probe_timeouts(self.ENV)[spec.option]
        added = 7.0
        calls = probe_budget.codexbar_calls_per_phase()
        self.assertEqual(calls, 5, "recount _codexbar sources per provider")
        env = dict(self.ENV, **{spec.env: str(float(before) + added)})
        delta = worst_case_check_seconds(env) - worst_case_check_seconds(self.ENV)
        self.assertAlmostEqual(
            delta, QUOTA_PROBE_PHASES_PER_CHECK * calls * added
        )

    def test_probe_phase_bound_is_monotone_in_every_override(self) -> None:
        """Every budget, raised, can only grow the phase — by at least its cost.

        The multipliers are hand-derived from the collector (and cross-checked
        against `codexbar_calls_per_phase`): a native timeout is paid once per
        phase because one helper serves the provider; a CodexBar timeout once
        per source that provider walks (`cli`+`oauth` for codex, one source for
        the others); the kill grace once per CodexBar call.
        """
        per_phase = {
            "codexbar_timeout": 2,                # codex: cli, then oauth
            "antigravity_codexbar_timeout": 1,    # one cli source
            "opencode_codexbar_timeout": 1,       # one api source
            "clinepass_codexbar_timeout": 1,      # one api source
            "antigravity_native_timeout": 1,      # one uv helper
            "opencode_native_timeout": 1,         # one python helper
            "clinepass_native_timeout": 1,        # one python helper
            "codexbar_kill_grace": 5,             # every CodexBar call
        }
        self.assertEqual(
            sorted(per_phase),
            sorted(spec.option for spec in probe_budget.PROBE_TIMEOUTS),
            "an override exists that this monotonicity test does not cover",
        )
        self.assertEqual(
            per_phase["codexbar_kill_grace"],
            probe_budget.codexbar_calls_per_phase(),
        )
        baseline = probe_budget.worst_case_probe_phase_seconds(self.ENV)
        for option, multiplier in per_phase.items():
            spec = probe_budget.PROBE_TIMEOUTS_BY_OPTION[option]
            before = probe_budget.quota_probe_timeouts(self.ENV)[option]
            added = 60.0
            env = dict(self.ENV, **{spec.env: str(float(before) + added)})
            with self.subTest(override=spec.env):
                raised = probe_budget.worst_case_probe_phase_seconds(env)
                self.assertGreaterEqual(raised, baseline)
                self.assertGreaterEqual(raised - baseline, multiplier * added)

    # ---- the Pi credential refresh: one prepare pass per burst ------------

    def test_pi_prepare_term_moves_the_bound(self) -> None:
        """The bounded credential refresh is paid once per burst, twice a check.

        `Application._burst` prepares every provider before its rounds, and the
        codex prepare asks Pi for a bearer token under `auth_timeout` plus the
        runner's kill grace. `getattr(config, "auth_timeout", None)` seeded from
        `models.PI_AUTH_TIMEOUT_SECONDS` is the seam that works whether or not
        the dataclass field has landed yet, so the test raises the module
        constant — the accessor reads both sources and keeps the larger.
        """
        baseline = worst_case_check_seconds(self.ENV)
        before = pi_auth_timeout_seconds(self.ENV)
        self.assertGreater(before, 0)
        # Keep agy the longest attempt so this isolates the burst prepare term.
        added = 60.0
        raised_seed = before + added
        with patch.object(models, "PI_AUTH_TIMEOUT_SECONDS", raised_seed, create=True):
            after = pi_auth_timeout_seconds(self.ENV)
            raised = worst_case_check_seconds(self.ENV)
            prepared = worst_case_prepare_seconds(self.ENV)
        self.assertAlmostEqual(after - before, added)
        self.assertAlmostEqual(raised - baseline, PI_PREPARE_BURSTS_PER_CHECK * added)
        # The refresh is bounded by the auth budget PLUS the runner's own kill
        # grace, so the prepare term is never just the auth timeout.
        grace = ModelRunnerConfig.from_env(self.ENV).kill_grace
        self.assertAlmostEqual(prepared, after + grace)

    def test_env_override_still_wins(self) -> None:
        self.assertEqual(
            check_command_timeout({"QUOTA_SENTINEL_CHECK_TIMEOUT": "1234"}), 1234.0
        )
        with patch.dict(os.environ, {"QUOTA_SENTINEL_CHECK_TIMEOUT": "1500.5"}):
            self.assertEqual(check_command_timeout(), 1500.5)

    def test_unusable_override_falls_back_to_the_derived_default(self) -> None:
        derived = worst_case_check_seconds(self.ENV) * (
            1.0 + CHECK_TIMEOUT_SAFETY_FRACTION
        )
        for raw in ("", "  ", "abc", "0", "-5", "inf", "nan"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    check_command_timeout({"QUOTA_SENTINEL_CHECK_TIMEOUT": raw}),
                    derived,
                )


if __name__ == "__main__":
    unittest.main()
