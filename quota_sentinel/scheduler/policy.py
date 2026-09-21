"""Scheduler policy as pure transitions (Phase 3C).

Every function here is ``new_state = f(old_state, inputs, now)``: no
network, no model execution, no lock acquisition, no cross-provider
mutation, no filesystem access. The I/O boundary lives in
``quota_sentinel.scheduler.engine``, so each transition can be proven
against the shell's behavior without a process, a lock, or a clock.

This module is a TRANSCRIPTION of the shell policy that shipped before it,
not a redesign. The constants, the branch order and the tie-breaking are
the same on purpose: the migration's job is to move the decision, not to
change it. Where the shell's behavior is surprising, the surprise is
documented rather than silently fixed, and the black-box zsh suites stay
in place as the compatibility proof.

Invariants this policy encodes, quoted from the shell comments it
replaces because losing them is how a scheduler regresses:

* retry_pending-before-attempt — a due task records its debt BEFORE the
  first attempt, so a crash mid-burst still owes the task.
* failure does not advance last_task — only a verified success commits.
* matured debt cannot move later — a deadline that has matured is a
  committed obligation; a fresh probe arriving at due time may not
  re-anchor the window forward.
* overdue deadline cannot silently disappear — nothing here ever clears
  ``next_due_at``.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional, Tuple

from ..state.models import ProviderState, ResetCandidate
from .models import (
    NO_OBSERVATION,
    Decision,
    QuotaObservation,
    SyncAction,
    Transition,
)

# Cadence and tolerances. These values MUST match the shell constants of
# the same name; tests/python-scheduler-regression.py pins the parity.
RUN_INTERVAL_SECONDS = 18060              # 5 hours 01 minute
RESET_BUFFER_SECONDS = 240                # 4 minutes after reset
RESET_NEAR_MOVEMENT_SECONDS = 300         # immediate same-window jitter
RESET_CONFIRM_MIN_AGE_SECONDS = 60        # independent observation gap
RESET_CONFIRM_MATCH_SECONDS = 30          # stable timestamp tolerance
MAX_WINDOW_FUTURE_SECONDS = 21600         # 6 hours


# ---------------------------------------------------------------------------
# Reads that are really policy: they decide what counts as evidence.
# ---------------------------------------------------------------------------
def fallback_due(state: ProviderState, now: int) -> int:
    """The no-quota deadline: last real task + the run interval.

    With no successful task on record there is no interval to measure, so
    the provider is due immediately (the shell's ``print "$now"``).
    """
    if state.last_task_at is not None:
        return state.last_task_at + RUN_INTERVAL_SECONDS
    return now


def current_trusted_reset(state: ProviderState) -> Optional[int]:
    """The reset this generation currently trusts, or None.

    A trusted reset from before the most recent successful task belongs to
    the previous generation. A reset still in the future AFTER that success
    remains authoritative (a task was run manually before the window
    reset), so ``last_task_at`` alone is never used as a hard ceiling.
    """
    trusted_reset = state.last_known_reset
    if trusted_reset is None:
        return None
    last_task = state.last_task_at
    if last_task is not None and trusted_reset <= last_task:
        return None
    return trusted_reset


def schedule_block_reason(state: ProviderState, now: int) -> Optional[str]:
    """Why this provider's scheduler is temporarily write-blocked.

    A reset-based deadline becomes committed as soon as its reset occurs,
    not only after the four-minute buffer matures. Pending/overdue task
    debt remains blocked until a verified success writes a new fallback.
    """
    if state.retry_pending:
        return "retry-pending"
    next_due = state.next_due_at
    if next_due is None:
        return None
    if now >= next_due:
        return "overdue"
    scheduled_reset = state.last_known_reset
    if (
        scheduled_reset is not None
        and next_due == scheduled_reset + RESET_BUFFER_SECONDS
        and now >= scheduled_reset
    ):
        return "reset-buffer"
    return None


def valid_reset_at(observation: QuotaObservation, now: int) -> Optional[int]:
    """The Fresh trust gate plus plausibility window.

    A stale observation never yields a reset here, which is what makes
    "Stale quota no-mutation" a property of one function rather than of
    every caller remembering to check ``fresh``.
    """
    if not observation.fresh:
        return None
    reset_at = observation.reset_at
    if reset_at is None:
        return None
    if not (reset_at > now and reset_at <= now + MAX_WINDOW_FUTURE_SECONDS):
        return None
    return reset_at


def min_next_due(states: dict) -> Optional[int]:
    """The precision timer's next wake-up over a roster.

    Providers carrying an unpaid debt are deliberately excluded: their
    stale past deadline would spin the timer once per second. Debt
    repayment is driven by the retry phase instead.
    """
    candidates = [
        state.next_due_at
        for state in states.values()
        if not state.retry_pending and state.next_due_at is not None
    ]
    if not candidates:
        return None
    return min(candidates)


def retry_due(state: ProviderState, now: int, gap_seconds: int) -> bool:
    """Whether a pending debt may start another watchdog burst.

    Spaced at least ``gap_seconds`` from the last real attempt so the
    launchd grid and the precision timer never double-burst. A debt with
    no recorded attempt is always eligible.
    """
    if not state.retry_pending:
        return False
    if state.last_attempt_at is None:
        return True
    return now - state.last_attempt_at >= gap_seconds


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
def begin_attempt(state: ProviderState, now: int) -> Transition:
    """Record one attempt: debt first, then the attempt timestamp.

    The two writes are one transition because their ORDER is the
    at-least-once guarantee: ``retry_pending`` is raised before the model
    process exists, so a crash during the attempt still leaves the debt on
    disk. ``last_attempt_at`` only spaces watchdog bursts.
    """
    after = replace(state, retry_pending=True, last_attempt_at=now)
    return Transition(
        before=state,
        after=after,
        decision=Decision.RETRY,
        reason="attempt recorded (debt raised before the model runs)",
        changed=after != state,
    )


def record_attempt(state: ProviderState, now: int) -> Transition:
    """Update only ``last_attempt_at`` (debt already recorded)."""
    after = replace(state, last_attempt_at=now)
    return Transition(
        before=state,
        after=after,
        decision=Decision.RETRY,
        reason="attempt timestamp updated",
        changed=after != state,
    )


def commit_success(state: ProviderState, success_at: int) -> Transition:
    """The single success-commit transition.

    Only a verified model success may run this. It is the ONLY transition
    that advances ``last_task_at`` and the only one that clears the
    generation's candidate and anchor.
    """
    after = replace(
        state,
        last_attempt_at=success_at,
        last_task_at=success_at,
        retry_pending=False,
        reset_candidate=None,
        reset_anchor=None,
        next_due_at=success_at + RUN_INTERVAL_SECONDS,
    )
    return Transition(
        before=state,
        after=after,
        decision=Decision.NO_CHANGE,
        reason=(
            f"last_task={success_at} "
            f"fallback_due={success_at + RUN_INTERVAL_SECONDS} retry_pending=0"
        ),
        changed=after != state,
        # The pre-migration shell always published an explicit retry_pending=0
        # on success, even when it was already 0. "Absent" and "0" mean the
        # same thing to every reader, but the on-disk contract is observable,
        # so the transition keeps producing it instead of quietly changing it.
        publish=("retry_pending",),
    )


def record_last_window(state: ProviderState, reset_at: int) -> Transition:
    """Remember which five-hour window a successful run belonged to."""
    window = str(reset_at)
    after = replace(state, last_triggered_window=window)
    return Transition(
        before=state,
        after=after,
        decision=Decision.NO_CHANGE,
        reason=f"last_triggered_window {state.last_triggered_window or '<unset>'} -> {window}",
        changed=after != state,
    )


def sync_deadline(
    state: ProviderState, observation: QuotaObservation, now: int
) -> Tuple[Transition, SyncAction]:
    """Recalibrate the deadline from a quota observation.

    Mirrors the shell's ``sync_provider_deadline_from_quota`` branch for
    branch. Returns the transition and which branch fired; the branch
    order is load-bearing and is asserted by the parity tests.
    """
    reset_at = valid_reset_at(observation, now)
    if reset_at is None:
        return _keep(state, SyncAction.NO_VALID_QUOTA,
                     "no valid fresh reset; deadline untouched"), SyncAction.NO_VALID_QUOTA

    reset_due = reset_at + RESET_BUFFER_SECONDS

    block_reason = schedule_block_reason(state, now)
    if block_reason is not None:
        return _keep(
            state, SyncAction.BLOCKED,
            f"sync blocked reason={block_reason} "
            f"reset={_show(state.last_known_reset)} due={_show(state.next_due_at)} "
            f"now={now}; fresh candidate reset={reset_at} deferred until success",
        ), SyncAction.BLOCKED

    trusted_reset = current_trusted_reset(state)
    if trusted_reset is None:
        # The first Fresh reset of a generation establishes a finite
        # anchor. It may legitimately be much later than last_task+5h.
        after = replace(
            state,
            reset_candidate=None,
            reset_anchor=reset_at,
            last_known_reset=reset_at,
            next_due_at=reset_due,
        )
        return Transition(
            state, after, Decision.NO_CHANGE,
            f"fresh reset established generation anchor reset={reset_at} due={reset_due}",
            after != state,
        ), SyncAction.ANCHOR_ESTABLISHED

    # Upgrade an in-flight generation without changing its current
    # deadline. Persisting the EXISTING trusted reset (not the new
    # observation) is what closes the cumulative-near-movement loophole.
    anchor_reset = state.reset_anchor
    anchor_note = ""
    if anchor_reset is None:
        anchor_reset = trusted_reset
        anchor_note = f"; reset anchor initialized from trusted reset={anchor_reset}"

    if reset_at <= anchor_reset + RESET_NEAR_MOVEMENT_SECONDS:
        after = replace(
            state,
            reset_candidate=None,
            reset_anchor=anchor_reset,
            last_known_reset=reset_at,
            next_due_at=reset_due,
        )
        return Transition(
            state, after, Decision.NO_CHANGE,
            f"near-window movement accepted reset={reset_at} due={reset_due}"
            f"{anchor_note}",
            after != state,
        ), SyncAction.NEAR_MOVEMENT

    candidate = state.reset_candidate
    if candidate is not None:
        delta = abs(reset_at - candidate.reset_at)
        if (
            delta <= RESET_CONFIRM_MATCH_SECONDS
            and now - candidate.observed_at >= RESET_CONFIRM_MIN_AGE_SECONDS
        ):
            # Use the later of the two stable observations so the
            # four-minute safety buffer is never shortened by jitter.
            confirmed_reset = max(candidate.reset_at, reset_at)
            after = replace(
                state,
                reset_candidate=None,
                reset_anchor=confirmed_reset,
                last_known_reset=confirmed_reset,
                next_due_at=confirmed_reset + RESET_BUFFER_SECONDS,
            )
            return Transition(
                state, after, Decision.NO_CHANGE,
                f"far reset promoted after stable observations "
                f"old_reset={trusted_reset} new_reset={confirmed_reset} "
                f"first_seen={candidate.observed_at} confirmed_at={now}",
                after != state,
            ), SyncAction.CANDIDATE_PROMOTED
        if delta <= RESET_CONFIRM_MATCH_SECONDS:
            return _keep(
                state, SyncAction.CANDIDATE_PENDING,
                f"far reset awaiting independent confirmation "
                f"trusted_reset={trusted_reset} candidate={candidate.reset_at} "
                f"observed_at={candidate.observed_at} now={now}",
            ), SyncAction.CANDIDATE_PENDING

    after = replace(
        state,
        reset_candidate=ResetCandidate(reset_at, now),
        reset_anchor=anchor_reset,
    )
    return Transition(
        state, after, Decision.NO_CHANGE,
        f"far-later fresh reset deferred trusted_reset={trusted_reset} "
        f"candidate={reset_at} observed_at={now}; preserved "
        f"reset={_show(state.last_known_reset)} due={_show(state.next_due_at)}"
        f"{anchor_note}",
        after != state,
    ), SyncAction.CANDIDATE_CREATED


def evaluate_due(
    state: ProviderState,
    observation: QuotaObservation,
    now: int,
) -> Tuple[Transition, Decision]:
    """The full due decision for one provider: sync, then compare.

    Branch order is the P1-1 starvation fix and must not be reordered:

    1. a MATURED deadline is a committed debt and returns immediately — a
       fresh probe at due time may not re-anchor the window forward
       (reproduced live 2026-08-30: a probe at due time pushed 18:11:35
       to 23:15:38);
    2. only then may Fresh recalibrate an un-matured deadline;
    3. with still no usable deadline, seed the no-quota fallback from the
       last real task and persist it.

    Returns the net transition over the whole decision plus the verdict.
    """
    # 1. Matured debt protection — decide BEFORE any fresh sync.
    next_due = state.next_due_at
    if next_due is not None and now >= next_due:
        candidate_reset = valid_reset_at(observation, now)
        if candidate_reset is not None:
            reason = (
                f"matured debt due={next_due}; fresh candidate "
                f"reset={candidate_reset} ignored this round"
            )
        else:
            reason = f"matured debt due={next_due}; no valid fresh data"
        return (
            Transition(state, state, Decision.RUN_NOW, reason, False),
            Decision.RUN_NOW,
        )

    # 2. Fresh may recalibrate earlier or later only before the scheduled
    #    reset; the sync layer refuses writes during its four-minute buffer.
    synced, action = sync_deadline(state, observation, now)
    current = synced.after

    # 3. No usable deadline at all: seed the fallback from the last task.
    if current.next_due_at is None:
        seeded = replace(current, next_due_at=fallback_due(current, now))
        reason = (
            f"{synced.reason}; seeded no-quota fallback "
            f"due={seeded.next_due_at}"
        )
        due = now >= seeded.next_due_at
        return (
            Transition(
                state, seeded,
                Decision.RUN_NOW if due else Decision.WAIT,
                reason, seeded != state,
            ),
            Decision.RUN_NOW if due else Decision.WAIT,
        )

    due = now >= current.next_due_at
    return (
        Transition(
            state, current,
            Decision.RUN_NOW if due else Decision.WAIT,
            synced.reason,
            current != state,
        ),
        Decision.RUN_NOW if due else Decision.WAIT,
    )


def retry_blocked(state: ProviderState, now: int) -> Transition:
    """The no-write decision for a provider that owes a task.

    Expressed as a transition so the "nothing was written" claim is
    checkable, not just asserted in a comment.
    """
    return Transition(
        state, state, Decision.RETRY,
        "debt unpaid; normal due evaluation skipped", False,
    )


def _keep(state: ProviderState, action: SyncAction, reason: str) -> Transition:
    return Transition(state, state, Decision.NO_CHANGE, reason, False)


def _show(value: Optional[int]) -> str:
    return "<unset>" if value is None else str(value)


__all__ = [
    "RUN_INTERVAL_SECONDS",
    "RESET_BUFFER_SECONDS",
    "RESET_NEAR_MOVEMENT_SECONDS",
    "RESET_CONFIRM_MIN_AGE_SECONDS",
    "RESET_CONFIRM_MATCH_SECONDS",
    "MAX_WINDOW_FUTURE_SECONDS",
    "fallback_due",
    "current_trusted_reset",
    "schedule_block_reason",
    "valid_reset_at",
    "min_next_due",
    "retry_due",
    "begin_attempt",
    "record_attempt",
    "commit_success",
    "record_last_window",
    "sync_deadline",
    "evaluate_due",
    "retry_blocked",
    "NO_OBSERVATION",
]
