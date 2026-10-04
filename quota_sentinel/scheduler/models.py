"""Scheduler domain models (Phase 3C).

The vocabulary the scheduler reasons in. Deliberately small: these are the
nouns the shell's decision code already used implicitly, named so the
policy can be written as pure transitions instead of scattered writes.

Nothing here performs I/O. ``ProviderState`` (the durable shape) lives in
``quota_sentinel.state``; ``QuotaObservation`` is this layer's view of one
quota probe, and the adapters that produce it are Phase 4's business.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from ..state.models import ProviderState, ResetCandidate


class Decision(Enum):
    """What the scheduler concluded for one provider at one instant.

    Values are log-facing strings, not control flow: the transition
    functions return them so the caller can decide what to do, and the
    observable behavior of a transition never depends on the enum member
    being compared anywhere.
    """

    RUN_NOW = "run-now"          # deadline has matured: the task is due
    WAIT = "wait"                # a future deadline holds the task back
    RETRY = "retry"              # an unpaid debt still owns this provider
    BLOCKED = "blocked"          # writes refused until a success commits
    NO_CHANGE = "no-change"      # inputs carried no information for us


class SyncAction(Enum):
    """Which branch of the deadline-synchronisation policy fired.

    One member per branch of the shell's ``sync_provider_deadline_from_quota``,
    kept distinct because the branches are what an operator reads in the
    log to understand why a deadline moved (or refused to).
    """

    NO_VALID_QUOTA = "no-valid-quota"        # stale/missing/implausible probe
    BLOCKED = "blocked"                      # matured debt / reset buffer
    ANCHOR_ESTABLISHED = "anchor-established"  # first Fresh reset of a generation
    NEAR_MOVEMENT = "near-movement"          # jitter inside the anchor tolerance
    CANDIDATE_PROMOTED = "candidate-promoted"  # far reset confirmed independently
    CANDIDATE_PENDING = "candidate-pending"  # seen twice, too soon to confirm
    CANDIDATE_CREATED = "candidate-created"  # first far observation, recorded


@dataclass(frozen=True)
class QuotaObservation:
    """One quota probe, already normalised by a provider adapter.

    ``fresh`` is the trust gate: only a Fresh observation may recalibrate a
    deadline. Cache and snapshot fallbacks deliberately arrive here with
    ``fresh=False`` so they can be displayed but never scheduled from.

    ``reset_at`` is the five-hour window reset the observation describes,
    or ``None`` when the probe did not carry a usable one. Providers that
    expose extra windows (OpenCode's monthly) keep them out of this type:
    the scheduler only ever deadlines on the five-hour window, and a
    display-only window must not be able to leak into deadline policy.
    """

    fresh: bool = False
    reset_at: Optional[int] = None
    source: str = "unknown"

    @property
    def usable(self) -> bool:
        return self.fresh and self.reset_at is not None


NO_OBSERVATION = QuotaObservation()


@dataclass(frozen=True)
class Transition:
    """One pure state transition: what changed, why, and what it decided.

    ``before``/``after`` are complete domain states, so a caller can commit
    them through any backend without re-deriving the delta. ``changed`` is
    False whenever the policy concluded "nothing to write", which is how
    the no-mutation branches (stale quota, blocked writes) stay provably
    non-writing rather than accidentally writing identical bytes.
    """

    before: ProviderState
    after: ProviderState
    decision: Decision
    reason: str
    changed: bool
    # Slots to materialize even when the value is unchanged. Empty for every
    # transition whose durable contract is "the value differs or nothing is
    # written"; the success commit is the one exception, because its
    # predecessor always left an explicit retry_pending=0 on disk.
    publish: Sequence[str] = ()

    @property
    def is_noop(self) -> bool:
        return not self.writes

    @property
    def writes(self) -> bool:
        """Whether this transition must touch the durable backend at all."""
        return self.changed or bool(self.publish)


@dataclass(frozen=True)
class RunOutcome:
    """The result of one model attempt, as the runner reports it.

    ``succeeded`` is the ONLY input to a success commit. Everything else
    (timeouts, non-zero exits, killed processes) is a failed attempt, and
    a failed attempt must never advance ``last_task_at`` or reseed the
    fallback deadline.
    """

    provider: str
    succeeded: bool
    exit_code: int = 0
    timed_out: bool = False


__all__ = [
    "ProviderState",
    "ResetCandidate",
    "Decision",
    "SyncAction",
    "QuotaObservation",
    "NO_OBSERVATION",
    "Transition",
    "RunOutcome",
]
