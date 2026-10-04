"""Public Quota Sentinel operations over the existing Python domain policy."""

from __future__ import annotations

import logging
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Mapping, Optional, Sequence

from quota_sentinel.quota.adapters import PROVIDERS
from quota_sentinel.scheduler import policy, service
from quota_sentinel.scheduler.models import NO_OBSERVATION, Decision, QuotaObservation
from quota_sentinel.state import acquire_run_lock, read_authority
from quota_sentinel.state.runlock import RunLockBusyError, RunLockError
from quota_sentinel.runtime.cards import format_reset_time

from .runtime.locks import LockError, acquire_quota_lock

logger = logging.getLogger("quota_sentinel.app")


@dataclass(frozen=True)
class AppConfig:
    initial_attempts: int = 3
    watchdog_attempts: int = 2
    retry_interval: int = 30
    watchdog_retry_gap: int = 780
    quota_wait: int = 20
    timer_recheck: int = 60
    # Providers whose MODEL TRIGGER is switched off while everything around it
    # keeps running: the quota probe (free, metadata only), the deadline
    # calibration, the run log and the /usage card. Nothing is deleted — the
    # transport, its tests and its roster entry stay in place, so removing the
    # name (or `QUOTA_SENTINEL_PROBE_ONLY`) restores the old behaviour exactly.
    probe_only: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("initial_attempts", "watchdog_attempts", "retry_interval"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("watchdog_retry_gap", "quota_wait", "timer_recheck"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        unknown = sorted(set(self.probe_only) - set(PROVIDERS))
        if unknown:
            # A typo would otherwise look like a working switch that silently
            # keeps spending quota, which is the one failure mode this option
            # exists to prevent.
            raise ValueError(f"unknown probe-only provider(s): {', '.join(unknown)}")

    @classmethod
    def from_env(cls, environment: Optional[Mapping[str, str]] = None) -> "AppConfig":
        """Read the documented overrides, with the shell's exact tolerance.

        A non-numeric value falls back to the default (the shell's
        ``env_int`` does the same), while a literal ``0`` reaches
        ``__post_init__`` and fails loudly — a zero attempt limit or retry
        interval would spin without ever attempting a task.
        """
        env = os.environ if environment is None else environment

        def env_int(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or not raw.strip().isdigit():
                return default
            return int(raw)

        raw_probe_only = env.get("QUOTA_SENTINEL_PROBE_ONLY", "")
        probe_only = frozenset(
            name.strip().lower()
            for name in raw_probe_only.replace(" ", ",").split(",")
            if name.strip()
        )

        return cls(
            initial_attempts=env_int("QUOTA_SENTINEL_INITIAL_ATTEMPTS", 3),
            watchdog_attempts=env_int("QUOTA_SENTINEL_WATCHDOG_ATTEMPTS", 2),
            retry_interval=env_int("QUOTA_SENTINEL_RETRY_INTERVAL", 30),
            watchdog_retry_gap=env_int("QUOTA_SENTINEL_WATCHDOG_RETRY_GAP", 780),
            probe_only=probe_only,
        )


def _observation(reading: object) -> QuotaObservation:
    quota = getattr(reading, "quota", None)
    if quota is None:
        return NO_OBSERVATION
    return QuotaObservation(
        fresh=bool(getattr(reading, "fresh", False) and quota.fresh),
        reset_at=quota.five_hour.reset_at,
        source=quota.source,
    )


def _reset_text(value: Optional[int]) -> str:
    return "unset" if value is None else format_reset_time(value)


def _drift_text(before: Optional[int], after: Optional[int]) -> str:
    """How far the window boundary moved, in one glance: +0h03m20s."""
    if before is None or after is None:
        return "no previous anchor"
    delta = after - before
    sign = "+" if delta >= 0 else "-"
    delta = abs(delta)
    return "%s%dh%02dm%02ds" % (
        sign, delta // 3600, (delta % 3600) // 60, delta % 60,
    )


class Application:
    """Coordinate locks, probes, attempts and notifications.

    The injected external adapters make every process/network operation
    replaceable in tests. State transitions always use the authoritative
    router and the scheduler policy package.
    """

    def __init__(
        self,
        state_dir: Path,
        model_runner: object,
        quota_collector_factory: Callable[[Path], object],
        notifier: object,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        config: AppConfig = AppConfig(),
        workspace_parent: Optional[Path] = None,
        preflight: Optional[Callable[[Sequence[str]], None]] = None,
        runtime_plan=None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.runtime_plan = runtime_plan
        self.activation_path = self.state_dir / "config.activations.json"
        self.active_providers = runtime_plan.active_providers if runtime_plan is not None else PROVIDERS
        self.opening_providers = runtime_plan.opening_providers if runtime_plan is not None else PROVIDERS
        self.model_runner = model_runner
        self.quota_collector_factory = quota_collector_factory
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.config = config
        self.workspace_parent = workspace_parent
        self.preflight = preflight

    def _collect(self, collector, raw=None, *, purpose):
        if self.runtime_plan is None:
            return collector.collect(raw) if raw is not None else collector.collect()
        return collector.collect(raw, purpose=purpose)

    def _write_active_transitions(self, awaiting):
        from quota_sentinel.state.activation import persist_activation
        self._activation_revision = persist_activation(
            self.state_dir, self.opening_providers, awaiting, self.activation_path,
            getattr(self, '_activation_revision', None))

    def _active_transitions(self):
        if self.runtime_plan is None:
            return set()
        path = self.state_dir / 'runtime-providers.json'
        from quota_sentinel.state.activation import read_journal,read_runtime_providers
        journal_pending, self._activation_revision = read_journal(self.activation_path)
        previous=read_runtime_providers(path)
        if previous is None:
            self._write_active_transitions(journal_pending)
            return journal_pending
        awaiting = journal_pending | set(previous['awaiting_resume']) | (set(self.opening_providers) - set(previous['enabled']))
        self._write_active_transitions(awaiting)
        return awaiting

    def _require_ready(self, providers: Sequence[str]) -> None:
        """Refuse to spend model quota on a run that cannot be delivered.

        The default is the notifier's own credential gate, which is the part
        of the shell's ``validate_run_requirements`` this module can know
        about; the CLI injects the full requirements check (binaries, Pi
        auth, provider hooks) through ``preflight``.
        """
        if self.preflight is not None:
            self.preflight(providers)
        else:
            self.notifier.validate_ready()

    def _now(self) -> int:
        return int(self.clock())

    def _providers(self, providers: Sequence[str]) -> tuple[str, ...]:
        names = tuple(providers) if providers else self.opening_providers
        if len(set(names)) != len(names) or any(p not in self.opening_providers for p in names):
            raise ValueError(f"invalid provider roster: {names!r}")
        return names

    def _workspace(self):
        parent = str(self.workspace_parent) if self.workspace_parent else None
        return tempfile.TemporaryDirectory(prefix="quota-sentinel.", dir=parent)

    def _burst(
        self, providers: Sequence[str], workspace: Path, phase: str, limit: int
    ) -> tuple[Dict[str, str], Dict[str, Path]]:
        remaining = list(providers)
        results: Dict[str, str] = {p: "发送失败" for p in remaining}
        pi_raw: Dict[str, Path] = {}
        for provider in remaining:
            self.model_runner.prepare(provider, workspace)
        for provider in remaining:
            service.begin_attempt(self.state_dir, provider, self._now())

        for round_number in range(1, limit + 1):
            if not remaining:
                break
            if round_number > 1:
                for provider in remaining:
                    service.record_attempt(self.state_dir, provider, self._now())

            current = tuple(remaining)
            with ThreadPoolExecutor(max_workers=len(current)) as pool:
                pending = {
                    provider: pool.submit(
                        self.model_runner.run, provider, workspace,
                        phase, round_number, limit,
                    )
                    for provider in current
                }
                succeeded = []
                failed = []
                for provider in current:
                    try:
                        outcome = pending[provider].result()
                    except Exception:
                        logger.exception("model %s could not complete", provider)
                        failed.append(provider)
                        continue
                    pi_raw[provider] = outcome.quota_path
                    if outcome.success:
                        succeeded.append(provider)
                        results[provider] = "发送成功"
                    else:
                        failed.append(provider)
            # A crash before commit leaves debt recorded and errs toward an
            # extra attempt, never toward losing a due task.
            for provider in succeeded:
                service.commit_success(self.state_dir, provider, self._now())
            remaining = failed
            if remaining and round_number < limit:
                self.sleep(self.config.retry_interval)
        return results, pi_raw

    def _log_anchor(self, provider: str, before: Optional[int], after: Optional[int]) -> None:
        """One line per real boundary movement, on every path that can move it.

        The check path moves the anchor through ``decide_due``, the post-run
        and /usage paths through ``_sync``; both must report the same thing or
        the drift cannot be reconstructed from one place.
        """
        if after == before:
            return
        logger.info(
            "quota %s: reset anchor %s -> %s (%s)",
            provider, _reset_text(before), _reset_text(after),
            _drift_text(before, after),
        )

    def _sync(self, provider: str, reading: object, now: int) -> None:
        state = service.load_state(self.state_dir, provider)
        if provider in self.config.probe_only:
            # /usage recalibrates deadlines the same way a check does, so it
            # must honour the probe-only contract too: a disabled provider's
            # deadline follows the observation and never lingers matured.
            transition = policy.reanchor_probe_only(
                state, _observation(reading), now
            )
        else:
            transition, _ = policy.sync_deadline(
                state, _observation(reading), now
            )
        service.apply_transition(self.state_dir, provider, transition)
        # The policy already computes WHY the deadline did or did not move;
        # writing its own words down is what makes the window boundary
        # measurable after the fact. The question it answers is the one that
        # decides whether the four-minute reset buffer earns its cost: had the
        # provider's reset already rolled when the deadline fired, and how far
        # does the boundary drift each cycle?
        logger.info("sched %s: %s", provider, transition.reason)
        self._log_anchor(
            provider, state.last_known_reset, transition.after.last_known_reset
        )

    def _run_selected(
        self, providers: Sequence[str], workspace: Path, collector: object
    ) -> Dict[str, str]:
        names = self._providers(providers)
        # Belt and braces for the one contract this switch exists for. Both
        # callers above already filter, but nothing may start a model for a
        # provider whose trigger is switched off — not even if a future caller
        # hands this method a roster that was never filtered.
        disabled = tuple(p for p in names if p in self.config.probe_only)
        if disabled:
            logger.info(
                "run: probe-only provider(s) dropped from the roster: %s",
                " ".join(disabled),
            )
            names = tuple(p for p in names if p not in self.config.probe_only)
        if not names:
            logger.info("run: every selected provider is probe-only; no attempt")
            return {}
        # Before the first attempt, never after: a missing push credential
        # discovered once three 300s model timeouts have been spent is a
        # failure the operator paid for and cannot use.
        self._require_ready(names)
        results, pi_raw = self._burst(
            names, workspace, "initial", self.config.initial_attempts
        )
        collector.save_pi_snapshots(pi_raw)
        readings: Mapping[str, object] = {}
        try:
            with acquire_quota_lock(self.state_dir, timeout=self.config.quota_wait):
                readings = self._collect(collector, pi_raw, purpose="schedule")
                now = self._now()
                for provider in names:
                    if results[provider] != "发送成功":
                        continue
                    observation = _observation(readings.get(provider))
                    reset_at = policy.valid_reset_at(observation, now)
                    if reset_at is not None:
                        service.record_last_window(self.state_dir, provider, reset_at)
                        self._sync(provider, readings[provider], now)
                        logger.info(
                            "run: post-run sync %s fresh_reset=%s",
                            provider, reset_at,
                        )
        except LockError:
            # Any acquisition failure counts as busy, exactly as the shell
            # treats it. The attempts already happened, so the task card is
            # still sent below; only the deadline calibration is skipped.
            logger.warning("post-run quota.lock unavailable; fallback deadlines remain")
        self.notifier.task(names, results, readings, self._now())
        return results

    def run(self, providers: Sequence[str] = ()) -> Dict[str, str]:
        requested = tuple(providers)
        names = self._providers(requested)
        disabled = tuple(p for p in names if p in self.config.probe_only)
        if disabled:
            if requested:
                # An explicit target is an operator asking to spend quota on a
                # provider whose model trigger is switched off. Refusing before
                # the run lock, the probe and the first attempt is the only
                # honest answer: quietly skipping would look like a delivered
                # task, and running it would break the one promise the switch
                # makes.
                raise ValueError(
                    "probe-only provider(s) cannot be run: %s "
                    "(unset QUOTA_SENTINEL_PROBE_ONLY to re-enable the model "
                    "trigger)" % ", ".join(disabled)
                )
            # The default roster is "everything that can be delivered", so a
            # switched-off provider is simply not part of it, and the task card
            # that follows reports only what actually ran.
            logger.info(
                "run: probe-only provider(s) excluded from the default roster: %s",
                " ".join(disabled),
            )
            names = tuple(p for p in names if p not in self.config.probe_only)
        if not names:
            logger.info("run: every provider is probe-only; nothing to attempt")
            return {}
        with acquire_run_lock(self.state_dir, timeout=0):
            read_authority(self.state_dir)
            with self._workspace() as temp:
                workspace = Path(temp)
                return self._run_selected(
                    names, workspace, self.quota_collector_factory(workspace)
                )

    def check(self) -> tuple[str, ...]:
        try:
            lock = acquire_run_lock(self.state_dir, timeout=0)
        except RunLockError:
            # Every acquisition failure — busy, or shlock missing entirely —
            # is a skip for the shell's watchdog: a scheduler tick that
            # cannot serialize must not run models, and must not be fatal.
            logger.info("check: run.lock busy, skipped")
            return ()
        with lock:
            started = self._now()
            read_authority(self.state_dir)
            with self._workspace() as temp:
                workspace = Path(temp)
                collector = self.quota_collector_factory(workspace)
                now = self._now()
                awaiting_resume = self._active_transitions()
                retry = service.retry_due_providers(
                    self.state_dir, self.opening_providers, now,
                    self.config.watchdog_retry_gap,
                )
                # A probe-only provider is never executed, so it must never
                # enter a retry burst either: its debt is cleared by the
                # probe-only transition below, not repaid with a model turn.
                retry = [
                    provider for provider in retry
                    if provider not in self.config.probe_only and provider not in awaiting_resume
                ]
                retry_results: Dict[str, str] = {}
                pi_raw: Mapping[str, Path] = {}
                if retry:
                    # The same refusal every other burst gets, for the same
                    # reason: a retry-eligible provider whose task cannot be
                    # delivered spends quota on a run nobody can read. This
                    # path used to skip the check because it runs before the
                    # due evaluation that owns it.
                    self._require_ready(retry)
                    logger.info(
                        "check: pending debt on %s; watchdog retry burst first",
                        " ".join(retry),
                    )
                    retry_results, pi_raw = self._burst(
                        retry, workspace, "watchdog-retry",
                        self.config.watchdog_attempts,
                    )
                    collector.save_pi_snapshots(pi_raw)

                # A recovered retry is owed a card from here on: the attempts
                # already happened and their debt is already paid, so a later
                # tick has nothing left to report. The quota lock below guards
                # the DEADLINE CALIBRATION only, which is why the card has to be
                # sent from both exits — dropping it was how a successful retry
                # became permanently invisible.
                recovered = [
                    provider for provider in retry
                    if retry_results.get(provider) == "发送成功"
                ]

                try:
                    quota_lock = acquire_quota_lock(self.state_dir, timeout=0)
                except LockError:
                    logger.info("check: quota.lock busy, skipped")
                    if recovered:
                        self.notifier.task(
                            recovered, retry_results, {}, self._now()
                        )
                    return ()
                with quota_lock:
                    readings = self._collect(collector, pi_raw, purpose="schedule")
                    now = self._now()
                    # Phase C decides only providers WITHOUT a pending debt,
                    # read LIVE here rather than from the gap-filtered retry
                    # list above. A provider still waiting out its watchdog
                    # gap is not due just because an older deadline matured:
                    # deciding it would start a fresh three-attempt initial
                    # burst on every tick of the wait loop and card each one.
                    pending = set(
                        service.pending_providers(self.state_dir, self.opening_providers)
                    )
                    due = []
                    for provider in self.opening_providers:
                        if provider in awaiting_resume:
                            resumed = service.resume_provider(self.state_dir, provider, _observation(readings.get(provider)), now)
                            if resumed.changed or policy.valid_reset_at(_observation(readings.get(provider)), now) is not None:
                                awaiting_resume.discard(provider)
                                self._write_active_transitions(awaiting_resume)
                            continue
                        if provider in self.config.probe_only:
                            # Switched off, but still measured: the probe above
                            # already read its quota, and this transition keeps
                            # the deadline ahead of now so neither the timer
                            # nor the watchdog grid can spin on a matured
                            # deadline that will never be executed.
                            result = service.reanchor_probe_only(
                                self.state_dir, provider, now,
                                _observation(readings.get(provider)),
                            )
                            logger.info("sched %s: %s", provider, result.reason)
                            self._log_anchor(
                                provider, result.before_state.last_known_reset,
                                result.state.last_known_reset,
                            )
                            continue
                        if provider in pending:
                            logger.info(
                                "check: %s pending (debt unpaid); normal due "
                                "evaluation skipped", provider,
                            )
                            continue
                        result = service.decide_due(
                            self.state_dir, provider, now,
                            _observation(readings.get(provider)),
                        )
                        # The shell logged one "sched <provider>: <reason>"
                        # line per provider per check; that line is also the
                        # record of what the provider reported about its reset
                        # at the moment the deadline fired.
                        logger.info("sched %s: %s", provider, result.reason)
                        self._log_anchor(
                            provider, result.before_state.last_known_reset,
                            result.state.last_known_reset,
                        )
                        if result.decision is Decision.RUN_NOW:
                            due.append(provider)

                if recovered:
                    self.notifier.task(
                        recovered, retry_results, readings, self._now()
                    )
                elapsed = self._now() - started
                if not due:
                    # The shell logged the roster's deadlines on a quiet tick;
                    # without this an operator reading the daily run log sees
                    # nothing at all and cannot tell a healthy tick from a
                    # scheduler that stopped running.
                    logger.info("check: nothing due (%s) (%ds)",
                                self._deadline_summary(), elapsed)
                else:
                    logger.info("check: due providers: %s (%ds)",
                                " ".join(due), elapsed)
                if due:
                    self._run_selected(due, workspace, collector)
                return tuple(due)

    def _deadline_summary(self) -> str:
        """The shell's readable form: a Shanghai wall clock, not an epoch."""
        states = service.load_roster(self.state_dir, self.active_providers)
        rendered = []
        for provider in self.opening_providers:
            next_due = states[provider].next_due_at
            rendered.append("%s next %s" % (
                provider,
                format_reset_time(next_due) if next_due is not None else "unset",
            ))
        return ", ".join(rendered)

    def usage(self) -> None:
        started = self._now()
        logger.info("usage: requested")
        read_authority(self.state_dir)
        with self._workspace() as temp:
            workspace = Path(temp)
            collector = self.quota_collector_factory(workspace)
            try:
                with acquire_quota_lock(
                    self.state_dir, timeout=self.config.quota_wait
                ):
                    readings = self._collect(collector, purpose="display")
            except LockError:
                # A broken lock is indistinguishable from a busy one at this
                # boundary, and the operator's /usage must still get an
                # answer: silence would look like the bot is down.
                logger.warning(
                    "usage: quota busy after %ss; replying busy",
                    self.config.quota_wait,
                )
                self.notifier.busy(self._now())
                return

            try:
                with acquire_run_lock(self.state_dir, timeout=0):
                    now = self._now()
                    for provider in (self.opening_providers if self.runtime_plan is None or self.runtime_plan.start_scheduler or self.runtime_plan.command in ("check", "wait", "run") else ()) :
                        self._sync(provider, readings.get(provider), now)
            except RunLockBusyError:
                logger.info("usage: scheduler busy; deadline sync skipped")
            self.notifier.usage(readings, self._now())
            logger.info("usage: completed (%ds)", self._now() - started)

    def status(self) -> Mapping[str, object]:
        read_authority(self.state_dir)
        return service.load_roster(self.state_dir, self.active_providers)

    def wait(self) -> None:
        logger.info("timer: watching deadlines (recheck every %ss)",
                    self.config.timer_recheck)
        while True:
            now = self._now()
            next_due = service.next_due(self.state_dir, self.opening_providers)
            if next_due is not None and now < next_due:
                self.sleep(min(next_due - now, self.config.timer_recheck))
                continue
            self.check()
            next_due = service.next_due(self.state_dir, self.opening_providers)
            if next_due is not None and self._now() < next_due:
                self.sleep(1)
            else:
                self.sleep(self.config.timer_recheck)


__all__ = ["AppConfig", "Application"]
