"""Scheduler I/O boundary: load, decide, commit (Phase 3C).

``policy`` is pure and knows nothing about disks; this module is the thin
layer that loads an authoritative state, hands it to the policy, and
commits the resulting transition through the router. It is deliberately
the ONLY place the two meet, so "which backend" and "what changed" never
get entangled.

Concurrency contract (unchanged from Phases 1-3A): every caller that
mutates scheduler state must hold the shell's ``run.lock``. The stores
provide defensive stale detection, not compare-and-swap; the lock is the
serialization. Readers (``next_due``, ``retry_due_providers``) do not need
it because they only read, and the router's generation guard already
protects them against a cutover landing mid-read.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..state import AuthorityError, AuthoritativeStateStore, BackendAuthority
from ..state.migration import DEFAULT_PROVIDERS
from ..state.models import ProviderState
from . import policy
from .models import Decision, QuotaObservation, Transition


@dataclass(frozen=True)
class DecisionResult:
    """One applied decision: the verdict, the state, and the durable owner."""

    provider: str
    decision: Decision
    reason: str
    changed: bool
    before_state: ProviderState
    state: ProviderState
    backend: str

    def as_lines(self) -> List[str]:
        return [
            f"decision={self.decision.value}",
            f"changed={1 if self.changed else 0}",
            f"backend={self.backend}",
            f"reason={self.reason}",
        ]


def router(state_dir: Path) -> AuthoritativeStateStore:
    return AuthoritativeStateStore(Path(state_dir))


def load_state(state_dir: Path, provider: str) -> ProviderState:
    return router(state_dir).load(provider)


def load_roster(
    state_dir: Path, providers: Optional[Sequence[str]] = None
) -> Dict[str, ProviderState]:
    roster = list(providers) if providers else list(DEFAULT_PROVIDERS)
    return router(state_dir).load_all(roster)


def apply_transition(
    state_dir: Path, provider: str, transition: Transition
) -> str:
    """Commit a transition if it changed anything; return the backend name.

    A no-op transition writes nothing at all — not even a re-publish of
    identical bytes — so "the policy concluded no change" is mechanically
    observable rather than a claim about what the bytes look like.
    """
    store = router(state_dir)
    if transition.writes:
        authority = store.commit(
            provider, transition.before, transition.after, transition.publish
        )
        return authority.backend
    return store.authority().backend


def decide_due(
    state_dir: Path,
    provider: str,
    now: int,
    observation: QuotaObservation,
) -> DecisionResult:
    """The full due decision for one provider, applied."""
    store = router(state_dir)
    state = store.load(provider)
    transition, decision = policy.evaluate_due(state, observation, now)
    backend = apply_transition(state_dir, provider, transition)
    return DecisionResult(
        provider=provider,
        decision=decision,
        reason=transition.reason,
        changed=transition.changed,
        before_state=transition.before,
        state=transition.after,
        backend=backend,
    )


def begin_attempt(state_dir: Path, provider: str, now: int) -> DecisionResult:
    store = router(state_dir)
    state = store.load(provider)
    transition = policy.begin_attempt(state, now)
    backend = apply_transition(state_dir, provider, transition)
    return DecisionResult(
        provider, Decision.RETRY, transition.reason, transition.changed,
        transition.before, transition.after, backend,
    )


def record_attempt(state_dir: Path, provider: str, now: int) -> DecisionResult:
    store = router(state_dir)
    state = store.load(provider)
    transition = policy.record_attempt(state, now)
    backend = apply_transition(state_dir, provider, transition)
    return DecisionResult(
        provider, Decision.RETRY, transition.reason, transition.changed,
        transition.before, transition.after, backend,
    )


def commit_success(state_dir: Path, provider: str, now: int) -> DecisionResult:
    store = router(state_dir)
    state = store.load(provider)
    transition = policy.commit_success(state, now)
    backend = apply_transition(state_dir, provider, transition)
    return DecisionResult(
        provider, Decision.NO_CHANGE, transition.reason, transition.changed,
        transition.before, transition.after, backend,
    )


def record_last_window(
    state_dir: Path, provider: str, reset_at: int
) -> DecisionResult:
    store = router(state_dir)
    state = store.load(provider)
    transition = policy.record_last_window(state, reset_at)
    backend = apply_transition(state_dir, provider, transition)
    return DecisionResult(
        provider, Decision.NO_CHANGE, transition.reason, transition.changed,
        transition.before, transition.after, backend,
    )


def retry_due_providers(
    state_dir: Path,
    providers: Optional[Sequence[str]],
    now: int,
    gap_seconds: int,
) -> List[str]:
    """Providers whose unpaid debt may start another watchdog burst."""
    states = load_roster(state_dir, providers)
    return [
        provider
        for provider, state in states.items()
        if policy.retry_due(state, now, gap_seconds)
    ]


def pending_providers(
    state_dir: Path, providers: Optional[Sequence[str]]
) -> List[str]:
    states = load_roster(state_dir, providers)
    return [p for p, s in states.items() if s.retry_pending]


def next_due(
    state_dir: Path, providers: Optional[Sequence[str]]
) -> Optional[int]:
    return policy.min_next_due(load_roster(state_dir, providers))


__all__ = [
    "AuthorityError",
    "DecisionResult",
    "router",
    "load_state",
    "load_roster",
    "apply_transition",
    "decide_due",
    "begin_attempt",
    "record_attempt",
    "commit_success",
    "record_last_window",
    "retry_due_providers",
    "pending_providers",
    "next_due",
]
