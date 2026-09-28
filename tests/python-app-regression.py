"""Black-box coordinator contracts with fake external processes."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from unittest import mock

from quota_sentinel import app as app_module
from quota_sentinel.app import AppConfig, Application
from quota_sentinel.quota.adapters import PROVIDERS
from quota_sentinel.quota.models import ProviderQuota, QuotaWindow
from quota_sentinel.runtime import factory as factory_module
from quota_sentinel.runtime.locks import LockError, acquire_quota_lock
from quota_sentinel.state import FileStateStore, ProviderState, bootstrap_legacy_authority
from quota_sentinel.state.runlock import RunLockBusyError


@dataclass(frozen=True)
class Reading:
    quota: Optional[ProviderQuota] = None
    fresh: bool = False


class FakeRunner:
    def __init__(self, state_dir: Path, succeed: bool = True):
        self.state_dir = state_dir
        self.succeed = succeed
        self.calls = []

    def prepare(self, provider, workspace):
        return None

    def run(self, provider, workspace, phase, attempt, limit):
        # Debt must exist before any external model process is started.
        self.calls.append((provider, phase, attempt, limit))
        state = FileStateStore(self.state_dir).load(provider)
        assert state.retry_pending
        return type("Result", (), {
            "success": self.succeed,
            "exit_code": 0 if self.succeed else 1,
            "timed_out": False,
            "quota_path": workspace / f"{provider}-quota.json",
        })()


class FakeCollector:
    def __init__(self, readings: Dict[str, Reading]):
        self.readings = readings
        self.collect_calls = 0
        self.saved = []

    def collect(self, pi_raw=None):
        self.collect_calls += 1
        return dict(self.readings)

    def save_pi_snapshots(self, pi_raw):
        self.saved.append(dict(pi_raw))


class FakeNotifier:
    def __init__(self):
        self.events = []

    def validate_ready(self):
        """Push credentials are checked before any model attempt."""

    def task(self, providers, results, readings, now):
        self.events.append(("task", tuple(providers), dict(results)))

    def usage(self, readings, now):
        self.events.append(("usage",))

    def busy(self, now):
        self.events.append(("busy",))


def no_readings():
    return {provider: Reading() for provider in PROVIDERS}


def fresh_reading(reset_at: int) -> Reading:
    quota = ProviderQuota(
        source="Native · test", fresh=True, cached=False, captured_at=1000,
        five_hour=QuotaWindow(85, reset_at),
        weekly=QuotaWindow(75, reset_at + 86400),
    )
    return Reading(quota, True)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"
        bootstrap_legacy_authority(self.state_dir)
        self.runner = FakeRunner(self.state_dir)
        self.collector = FakeCollector(no_readings())
        self.notifier = FakeNotifier()
        self.app = Application(
            self.state_dir, self.runner, lambda workspace: self.collector,
            self.notifier, clock=lambda: 1000, sleep=lambda seconds: None,
            config=AppConfig(initial_attempts=1, watchdog_attempts=1, quota_wait=0),
            workspace_parent=Path(self.temp.name),
        )

    def test_check_with_future_deadlines_only_collects_quota(self):
        store = FileStateStore(self.state_dir)
        for provider in PROVIDERS:
            store.commit(provider, ProviderState(), ProviderState(next_due_at=2000))
        self.app.check()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.collector.collect_calls, 1)
        self.assertEqual(self.notifier.events, [])

    def _probe_only_app(self, providers):
        """The same application, with one provider's model trigger switched off."""
        return Application(
            self.state_dir, self.runner, lambda workspace: self.collector,
            self.notifier, clock=lambda: 1000, sleep=lambda seconds: None,
            config=AppConfig(
                initial_attempts=1, watchdog_attempts=1, quota_wait=0,
                probe_only=frozenset(providers),
            ),
            workspace_parent=Path(self.temp.name),
        )

    def test_probe_only_provider_is_probed_but_never_run(self):
        """The switch costs the model turn, not the measurement or the card."""
        store = FileStateStore(self.state_dir)
        for provider in PROVIDERS:
            future = 2000 if provider != "antigravity" else 1
            store.commit(provider, ProviderState(), ProviderState(next_due_at=future))
        self.collector.readings = no_readings()
        self.collector.readings["antigravity"] = fresh_reading(5000)

        self._probe_only_app(["antigravity"]).check()

        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.collector.collect_calls, 1)
        state = FileStateStore(self.state_dir).load("antigravity")
        # Exactly the deadline a run would have produced from that reading.
        self.assertEqual(state.next_due_at, 5000 + 240)
        self.assertFalse(state.retry_pending)
        self.assertEqual(self.notifier.events, [])

    def test_probe_only_clears_debt_it_could_never_repay(self):
        """A disabled provider never enters a watchdog retry burst."""
        store = FileStateStore(self.state_dir)
        for provider in PROVIDERS:
            if provider == "antigravity":
                store.commit(
                    provider, ProviderState(),
                    ProviderState(next_due_at=1, retry_pending=True),
                )
            else:
                store.commit(
                    provider, ProviderState(), ProviderState(next_due_at=2000)
                )
        self.collector.readings = no_readings()

        self._probe_only_app(["antigravity"]).check()

        self.assertEqual(self.runner.calls, [])
        state = FileStateStore(self.state_dir).load("antigravity")
        self.assertFalse(state.retry_pending)
        # No fresh reset available: the deadline advances a whole run interval,
        # so the timer can never spin on a permanently matured deadline.
        self.assertEqual(state.next_due_at, 1000 + 18060)

    def test_probe_only_switch_is_env_driven_and_rejects_typos(self):
        from quota_sentinel.app import AppConfig as _AppConfig
        self.assertEqual(
            set(_AppConfig.from_env({"QUOTA_SENTINEL_PROBE_ONLY": "antigravity"}).probe_only),
            {"antigravity"},
        )
        self.assertEqual(set(_AppConfig.from_env({}).probe_only), set())
        with self.assertRaises(ValueError):
            _AppConfig.from_env({"QUOTA_SENTINEL_PROBE_ONLY": "antigravty"})

    def test_probe_only_provider_is_refused_by_an_explicit_run(self):
        """`run <provider>` on a switched-off trigger is a refusal.

        The switch promises that the provider is never executed. Skipping it
        silently would report a run that delivered nothing as a success, so the
        explicit target — and only the explicit target — is refused before the
        run lock, the probe or the first attempt.
        """
        with self.assertRaises(ValueError):
            self._probe_only_app(["antigravity"]).run(("antigravity",))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.notifier.events, [])

    def test_probe_only_provider_leaves_the_default_roster_but_the_rest_runs(self):
        """The default roster is what can be delivered; nothing else changes."""
        results = self._probe_only_app(["antigravity"]).run()
        expected = tuple(p for p in PROVIDERS if p != "antigravity")
        self.assertEqual(tuple(sorted(results)), tuple(sorted(expected)))
        self.assertTrue(all(call[0] != "antigravity" for call in self.runner.calls))
        # The card reports the providers that actually ran, so a switched-off
        # provider never appears in a delivery nobody made.
        self.assertEqual(self.notifier.events[0][:2], ("task", expected))

    def test_due_run_records_debt_before_model_and_commits_success(self):
        self.app.run(("codex",))
        self.assertEqual(self.runner.calls, [("codex", "initial", 1, 1)])
        state = FileStateStore(self.state_dir).load("codex")
        self.assertFalse(state.retry_pending)
        self.assertEqual(state.last_task_at, 1000)
        self.assertEqual(state.next_due_at, 1000 + 18060)
        self.assertEqual(self.notifier.events[0][:2], ("task", ("codex",)))

    def test_failed_run_keeps_retry_debt_and_does_not_commit_success(self):
        self.runner.succeed = False
        self.app.run(("opencode",))
        state = FileStateStore(self.state_dir).load("opencode")
        self.assertTrue(state.retry_pending)
        self.assertIsNone(state.last_task_at)
        self.assertEqual(self.notifier.events[0][2]["opencode"], "发送失败")

    def test_missing_authority_refuses_before_model_execution(self):
        (self.state_dir / "backend-authority.json").unlink()
        with self.assertRaises(Exception):
            self.app.run(("codex",))
        self.assertEqual(self.runner.calls, [])

    def test_post_run_fresh_quota_replaces_fallback_and_records_window(self):
        self.collector.readings["codex"] = fresh_reading(2000)
        self.app.run(("codex",))
        state = FileStateStore(self.state_dir).load("codex")
        self.assertEqual(state.last_triggered_window, "2000")
        self.assertEqual(state.next_due_at, 2240)

    def test_usage_busy_reply_probes_nothing(self):
        with acquire_quota_lock(self.state_dir, timeout=0):
            self.app.usage()
        self.assertEqual(self.collector.collect_calls, 0)
        self.assertEqual(self.notifier.events, [("busy",)])

    def test_usage_fresh_calibrates_but_stale_does_not(self):
        store = FileStateStore(self.state_dir)
        store.commit("codex", ProviderState(), ProviderState(next_due_at=3000))
        self.collector.readings["codex"] = fresh_reading(2000)
        self.collector.readings["antigravity"] = Reading(
            fresh_reading(2000).quota, False
        )
        self.app.usage()
        self.assertEqual(store.load("codex").next_due_at, 2240)
        self.assertIsNone(store.load("antigravity").last_known_reset)
        self.assertEqual(self.notifier.events, [("usage",)])

    def test_usage_sends_card_when_run_lock_is_busy(self):
        before = FileStateStore(self.state_dir).load("codex")
        with mock.patch.object(
            app_module, "acquire_run_lock", side_effect=RunLockBusyError("held")
        ):
            self.app.usage()
        self.assertEqual(self.collector.collect_calls, 1)
        self.assertEqual(self.notifier.events, [("usage",)])
        self.assertEqual(FileStateStore(self.state_dir).load("codex"), before)

    def test_preflight_receives_only_the_selected_providers(self):
        checked = []
        self.app.preflight = lambda providers: checked.append(tuple(providers))
        self.app.run(("codex",))
        self.assertEqual(checked, [("codex",)])

    def test_factory_preflight_checks_only_the_run_roster(self):
        app = factory_module.create_application(
            self.state_dir, environment={}, clock=lambda: 1000,
            sleep=lambda seconds: None,
            config=AppConfig(initial_attempts=1, watchdog_attempts=1, quota_wait=0),
        )
        app.model_runner = self.runner
        app.quota_collector_factory = lambda workspace: self.collector
        app.notifier = self.notifier
        app.workspace_parent = Path(self.temp.name)
        with mock.patch.object(factory_module, "readiness_problems", return_value=[]) as check:
            app.run(("codex",))
        check.assert_called_once_with(
            self.state_dir, environment={}, providers=("codex",)
        )

    def test_preparation_failure_does_not_create_unattempted_debt(self):
        def prepare(provider, workspace):
            if provider == "antigravity":
                raise OSError("preparation failed")

        with mock.patch.object(self.runner, "prepare", side_effect=prepare):
            with self.assertRaisesRegex(OSError, "preparation failed"):
                self.app.run(("codex", "antigravity"))
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(FileStateStore(self.state_dir).load("codex").retry_pending)
        self.assertFalse(FileStateStore(self.state_dir).load("antigravity").retry_pending)

    def test_continued_watchdog_failure_is_silent(self):
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=0),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(
                provider, ProviderState(), ProviderState(next_due_at=3000)
            )
        self.runner.succeed = False
        self.app.check()
        self.assertEqual(self.runner.calls, [("codex", "watchdog-retry", 1, 1)])
        self.assertTrue(store.load("codex").retry_pending)
        self.assertEqual(self.notifier.events, [])

    def test_recovered_watchdog_debt_sends_one_card(self):
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=0),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(
                provider, ProviderState(), ProviderState(next_due_at=3000)
            )
        self.app.check()
        self.assertEqual(self.runner.calls, [("codex", "watchdog-retry", 1, 1)])
        self.assertFalse(store.load("codex").retry_pending)
        self.assertEqual(len(self.notifier.events), 1)
        self.assertEqual(self.notifier.events[0][:2], ("task", ("codex",)))

    # ---- Phase C participation: the guard is DEBT, not gap eligibility ----
    def test_pending_debt_within_its_gap_is_not_re_evaluated_as_due(self):
        """A pending debt whose watchdog gap has NOT elapsed is skipped.

        The shell's Phase C evaluates "only providers without a pending
        debt" (quota-sentinel.sh:3260): a provider waiting out its 780s
        watchdog gap is NOT due just because its old deadline matured.
        Deciding it as due would start a fresh three-attempt INITIAL burst on
        every tick from the wait loop, and send a failure card each time —
        turning a long outage into a model-quota storm the shell never had.
        """
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=900, next_due_at=500),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(
                provider, ProviderState(), ProviderState(next_due_at=3000)
            )
        self.app.check()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.notifier.events, [])
        self.assertTrue(store.load("codex").retry_pending)

    def test_gap_eligible_debt_runs_the_watchdog_burst_not_an_initial_one(self):
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=0, next_due_at=500),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(
                provider, ProviderState(), ProviderState(next_due_at=3000)
            )
        self.app.check()
        self.assertEqual(self.runner.calls, [("codex", "watchdog-retry", 1, 1)])

    def test_debt_raised_this_tick_is_not_decided_again_in_the_same_check(self):
        """A provider that just failed its watchdog burst keeps its debt.

        The live read after the burst is what the shell relies on
        (cli.py scheduler-decide-all), so the provider must not be handed to
        the due machine as well.
        """
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=0, next_due_at=500),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(
                provider, ProviderState(), ProviderState(next_due_at=3000)
            )
        self.runner.succeed = False
        self.app.check()
        self.assertEqual(self.runner.calls, [("codex", "watchdog-retry", 1, 1)])

    # ---- Readiness is checked BEFORE model quota is spent ----
    def test_readiness_failure_stops_before_any_model_attempt(self):
        class NotReady(FakeNotifier):
            def validate_ready(self):
                raise RuntimeError("missing Feishu app secret")

        app = Application(
            self.state_dir, self.runner, lambda workspace: self.collector,
            NotReady(), clock=lambda: 1000, sleep=lambda seconds: None,
            config=AppConfig(initial_attempts=1, watchdog_attempts=1, quota_wait=0),
            workspace_parent=Path(self.temp.name),
        )
        with self.assertRaises(RuntimeError):
            app.run(("codex",))
        self.assertEqual(self.runner.calls, [])

    def test_watchdog_retry_also_stops_before_any_model_attempt(self):
        """The retry burst is a burst: it owes the same readiness check.

        A retry runs BEFORE the due evaluation, so it is the one path that
        could reach the model without ever passing the run-requirements gate —
        spending three attempts' worth of quota on a task nobody can receive.
        """
        class NotReady(FakeNotifier):
            def validate_ready(self):
                raise RuntimeError("missing Feishu app secret")

        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=0),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(
                provider, ProviderState(), ProviderState(next_due_at=3000)
            )
        app = Application(
            self.state_dir, self.runner, lambda workspace: self.collector,
            NotReady(), clock=lambda: 1000, sleep=lambda seconds: None,
            config=AppConfig(initial_attempts=1, watchdog_attempts=1, quota_wait=0),
            workspace_parent=Path(self.temp.name),
        )
        with self.assertRaises(RuntimeError):
            app.check()
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(store.load("codex").retry_pending)

    # ---- Lock INFRASTRUCTURE failures degrade exactly like BUSY ----
    def test_check_skips_when_the_quota_lock_cannot_be_acquired_at_all(self):
        store = FileStateStore(self.state_dir)
        store.commit("codex", ProviderState(), ProviderState(next_due_at=500))
        with mock.patch.object(
            app_module, "acquire_quota_lock", side_effect=LockError("no shlock")
        ):
            self.assertEqual(self.app.check(), ())
        self.assertEqual(self.runner.calls, [])
        # Nothing was attempted, so nothing is owed and the deadline stands:
        # the tick is a no-op, exactly like the shell's "quota.lock busy".
        state = store.load("codex")
        self.assertFalse(state.retry_pending)
        self.assertEqual(state.next_due_at, 500)

    def test_usage_still_answers_busy_when_the_lock_is_broken(self):
        with mock.patch.object(
            app_module, "acquire_quota_lock", side_effect=LockError("no shlock")
        ):
            self.app.usage()
        self.assertEqual(self.collector.collect_calls, 0)
        self.assertEqual(self.notifier.events, [("busy",)])

    def test_run_still_notifies_when_the_post_run_lock_is_broken(self):
        with mock.patch.object(
            app_module, "acquire_quota_lock", side_effect=LockError("no shlock")
        ):
            results = self.app.run(("codex",))
        self.assertEqual(results["codex"], "发送成功")
        self.assertEqual(self.notifier.events[0][:2], ("task", ("codex",)))

    def test_a_recovered_retry_is_still_reported_when_the_quota_lock_is_broken(self):
        """The card is owed by the attempt, not by the deadline calibration.

        A watchdog retry that succeeds commits its success immediately, so its
        debt is gone. If the card were dropped here, the only record of that
        success would never exist: no later tick has anything left to report.
        """
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=0),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(
                provider, ProviderState(), ProviderState(next_due_at=3000)
            )
        with mock.patch.object(
            app_module, "acquire_quota_lock", side_effect=LockError("no shlock")
        ):
            self.assertEqual(self.app.check(), ())
        self.assertEqual(self.runner.calls, [("codex", "watchdog-retry", 1, 1)])
        self.assertFalse(store.load("codex").retry_pending)
        self.assertEqual(
            self.notifier.events, [("task", ("codex",), {"codex": "发送成功"})]
        )


class BurstEngineTests(unittest.TestCase):
    """The multi-round burst engine, at the attempt counts production uses.

    The ApplicationTests cases exercise the coordinator with one attempt per
    round; the shell's retry machine is really about what happens ACROSS
    rounds, so these pin the round semantics directly.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"
        bootstrap_legacy_authority(self.state_dir)

    def build(self, outcomes, *, initial=3, watchdog=2, readings=None):
        """outcomes: per-call success flags; readings: fresh quota or none."""
        self.outcomes = list(outcomes)
        calls = []

        class ScriptedRunner:
            def __init__(_self):
                _self.slept = []

            def prepare(_self, provider, workspace):
                return None

            def run(_self, provider, workspace, phase, attempt, limit):
                calls.append((provider, phase, attempt, limit))
                succeed = self.outcomes.pop(0) if self.outcomes else False
                return type("Result", (), {
                    "success": succeed,
                    "exit_code": 0 if succeed else 1,
                    "timed_out": False,
                    "quota_path": workspace / f"{provider}-quota.json",
                })()

        collector = FakeCollector(readings if readings is not None else no_readings())
        notifier = FakeNotifier()
        app = Application(
            self.state_dir, ScriptedRunner(), lambda workspace: collector,
            notifier, clock=lambda: 1000, sleep=lambda seconds: None,
            config=AppConfig(initial_attempts=initial, watchdog_attempts=watchdog,
                             quota_wait=0),
            workspace_parent=Path(self.temp.name),
        )
        return app, calls, notifier

    def test_initial_burst_stops_at_the_first_success(self):
        app, calls, _ = self.build([False, True, True])
        results = app.run(("codex",))
        self.assertEqual([c[2] for c in calls], [1, 2])
        self.assertEqual(results["codex"], "发送成功")
        state = FileStateStore(self.state_dir).load("codex")
        self.assertFalse(state.retry_pending)
        self.assertEqual(state.last_task_at, 1000)

    def test_initial_burst_succeeds_on_the_last_attempt(self):
        app, calls, _ = self.build([False, False, True])
        app.run(("codex",))
        self.assertEqual([c[2] for c in calls], [1, 2, 3])
        state = FileStateStore(self.state_dir).load("codex")
        self.assertFalse(state.retry_pending)
        self.assertEqual(state.last_task_at, 1000)

    def test_exhausted_burst_leaves_debt_and_never_advances_the_deadline(self):
        app, calls, notifier = self.build([False, False, False])
        app.run(("codex",))
        self.assertEqual([c[2] for c in calls], [1, 2, 3])
        state = FileStateStore(self.state_dir).load("codex")
        self.assertTrue(state.retry_pending)
        self.assertIsNone(state.last_task_at)
        self.assertIsNone(state.next_due_at)
        self.assertEqual(notifier.events[0][2]["codex"], "发送失败")

    def test_a_timeout_counts_as_a_failed_attempt_inside_the_burst(self):
        app, calls, _ = self.build([False, False, False])
        app.model_runner.run = lambda *a, **k: type("Result", (), {
            "success": False, "exit_code": 124, "timed_out": True,
            "quota_path": Path(self.temp.name) / "quota.json",
        })()
        app.run(("codex",))
        state = FileStateStore(self.state_dir).load("codex")
        self.assertTrue(state.retry_pending)
        self.assertIsNone(state.last_task_at)

    def test_watchdog_burst_stops_at_the_first_success_and_clears_debt(self):
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=0),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(provider, ProviderState(), ProviderState(next_due_at=4000000000))
        app, calls, notifier = self.build([False, True], watchdog=2)
        app.check()
        self.assertEqual(calls, [("codex", "watchdog-retry", 1, 2),
                                 ("codex", "watchdog-retry", 2, 2)])
        self.assertFalse(store.load("codex").retry_pending)
        self.assertEqual(self.events_of(notifier), [("task", ("codex",))])

    def test_two_pending_providers_are_repaid_in_one_tick(self):
        store = FileStateStore(self.state_dir)
        for provider in ("codex", "antigravity"):
            store.commit(
                provider, ProviderState(),
                ProviderState(retry_pending=True, last_attempt_at=0),
            )
        for provider in (p for p in PROVIDERS if p not in ("codex", "antigravity")):
            store.commit(provider, ProviderState(), ProviderState(next_due_at=4000000000))
        app, calls, _ = self.build([True, True], watchdog=2)
        app.check()
        self.assertEqual(sorted(c[0] for c in calls), ["antigravity", "codex"])
        self.assertEqual(sorted(c[1] for c in calls), ["watchdog-retry", "watchdog-retry"])
        for provider in ("codex", "antigravity"):
            self.assertFalse(store.load(provider).retry_pending)

    def test_one_failing_provider_does_not_cancel_the_others_success(self):
        """Round participants are independent: codex burns all three attempts
        while antigravity succeeds on its first, in the SAME rounds."""
        app, calls, notifier = self.build([], initial=3)

        def scripted(provider, workspace, phase, attempt, limit):
            calls.append((provider, phase, attempt, limit))
            succeed = provider == "antigravity"
            return type("Result", (), {
                "success": succeed, "exit_code": 0 if succeed else 1,
                "timed_out": False,
                "quota_path": workspace / f"{provider}-quota.json",
            })()

        app.model_runner.run = scripted
        results = app.run(("codex", "antigravity"))
        self.assertEqual(results["codex"], "发送失败")
        self.assertEqual(results["antigravity"], "发送成功")
        self.assertEqual(
            sorted((c[0], c[2]) for c in calls),
            [("antigravity", 1), ("codex", 1), ("codex", 2), ("codex", 3)],
        )
        self.assertEqual(notifier.events[0][2]["codex"], "发送失败")
        self.assertEqual(notifier.events[0][2]["antigravity"], "发送成功")

    def test_fresh_quota_while_debt_is_pending_cannot_move_the_deadline(self):
        """Probe data must never push a due-but-unsucceeded task forward.

        The shell's Phase A ordering exists for exactly this: repayment runs
        BEFORE any quota observation can re-anchor a deadline.
        """
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(retry_pending=True, last_attempt_at=900,
                          next_due_at=500, last_known_reset=400),
        )
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(provider, ProviderState(), ProviderState(next_due_at=4000000000))
        app, calls, _ = self.build([], readings={"codex": fresh_reading(2000)})
        app.check()
        state = store.load("codex")
        # The gap is not elapsed, so nothing ran and nothing was re-anchored.
        self.assertEqual(calls, [])
        self.assertEqual(state.next_due_at, 500)
        self.assertEqual(state.last_known_reset, 400)
        self.assertTrue(state.retry_pending)

    @staticmethod
    def events_of(notifier):
        return [event[:2] for event in notifier.events]

    # ---- the operator's run log -----------------------------------------
    def test_a_quiet_tick_still_reports_the_roster_deadlines(self):
        """A healthy tick must be visible in the daily run log.

        The shell logged "check: nothing due (...)" on every quiet tick; a
        port that writes nothing leaves an operator unable to tell a healthy
        scheduler from one that stopped running.
        """
        store = FileStateStore(self.state_dir)
        for provider in PROVIDERS:
            store.commit(provider, ProviderState(),
                         ProviderState(next_due_at=4000000000))
        app, calls, _ = self.build([])
        with self.assertLogs("quota_sentinel.app", level="INFO") as captured:
            app.check()
        text = "\n".join(captured.output)
        self.assertIn("check: nothing due", text)
        for provider in PROVIDERS:
            # The shell printed a readable Shanghai time here, not an epoch.
            self.assertIn("%s next 2096-" % provider, text)

    def test_a_due_tick_names_the_providers_it_is_running(self):
        app, calls, _ = self.build([True], initial=1)
        with self.assertLogs("quota_sentinel.app", level="INFO") as captured:
            app.check()
        text = "\n".join(captured.output)
        self.assertIn("check: due providers: codex", text)

    def test_a_pending_debt_tick_says_so(self):
        store = FileStateStore(self.state_dir)
        store.commit("codex", ProviderState(),
                     ProviderState(retry_pending=True, last_attempt_at=900))
        for provider in (p for p in PROVIDERS if p != "codex"):
            store.commit(provider, ProviderState(),
                         ProviderState(next_due_at=4000000000))
        app, calls, _ = self.build([])
        with self.assertLogs("quota_sentinel.app", level="INFO") as captured:
            app.check()
        self.assertIn("pending (debt unpaid)", "\n".join(captured.output))

    # ---- measuring the window boundary ----------------------------------
    def test_every_check_logs_the_scheduler_reason_per_provider(self):
        """The policy already says WHY a deadline did or did not move.

        Without its own words the window boundary is unmeasurable after the
        fact: you cannot tell whether the provider's reset had already rolled
        when the deadline fired, which is exactly what decides whether the
        four-minute buffer is load-bearing or just delaying us.
        """
        store = FileStateStore(self.state_dir)
        for provider in PROVIDERS:
            store.commit(provider, ProviderState(),
                         ProviderState(next_due_at=4000000000))
        app, calls, _ = self.build([])
        with self.assertLogs("quota_sentinel.app", level="INFO") as captured:
            app.check()
        text = "\n".join(captured.output)
        for provider in PROVIDERS:
            self.assertIn("sched %s: " % provider, text)
        # The policy's own words, not a paraphrase.
        self.assertIn("no valid fresh reset", text)

    def test_a_moved_reset_anchor_is_logged_with_its_delta(self):
        """A moved anchor must be readable as old -> new plus the drift.

        The drift per cycle is what the four-minute buffer costs, so it has
        to be visible without reconstructing it from state files later.
        """
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(last_known_reset=2000, reset_anchor=2000,
                          next_due_at=2240),
        )
        app, _, _ = self.build([], readings={"codex": fresh_reading(2200)})
        with self.assertLogs("quota_sentinel.app", level="INFO") as captured:
            app.usage()
        text = "\n".join(captured.output)
        self.assertIn("reset anchor", text)
        self.assertIn("1970-01-01 08:33:20 CST", text)   # 2000, the old anchor
        self.assertIn("1970-01-01 08:36:40 CST", text)   # 2200, the new one
        self.assertIn("+0h03m20s", text)                 # the 200s drift

    def test_the_check_path_reports_an_anchor_move_too(self):
        """The check path moves the anchor through decide_due, not _sync.

        Both paths must report the movement, or the drift can only be
        reconstructed from one of them.
        """
        app, _, _ = self.build([], readings={"codex": fresh_reading(4000)})
        with self.assertLogs("quota_sentinel.app", level="INFO") as captured:
            app.check()
        text = "\n".join(captured.output)
        self.assertIn("sched codex: fresh reset established generation anchor", text)
        self.assertIn("reset anchor unset -> 1970-01-01 09:06:40 CST", text)
        self.assertIn("(no previous anchor)", text)

    def test_the_boundary_lines_carry_no_paths_or_secrets(self):
        store = FileStateStore(self.state_dir)
        store.commit(
            "codex", ProviderState(),
            ProviderState(last_known_reset=2000, reset_anchor=2000,
                          next_due_at=2240),
        )
        app, _, _ = self.build([], readings={"codex": fresh_reading(2200)})
        with self.assertLogs("quota_sentinel.app", level="INFO") as captured:
            app.usage()
        text = "\n".join(captured.output)
        # No absolute home path may reach the boundary lines. The bare
        # home-directory literal is deliberately absent from this file: AR6b
        # scans committed sources for it, and Path.home() covers the same
        # ground without tripping the guard.
        for forbidden in (str(self.state_dir), str(Path.home()),
                          "Bearer", "sk-"):
            self.assertNotIn(forbidden, text, text)


class AppConfigTests(unittest.TestCase):
    def test_env_overrides_match_the_documented_shell_names(self):
        config = AppConfig.from_env({
            "QUOTA_SENTINEL_RETRY_INTERVAL": "45",
            "QUOTA_SENTINEL_INITIAL_ATTEMPTS": "4",
            "QUOTA_SENTINEL_WATCHDOG_ATTEMPTS": "2",
            "QUOTA_SENTINEL_WATCHDOG_RETRY_GAP": "600",
        })
        self.assertEqual(config.retry_interval, 45)
        self.assertEqual(config.initial_attempts, 4)
        self.assertEqual(config.watchdog_attempts, 2)
        self.assertEqual(config.watchdog_retry_gap, 600)

    def test_non_numeric_values_fall_back_and_zero_is_refused(self):
        # The shell's env_int() falls back on a non-digit; a literal 0 then
        # fails the loud validation instead of spinning without an attempt.
        config = AppConfig.from_env({"QUOTA_SENTINEL_RETRY_INTERVAL": "abc"})
        self.assertEqual(config.retry_interval, 30)
        with self.assertRaises(ValueError):
            AppConfig.from_env({"QUOTA_SENTINEL_WATCHDOG_ATTEMPTS": "0"})


if __name__ == "__main__":
    unittest.main()
