#!/usr/bin/env python3
"""Phase 3C tests: the scheduler policy as pure transitions.

The migration's contract is BEHAVIOR PARITY, not a redesign, so this suite
is written as a transcription check against the shell policy that shipped
before it. Every case names the invariant it protects:

  * success commit / retry debt creation / repayment / last_attempt /
    last_task advancement / generation anchor init / candidate creation /
    candidate confirmation / far-reset promotion / Fresh calibration /
    Stale no-mutation / matured-debt protection / reset-buffer blocking /
    last_window updates;
  * at-least-once directionality: a crash prefix of any transition may
    duplicate work but may never lose a due task;
  * the cumulative near-movement loophole stays closed (the anchor does
    not follow repeated small movements);
  * the shell holds no second copy of any policy value.

Run: PYTHONPATH=. uv run --frozen --no-sync python tests/python-scheduler-regression.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.scheduler import policy, service
from quota_sentinel.scheduler.models import Decision, QuotaObservation, SyncAction
from quota_sentinel.scheduler.observation import (
    observation_for,
    read_normalized_quota,
)
from quota_sentinel.state import (
    AuthoritativeStateStore,
    bootstrap_legacy_authority,
    BackendAuthority,
    BACKEND_JSON,
    FileStateStore,
    JsonStateStore,
    ProviderState,
    ResetCandidate,
    cutover_to_json,
    write_authority,
)

SHELL = REPO / "quota-sentinel.sh"


def state(**kwargs) -> ProviderState:
    return ProviderState(**kwargs)


def fresh(reset_at: int) -> QuotaObservation:
    return QuotaObservation(fresh=True, reset_at=reset_at, source="test")


STALE = QuotaObservation(fresh=False, reset_at=None, source="cache")


class PurePolicyTests(unittest.TestCase):
    """The transitions themselves — no filesystem, no clock, no locks."""

    NOW = 1_000_000

    # ---- fallback and trust gates ---------------------------------------
    def test_fallback_due_uses_last_task_or_now(self):
        self.assertEqual(policy.fallback_due(state(), 500), 500)
        self.assertEqual(
            policy.fallback_due(state(last_task_at=100), 500),
            100 + policy.RUN_INTERVAL_SECONDS,
        )

    def test_trusted_reset_expires_with_the_generation(self):
        # A trusted reset at or before the last success belongs to the
        # previous generation and is no longer trusted.
        self.assertIsNone(policy.current_trusted_reset(state()))
        self.assertIsNone(
            policy.current_trusted_reset(
                state(last_known_reset=100, last_task_at=100)
            )
        )
        self.assertIsNone(
            policy.current_trusted_reset(
                state(last_known_reset=90, last_task_at=100)
            )
        )
        # A reset still in the future AFTER that success stays authoritative.
        self.assertEqual(
            policy.current_trusted_reset(
                state(last_known_reset=200, last_task_at=100)
            ),
            200,
        )

    def test_valid_reset_requires_fresh_and_a_plausible_window(self):
        now = self.NOW
        self.assertIsNone(policy.valid_reset_at(STALE, now))
        self.assertIsNone(policy.valid_reset_at(fresh(now), now))       # not future
        self.assertIsNone(policy.valid_reset_at(fresh(now - 5), now))   # past
        self.assertIsNone(
            policy.valid_reset_at(
                fresh(now + policy.MAX_WINDOW_FUTURE_SECONDS + 1), now
            )
        )
        self.assertEqual(
            policy.valid_reset_at(fresh(now + 60), now), now + 60
        )
        self.assertEqual(
            policy.valid_reset_at(
                fresh(now + policy.MAX_WINDOW_FUTURE_SECONDS), now
            ),
            now + policy.MAX_WINDOW_FUTURE_SECONDS,
        )
        # A fresh observation with no usable reset is still no evidence.
        self.assertIsNone(
            policy.valid_reset_at(QuotaObservation(fresh=True), now)
        )

    def test_schedule_block_reasons(self):
        now = self.NOW
        self.assertEqual(
            policy.schedule_block_reason(state(retry_pending=True), now),
            "retry-pending",
        )
        self.assertIsNone(policy.schedule_block_reason(state(), now))
        self.assertEqual(
            policy.schedule_block_reason(state(next_due_at=now), now), "overdue"
        )
        self.assertEqual(
            policy.schedule_block_reason(state(next_due_at=now - 1), now),
            "overdue",
        )
        reset = now - 10
        due = reset + policy.RESET_BUFFER_SECONDS
        self.assertEqual(
            policy.schedule_block_reason(
                state(next_due_at=due, last_known_reset=reset), now
            ),
            "reset-buffer",
        )
        # Before the reset the buffer rule does not apply yet.
        self.assertIsNone(
            policy.schedule_block_reason(
                state(next_due_at=due, last_known_reset=now + 10), now
            )
        )

    def test_retry_due_spacing(self):
        self.assertFalse(policy.retry_due(state(), self.NOW, 100))
        self.assertTrue(
            policy.retry_due(state(retry_pending=True), self.NOW, 100)
        )
        self.assertFalse(
            policy.retry_due(
                state(retry_pending=True, last_attempt_at=self.NOW - 99),
                self.NOW, 100,
            )
        )
        self.assertTrue(
            policy.retry_due(
                state(retry_pending=True, last_attempt_at=self.NOW - 100),
                self.NOW, 100,
            )
        )

    # ---- transition: attempt bookkeeping ---------------------------------
    def test_retry_debt_is_created_before_the_attempt(self):
        """The whole at-least-once guarantee for a due task: the debt is
        durable BEFORE any model process exists."""
        before = state(next_due_at=900, last_task_at=500)
        after = policy.begin_attempt(before, self.NOW)
        self.assertTrue(after.after.retry_pending)
        self.assertEqual(after.after.last_attempt_at, self.NOW)
        # Nothing else moves: a due-but-unsucceeded task keeps its deadline.
        self.assertEqual(after.after.next_due_at, 900)
        self.assertEqual(after.after.last_task_at, 500)

    def test_record_attempt_only_moves_last_attempt(self):
        before = state(last_attempt_at=1, retry_pending=True, next_due_at=9)
        after = policy.record_attempt(before, self.NOW).after
        self.assertEqual(after.last_attempt_at, self.NOW)
        self.assertEqual(after.next_due_at, 9)
        self.assertTrue(after.retry_pending)

    def test_success_commit_is_the_only_last_task_writer(self):
        before = state(
            last_attempt_at=1,
            last_task_at=2,
            next_due_at=3,
            retry_pending=True,
            last_known_reset=4,
            last_triggered_window="4",
            reset_anchor=4,
            reset_candidate=ResetCandidate(5, 6),
        )
        transition = policy.commit_success(before, self.NOW)
        after = transition.after
        self.assertEqual(after.last_attempt_at, self.NOW)
        self.assertEqual(after.last_task_at, self.NOW)
        self.assertFalse(after.retry_pending)
        self.assertIsNone(after.reset_candidate)
        self.assertIsNone(after.reset_anchor)
        self.assertEqual(
            after.next_due_at, self.NOW + policy.RUN_INTERVAL_SECONDS
        )
        # last_triggered_window belongs to the run, not the commit.
        self.assertEqual(after.last_triggered_window, "4")
        # The success commit is the one transition that materializes a slot
        # whose value did not change (retry_pending must be visibly 0).
        self.assertEqual(tuple(transition.publish), ("retry_pending",))
        self.assertTrue(transition.writes)

    def test_last_window_is_recorded_from_the_reset(self):
        after = policy.record_last_window(state(), 1234).after
        self.assertEqual(after.last_triggered_window, "1234")

    # ---- transition: deadline calibration --------------------------------
    def test_generation_anchor_is_established_by_the_first_fresh_reset(self):
        reset = self.NOW + 1000
        transition, action = policy.sync_deadline(state(), fresh(reset), self.NOW)
        self.assertEqual(action, SyncAction.ANCHOR_ESTABLISHED)
        self.assertEqual(transition.after.last_known_reset, reset)
        self.assertEqual(
            transition.after.next_due_at, reset + policy.RESET_BUFFER_SECONDS
        )
        self.assertEqual(transition.after.reset_anchor, reset)
        self.assertIsNone(transition.after.reset_candidate)

    def test_anchor_is_backfilled_from_the_trusted_reset(self):
        anchor_reset = self.NOW + 1000
        before = state(last_known_reset=anchor_reset, last_attempt_at=1)
        near = anchor_reset + policy.RESET_NEAR_MOVEMENT_SECONDS
        transition, action = policy.sync_deadline(before, fresh(near), self.NOW)
        self.assertEqual(action, SyncAction.NEAR_MOVEMENT)
        self.assertEqual(transition.after.reset_anchor, anchor_reset)
        self.assertEqual(transition.after.last_known_reset, near)
        self.assertIn("reset anchor initialized", transition.reason)

    def test_stale_observation_never_mutates(self):
        for before in (
            state(),
            state(last_known_reset=self.NOW + 100, reset_anchor=self.NOW + 100),
            state(next_due_at=self.NOW + 10, last_known_reset=self.NOW + 100),
        ):
            with self.subTest(before=before):
                transition, action = policy.sync_deadline(before, STALE, self.NOW)
                self.assertEqual(action, SyncAction.NO_VALID_QUOTA)
                self.assertEqual(transition.after, before)
                self.assertFalse(transition.changed)
                self.assertFalse(transition.writes)

    def test_matured_debt_protection_ignores_a_fresh_candidate(self):
        """P1-1: a probe at due time must not push the deadline forward."""
        due = self.NOW - 1
        before = state(next_due_at=due, last_task_at=due - 1000)
        transition, decision = policy.evaluate_due(
            before, fresh(self.NOW + 5000), self.NOW
        )
        self.assertEqual(decision, Decision.RUN_NOW)
        self.assertEqual(transition.after, before)
        self.assertFalse(transition.changed)
        self.assertIn("ignored this round", transition.reason)

    def test_matured_debt_reports_the_absence_of_fresh_data(self):
        before = state(next_due_at=self.NOW - 1)
        transition, decision = policy.evaluate_due(before, STALE, self.NOW)
        self.assertEqual(decision, Decision.RUN_NOW)
        self.assertFalse(transition.changed)
        self.assertIn("no valid fresh data", transition.reason)

    def test_reset_buffer_blocks_writes_until_success(self):
        reset = self.NOW - 10
        before = state(
            next_due_at=reset + policy.RESET_BUFFER_SECONDS,
            last_known_reset=reset,
        )
        transition, action = policy.sync_deadline(
            before, fresh(self.NOW + 900), self.NOW
        )
        self.assertEqual(action, SyncAction.BLOCKED)
        self.assertEqual(transition.after, before)
        self.assertFalse(transition.writes)
        self.assertIn("sync blocked", transition.reason)

    def test_far_reset_needs_two_stable_observations(self):
        anchor = self.NOW + 1000
        before = state(last_known_reset=anchor, last_attempt_at=1)
        far = anchor + policy.RESET_NEAR_MOVEMENT_SECONDS + 5000

        # 1st observation: recorded as a candidate, deadline untouched.
        first, action = policy.sync_deadline(before, fresh(far), self.NOW)
        self.assertEqual(action, SyncAction.CANDIDATE_CREATED)
        self.assertEqual(first.after.reset_candidate, ResetCandidate(far, self.NOW))
        self.assertEqual(first.after.last_known_reset, anchor)
        self.assertEqual(first.after.next_due_at, before.next_due_at)
        self.assertEqual(first.after.reset_anchor, anchor)

        # 2nd observation, too soon: reported, still no promotion.
        too_soon = self.NOW + policy.RESET_CONFIRM_MIN_AGE_SECONDS - 1
        second, action = policy.sync_deadline(
            first.after, fresh(far), too_soon
        )
        self.assertEqual(action, SyncAction.CANDIDATE_PENDING)
        self.assertFalse(second.changed)

        # 3rd observation, old enough and within tolerance: promoted.
        mature = self.NOW + policy.RESET_CONFIRM_MIN_AGE_SECONDS
        third, action = policy.sync_deadline(first.after, fresh(far), mature)
        self.assertEqual(action, SyncAction.CANDIDATE_PROMOTED)
        self.assertIsNone(third.after.reset_candidate)
        self.assertEqual(third.after.last_known_reset, far)
        self.assertEqual(
            third.after.next_due_at, far + policy.RESET_BUFFER_SECONDS
        )
        self.assertEqual(third.after.reset_anchor, far)

    def test_promotion_takes_the_later_of_the_two_observations(self):
        anchor = self.NOW + 1000
        far = anchor + policy.RESET_NEAR_MOVEMENT_SECONDS + 5000
        candidate = ResetCandidate(far, self.NOW)
        before = state(
            last_known_reset=anchor,
            last_attempt_at=1,
            reset_anchor=anchor,
            reset_candidate=candidate,
        )
        jittered = far + policy.RESET_CONFIRM_MATCH_SECONDS
        transition, action = policy.sync_deadline(
            before, fresh(jittered),
            self.NOW + policy.RESET_CONFIRM_MIN_AGE_SECONDS,
        )
        self.assertEqual(action, SyncAction.CANDIDATE_PROMOTED)
        self.assertEqual(transition.after.last_known_reset, jittered)

    def test_an_unstable_far_reset_never_confirms(self):
        anchor = self.NOW + 1000
        far = anchor + policy.RESET_NEAR_MOVEMENT_SECONDS + 5000
        before = state(
            last_known_reset=anchor,
            last_attempt_at=1,
            reset_anchor=anchor,
            reset_candidate=ResetCandidate(far, self.NOW),
        )
        # Outside the match tolerance: replaced by a fresh candidate.
        moving = far + policy.RESET_CONFIRM_MATCH_SECONDS + 1
        transition, action = policy.sync_deadline(
            before, fresh(moving),
            self.NOW + policy.RESET_CONFIRM_MIN_AGE_SECONDS,
        )
        self.assertEqual(action, SyncAction.CANDIDATE_CREATED)
        self.assertEqual(
            transition.after.last_known_reset, anchor
        )  # deadline never moved

    def test_cumulative_near_movement_cannot_starve_the_task(self):
        """The anchor is the ceiling: repeated small +N-second observations
        must not walk the deadline forward indefinitely."""
        anchor = self.NOW
        before = state(
            last_known_reset=anchor, last_attempt_at=1, reset_anchor=anchor
        )
        now = self.NOW
        for step in range(1, 25):
            now += 1
            observation = fresh(anchor + step * 20)   # +20s each time
            transition, action = policy.sync_deadline(before, observation, now)
            before = transition.after
            self.assertIn(
                action,
                (SyncAction.NEAR_MOVEMENT, SyncAction.CANDIDATE_CREATED,
                 SyncAction.CANDIDATE_PENDING),
            )
        # Every accepted movement stayed inside the tolerance band above the
        # anchor, so the deadline never exceeded anchor + tolerance + buffer.
        self.assertLessEqual(
            before.last_known_reset,
            anchor + policy.RESET_NEAR_MOVEMENT_SECONDS,
        )
        self.assertLessEqual(
            before.next_due_at,
            anchor + policy.RESET_NEAR_MOVEMENT_SECONDS
            + policy.RESET_BUFFER_SECONDS,
        )

    # ---- transition: the full due decision -------------------------------
    def test_decide_seeds_the_no_quota_fallback(self):
        before = state(last_task_at=self.NOW - policy.RUN_INTERVAL_SECONDS - 1)
        transition, decision = policy.evaluate_due(before, STALE, self.NOW)
        self.assertEqual(decision, Decision.RUN_NOW)
        self.assertEqual(
            transition.after.next_due_at, before.last_task_at + policy.RUN_INTERVAL_SECONDS
        )

    def test_decide_waits_when_the_fallback_is_in_the_future(self):
        before = state(last_task_at=self.NOW)
        transition, decision = policy.evaluate_due(before, STALE, self.NOW)
        self.assertEqual(decision, Decision.WAIT)
        # The fallback is PERSISTED even when it is still in the future: an
        # unrecorded deadline would make every later tick re-derive it.
        self.assertTrue(transition.changed)
        self.assertEqual(
            transition.after.next_due_at, self.NOW + policy.RUN_INTERVAL_SECONDS
        )
        self.assertIn("seeded no-quota fallback", transition.reason)

    def test_decide_prefers_the_fresh_deadline_over_the_fallback(self):
        reset = self.NOW + 600
        before = state(last_task_at=self.NOW - 10_000)
        transition, decision = policy.evaluate_due(before, fresh(reset), self.NOW)
        self.assertEqual(decision, Decision.WAIT)
        self.assertEqual(
            transition.after.next_due_at, reset + policy.RESET_BUFFER_SECONDS
        )

    def test_decide_waits_when_the_deadline_is_still_ahead(self):
        before = state(last_known_reset=self.NOW + 1000, last_attempt_at=1)
        transition, decision = policy.evaluate_due(
            before, fresh(self.NOW + 1000), self.NOW
        )
        self.assertEqual(decision, Decision.WAIT)
        self.assertEqual(transition.after.next_due_at, self.NOW + 1240)

    def test_retry_blocked_is_a_provable_no_op(self):
        before = state(retry_pending=True, next_due_at=1)
        transition = policy.retry_blocked(before, self.NOW)
        self.assertEqual(transition.decision, Decision.RETRY)
        self.assertEqual(transition.after, before)
        self.assertFalse(transition.writes)

    def test_min_next_due_excludes_pending_debt(self):
        states = {
            "codex": state(next_due_at=10),
            "antigravity": state(next_due_at=5, retry_pending=True),
            "opencode": state(next_due_at=20),
        }
        self.assertEqual(policy.min_next_due(states), 10)
        self.assertIsNone(policy.min_next_due({}))
        self.assertIsNone(
            policy.min_next_due({"codex": state(next_due_at=5, retry_pending=True)})
        )


class ObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "quota.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_the_five_hour_window_only(self):
        self.path.write_text(
            '{"source":"Native","fresh":true,'
            '"fiveHour":{"remainingPercent":90,"resetAt":1234},'
            '"monthly":{"remainingPercent":50,"resetAt":999999}}',
            encoding="utf-8",
        )
        observation = read_normalized_quota(self.path)
        self.assertTrue(observation.fresh)
        self.assertEqual(observation.reset_at, 1234)
        self.assertEqual(observation.source, "Native")

    def test_unreadable_probe_is_never_fresh(self):
        """No file, no bytes, no JSON: freshness must be False, because a
        deadline may only ever move on evidence the probe actually carried."""
        for label, payload in {
            "missing": None,
            "empty": "",
            "invalid json": "{ nope",
            "not an object": "[1,2,3]",
            "no fresh flag": '{"fiveHour":{"resetAt":1234}}',
            "fresh not a bool": '{"fresh":"yes","fiveHour":{"resetAt":1234}}',
        }.items():
            with self.subTest(label=label):
                if payload is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(payload, encoding="utf-8")
                observation = read_normalized_quota(self.path)
                self.assertFalse(observation.fresh)
                self.assertIsNone(policy.valid_reset_at(observation, 0))

    def test_an_unusable_reset_is_not_evidence(self):
        """A fresh probe whose five-hour reset is missing or ill-typed is
        still not usable: freshness describes the probe, the reset is the
        scheduling evidence, and this reader never invents one."""
        for label, payload in {
            "no five hour": '{"fresh":true}',
            "reset not an int": '{"fresh":true,"fiveHour":{"resetAt":"1234"}}',
            "float reset": '{"fresh":true,"fiveHour":{"resetAt":1.5}}',
            "negative reset": '{"fresh":true,"fiveHour":{"resetAt":-3}}',
            "bool reset": '{"fresh":true,"fiveHour":{"resetAt":true}}',
            "null reset": '{"fresh":true,"fiveHour":{"resetAt":null}}',
        }.items():
            with self.subTest(label=label):
                self.path.write_text(payload, encoding="utf-8")
                observation = read_normalized_quota(self.path)
                self.assertIsNone(observation.reset_at)
                self.assertFalse(observation.usable)
                # ... and it therefore cannot calibrate a deadline.
                self.assertIsNone(policy.valid_reset_at(observation, 0))

    def test_freshness_override_can_only_lower_trust(self):
        self.path.write_text(
            '{"fresh":true,"fiveHour":{"resetAt":99}}', encoding="utf-8"
        )
        self.assertTrue(observation_for(self.path, True).fresh)
        self.assertTrue(observation_for(self.path, None).fresh)
        self.assertFalse(observation_for(self.path, False).fresh)
        # A caller cannot claim freshness the file does not carry.
        self.path.write_text(
            '{"fresh":false,"fiveHour":{"resetAt":99}}', encoding="utf-8"
        )
        self.assertFalse(observation_for(self.path, True).fresh)


class ShellOwnershipTests(unittest.TestCase):
    """D. the shell must not keep a second copy of any policy value."""

    SHELL_TEXT = SHELL.read_text(encoding="utf-8")

    def test_no_numeric_policy_literals_remain(self):
        names = (
            "RUN_INTERVAL_SECONDS",
            "RESET_BUFFER_SECONDS",
            "RESET_NEAR_MOVEMENT_SECONDS",
            "RESET_CONFIRM_MIN_AGE_SECONDS",
            "RESET_CONFIRM_MATCH_SECONDS",
            "MAX_WINDOW_FUTURE_SECONDS",
            "RETRY_INTERVAL_SECONDS",
            "INITIAL_ATTEMPT_LIMIT",
            "WATCHDOG_ATTEMPT_LIMIT",
            "WATCHDOG_RETRY_GAP_SECONDS",
        )
        for name in names:
            with self.subTest(name=name):
                # No assignment form (`name=123`, `readonly name=123`) may
                # exist anywhere in the shell: values arrive from Python.
                pattern = re.compile(
                    rf"(^|[^A-Za-z0-9_])(readonly\s+|typeset\s+-\w+\s+)?"
                    rf"{name}\s*=\s*[0-9]"
                )
                for match in pattern.finditer(self.SHELL_TEXT):
                    line = self.SHELL_TEXT[: match.start()].count("\n") + 1
                    self.fail(
                        f"{name} is assigned a literal at line {line}: the "
                        "scheduler policy must have exactly one owner"
                    )

    def test_shell_fetches_policy_from_the_domain(self):
        self.assertIn("scheduler_policy_config()", self.SHELL_TEXT)
        self.assertIn("scheduler-config", self.SHELL_TEXT)

    def test_shell_holds_no_state_transition_policy(self):
        """The decision functions must delegate; none of them may contain
        their own arithmetic on deadlines or tolerances."""
        forbidden = (
            "RESET_BUFFER_SECONDS +",
            "+ RESET_BUFFER_SECONDS",
            "RESET_NEAR_MOVEMENT_SECONDS",
            "RESET_CONFIRM_",
            "MAX_WINDOW_FUTURE_SECONDS",
        )
        regions = {}
        for name in (
            "sync_provider_deadline_from_quota",
            "evaluate_provider",
            "valid_provider_reset_at",
            "provider_schedule_block_reason",
            "provider_fallback_due",
        ):
            start = self.SHELL_TEXT.index(f"{name}() {{")
            end = self.SHELL_TEXT.index("\n}", start)
            regions[name] = self.SHELL_TEXT[start:end]
        for name, body in regions.items():
            for token in forbidden:
                with self.subTest(function=name, token=token):
                    self.assertNotIn(token, body)

    def test_shell_decision_functions_call_the_bridge(self):
        for name, verb in (
            ("sync_provider_deadline_from_quota", "scheduler-sync"),
            ("evaluate_provider", "scheduler-decide"),
            ("provider_fallback_due", "scheduler-fallback-due"),
            ("valid_provider_reset_at", "scheduler-valid-reset"),
            ("read_next_due", "scheduler-next-due"),
            ("provider_retry_due", "scheduler-retry-due"),
        ):
            with self.subTest(function=name):
                start = self.SHELL_TEXT.index(f"{name}() {{")
                end = self.SHELL_TEXT.index("\n}", start)
                self.assertIn(verb, self.SHELL_TEXT[start:end])

    def test_bridge_runs_on_the_project_interpreter(self):
        self.assertIn('PYTHONPATH="$SCRIPT_DIR" "$PYTHON3_BIN"', self.SHELL_TEXT)


class TransitionPersistenceTests(unittest.TestCase):
    """E. transitions commit identically through both backends."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        self.state_dir.mkdir(parents=True)
        # Every deployment has an authority manifest; the runtime requires
        # it, so a throwaway legacy deployment gets one too.
        bootstrap_legacy_authority(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _exercise(self, provider: str) -> list:
        """Drive the same transition sequence, returning observed states."""
        observed = []
        now = 1_000_000
        service.begin_attempt(self.state_dir, provider, now)
        observed.append(service.load_state(self.state_dir, provider))
        service.record_attempt(self.state_dir, provider, now + 5)
        observed.append(service.load_state(self.state_dir, provider))
        service.commit_success(self.state_dir, provider, now + 10)
        observed.append(service.load_state(self.state_dir, provider))
        service.record_last_window(self.state_dir, provider, now + 900)
        observed.append(service.load_state(self.state_dir, provider))
        service.decide_due(self.state_dir, provider, now + 20, STALE)
        observed.append(service.load_state(self.state_dir, provider))
        service.decide_due(
            self.state_dir, provider, now + 30, fresh(now + 4000)
        )
        observed.append(service.load_state(self.state_dir, provider))
        return observed

    def test_same_transitions_on_both_backends(self):
        provider = "codex"
        legacy = self._exercise(provider)

        # Start over on the JSON backend with the same input state.
        other = Path(self._tmp.name) / "json-state"
        other.mkdir()
        bootstrap_legacy_authority(other)
        self.state_dir = other
        cutover_to_json(other)
        routed = self._exercise(provider)

        self.assertEqual(legacy, routed)
        self.assertEqual(AuthoritativeStateStore(other).authority().backend, BACKEND_JSON)

    def test_success_materializes_retry_pending_zero(self):
        provider = "codex"
        service.commit_success(self.state_dir, provider, 500)
        path = self.state_dir / f"{provider}-retry-pending"
        self.assertTrue(path.exists(), "retry_pending=0 must be materialized")
        self.assertEqual(path.read_text(encoding="utf-8"), "0\n")

    def test_authority_flip_changes_where_transitions_land(self):
        provider = "codex"
        service.commit_success(self.state_dir, provider, 500)
        legacy_bytes = (self.state_dir / f"{provider}-next-due-at").read_bytes()

        cutover_to_json(self.state_dir)
        service.commit_success(self.state_dir, provider, 900)
        # The retired backend is frozen at the value it held at the cutover.
        self.assertEqual(
            (self.state_dir / f"{provider}-next-due-at").read_bytes(),
            legacy_bytes,
        )
        self.assertEqual(
            service.load_state(self.state_dir, provider).next_due_at,
            900 + policy.RUN_INTERVAL_SECONDS,
        )

    def test_no_op_decision_writes_nothing(self):
        provider = "codex"
        service.commit_success(self.state_dir, provider, 500)
        before = {
            path.name: path.read_bytes()
            for path in sorted(self.state_dir.iterdir())
        }
        # A stale observation cannot move a deadline.
        result = service.decide_due(self.state_dir, provider, 600, STALE)
        self.assertFalse(result.changed)
        after = {
            path.name: path.read_bytes()
            for path in sorted(self.state_dir.iterdir())
        }
        self.assertEqual(before, after)

    def test_json_authoritative_read_failure_fails_closed(self):
        provider = "codex"
        cutover_to_json(self.state_dir)
        (self.state_dir / f"{provider}-state.json").write_bytes(b"{ broken")
        from quota_sentinel.state import StateStoreError
        with self.assertRaises(StateStoreError):
            service.commit_success(self.state_dir, provider, 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
