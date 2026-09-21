"""Scheduler domain: pure policy, durable transitions, and the seam the
shell calls.

* ``policy`` — pure transitions (``new_state = f(old_state, inputs, now)``).
* ``models`` — the vocabulary (Decision, QuotaObservation, Transition).
* ``observation`` — reading one normalised quota probe.
* ``service`` — the I/O boundary that pairs the policy with the
  authoritative state router.

The shell owns processes, locks, model execution and notifications; it
holds no scheduler policy of its own. Where the shell still performs a
loop, it asks this package for each decision and each transition.
"""
from __future__ import annotations

from .models import (
    NO_OBSERVATION,
    Decision,
    QuotaObservation,
    RunOutcome,
    SyncAction,
    Transition,
)
from .observation import observation_for, read_normalized_quota
from .policy import (
    MAX_WINDOW_FUTURE_SECONDS,
    RESET_BUFFER_SECONDS,
    RESET_CONFIRM_MATCH_SECONDS,
    RESET_CONFIRM_MIN_AGE_SECONDS,
    RESET_NEAR_MOVEMENT_SECONDS,
    RUN_INTERVAL_SECONDS,
    begin_attempt,
    commit_success,
    current_trusted_reset,
    evaluate_due,
    fallback_due,
    min_next_due,
    record_attempt,
    record_last_window,
    retry_blocked,
    retry_due,
    schedule_block_reason,
    sync_deadline,
    valid_reset_at,
)
from .service import (
    DecisionResult,
    apply_transition,
    commit_success as commit_success_state,
    decide_due,
    ensure_authority,
    load_roster,
    load_state,
    next_due,
    pending_providers,
    record_attempt as record_attempt_state,
    record_last_window as record_last_window_state,
    retry_due_providers,
    router,
)

__all__ = [
    "Decision",
    "SyncAction",
    "QuotaObservation",
    "NO_OBSERVATION",
    "Transition",
    "RunOutcome",
    "observation_for",
    "read_normalized_quota",
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
    "DecisionResult",
    "router",
    "load_state",
    "load_roster",
    "apply_transition",
    "decide_due",
    "commit_success_state",
    "record_attempt_state",
    "record_last_window_state",
    "retry_due_providers",
    "pending_providers",
    "next_due",
    "ensure_authority",
]
