"""Shell-facing scheduler CLI (Phase 3C bridge).

Every verb here is a thin wrapper over ``quota_sentinel.scheduler``, and
every one of them is a place the shell used to hold policy of its own. The
bridge is deliberately small and boring: parse flags, call the domain,
print one ``key=value`` per line, return a meaningful exit code.

EXIT CODES ARE PART OF THE CONTRACT. The shell decides with ``if`` and
``case``, so the codes reproduce the pre-migration shell functions exactly:

    scheduler-valid-reset   0 found (prints it) | 1 no valid reset
    scheduler-sync          0 applied | 1 no valid quota | 2 blocked
                            | 3 deferred (candidate recorded or pending)
    scheduler-decide        0 due now | 1 wait
    everything else         0 ok | 1 "no answer" | 3 bad argument | 4 error

``state-change`` lines are emitted in the store's canonical SLOTS order
and only for the slots the shell logged before this migration, so the run
log keeps its existing shape byte for byte.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from ..state.models import ProviderState, ResetCandidate
from ..state.migration import DEFAULT_PROVIDERS

DEFAULT_PROVIDERS_LIST = list(DEFAULT_PROVIDERS)
from . import policy, service
from .models import SyncAction

# Slots whose transitions the shell logged, in the store's canonical order.
LOGGED_SLOTS = ("last_attempt_at", "retry_pending", "next_due_at")

# Mirror of the shell's ``sync_provider_deadline_from_quota`` return codes.
SYNC_EXIT = {
    SyncAction.NO_VALID_QUOTA: 1,
    SyncAction.BLOCKED: 2,
    SyncAction.ANCHOR_ESTABLISHED: 0,
    SyncAction.NEAR_MOVEMENT: 0,
    SyncAction.CANDIDATE_PROMOTED: 0,
    SyncAction.CANDIDATE_PENDING: 3,
    SyncAction.CANDIDATE_CREATED: 3,
}

# Which branches logged in the shell, and at which level. NEAR_MOVEMENT and
# NO_VALID_QUOTA were silent; CANDIDATE_CREATED was a warning.
SYNC_LOG_LEVEL = {
    SyncAction.BLOCKED: "info",
    SyncAction.ANCHOR_ESTABLISHED: "info",
    SyncAction.CANDIDATE_PROMOTED: "info",
    SyncAction.CANDIDATE_PENDING: "info",
    SyncAction.CANDIDATE_CREATED: "warn",
}


def _display(value: object) -> str:
    """Shell-display form of a slot value. Empty means unset."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, ResetCandidate):
        return value.as_compound()
    return str(value)


def _emit_changes(provider: str, before: ProviderState, after: ProviderState) -> None:
    """The canonical change log, restricted to the slots the shell logged."""
    for slot in LOGGED_SLOTS:
        old = getattr(before, slot)
        new = getattr(after, slot)
        if old == new:
            continue
        print(f"change\t{provider}\t{slot}\t{_display(old)}\t{_display(new)}")


def _emit_result(result: service.DecisionResult, *, log: bool = False) -> None:
    """Transition verbs: backend, the change log, and (optionally) a reason.

    ``before_state`` comes from the same load/commit pair as ``state``
    rather than from a second read — a re-read could observe a different
    value and print a diff that never happened.

    ``log`` is False for the verbs whose shell predecessors logged nothing
    but the value diff (attempt bookkeeping, window bookkeeping). Emitting
    a reason line there would add log lines the migration is not allowed
    to invent.
    """
    print(f"backend={result.backend}")
    print(f"decision={result.decision.value}")
    print(f"changed={1 if result.changed else 0}")
    _emit_changes(result.provider, result.before_state, result.state)
    if log:
        print(f"log=info\t{result.reason}")


def _roster(values: Optional[Sequence[str]]) -> List[str]:
    return list(values) if values else list(DEFAULT_PROVIDERS)


def _add_now(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--now", type=int, required=True)


def _add_quota(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--quota-file", type=Path, default=None)
    parser.add_argument(
        "--fresh", type=int, choices=(0, 1), default=None,
        help="the caller's freshness verdict for this probe",
    )


def _observation(args: argparse.Namespace):
    from .observation import observation_for

    fresh = None if args.fresh is None else bool(args.fresh)
    return observation_for(args.quota_file, fresh)


def _log_lines(action: Optional[SyncAction], reason: str) -> None:
    level = SYNC_LOG_LEVEL.get(action) if action is not None else "info"
    if level is None:
        return
    print(f"log={level}\t{reason}")


# ---------------------------------------------------------------------------
# verbs
# ---------------------------------------------------------------------------
def cmd_config(args: argparse.Namespace) -> int:
    """Scheduler policy constants, so the shell holds no second copy."""
    import os

    def env_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or not raw.strip().isdigit():
            return default
        return int(raw)

    values = {
        "RUN_INTERVAL_SECONDS": policy.RUN_INTERVAL_SECONDS,
        "RESET_BUFFER_SECONDS": policy.RESET_BUFFER_SECONDS,
        "RESET_NEAR_MOVEMENT_SECONDS": policy.RESET_NEAR_MOVEMENT_SECONDS,
        "RESET_CONFIRM_MIN_AGE_SECONDS": policy.RESET_CONFIRM_MIN_AGE_SECONDS,
        "RESET_CONFIRM_MATCH_SECONDS": policy.RESET_CONFIRM_MATCH_SECONDS,
        "MAX_WINDOW_FUTURE_SECONDS": policy.MAX_WINDOW_FUTURE_SECONDS,
        "RETRY_INTERVAL_SECONDS": env_int("QUOTA_SENTINEL_RETRY_INTERVAL", 30),
        "INITIAL_ATTEMPT_LIMIT": env_int("QUOTA_SENTINEL_INITIAL_ATTEMPTS", 3),
        "WATCHDOG_ATTEMPT_LIMIT": env_int("QUOTA_SENTINEL_WATCHDOG_ATTEMPTS", 2),
        "WATCHDOG_RETRY_GAP_SECONDS": env_int(
            "QUOTA_SENTINEL_WATCHDOG_RETRY_GAP", 780
        ),
    }
    for key in sorted(values):
        print(f"{key}={values[key]}")
    return 0


def cmd_ensure_authority(args: argparse.Namespace) -> int:
    authority = service.ensure_authority(args.state_dir)
    print(f"backend={authority.backend}")
    print(f"epoch={authority.epoch}")
    print(f"log=info\tauthoritative backend is {authority.backend} "
          f"(epoch {authority.epoch})")
    return 0


def cmd_valid_reset(args: argparse.Namespace) -> int:
    reset_at = policy.valid_reset_at(_observation(args), args.now)
    if reset_at is None:
        return 1
    print(reset_at)
    return 0


def cmd_fallback_due(args: argparse.Namespace) -> int:
    state = service.load_state(args.state_dir, args.provider)
    print(policy.fallback_due(state, args.now))
    return 0


def cmd_block_reason(args: argparse.Namespace) -> int:
    state = service.load_state(args.state_dir, args.provider)
    reason = policy.schedule_block_reason(state, args.now)
    if reason is None:
        return 1
    print(reason)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    state = service.load_state(args.state_dir, args.provider)
    transition, action = policy.sync_deadline(state, _observation(args), args.now)
    backend = service.apply_transition(args.state_dir, args.provider, transition)
    print(f"provider={args.provider}")
    print(f"backend={backend}")
    print(f"action={action.value}")
    print(f"changed={1 if transition.changed else 0}")
    _emit_changes(args.provider, transition.before, transition.after)
    _log_lines(action, transition.reason)
    return SYNC_EXIT[action]


def cmd_decide(args: argparse.Namespace) -> int:
    # A provider that owes a task is not evaluated at all: a fresh probe must
    # never push a due-but-unsucceeded task into the future. Folding this into
    # the decide verb (rather than asking "is it pending?" first) keeps the
    # whole per-provider decision in one authoritative read.
    state = service.load_state(args.state_dir, args.provider)
    if state.retry_pending:
        blocked = policy.retry_blocked(state, args.now)
        print(f"provider={args.provider}")
        print(f"backend={service.router(args.state_dir).authority().backend}")
        print("decision=retry")
        print("changed=0")
        # Deliberately NOT a log record: the caller already logs this case in
        # its own words, and the migration must not invent log lines.
        print(f"reason={blocked.reason}")
        return 2
    result = service.decide_due(
        args.state_dir, args.provider, args.now, _observation(args)
    )
    print(f"provider={args.provider}")
    print(f"backend={result.backend}")
    print(f"decision={result.decision.value}")
    print(f"changed={1 if result.changed else 0}")
    _emit_changes(result.provider, result.before_state, result.state)
    print(f"log=info\t{result.reason}")
    return 0 if result.decision.value == "run-now" else 1


def _each(handler, args, providers) -> int:
    """Apply one transition per provider, in the order given.

    The burst verbs take several providers because the shell launches their
    attempts in parallel and a per-provider round trip would add a process
    spawn to a hot path that used to be a file write. One transition per
    provider is still one whole-document commit per provider — batching the
    PROCESS, never the transaction.
    """
    for provider in providers:
        print(f"provider={provider}")
        handler(args, provider)
    return 0


def cmd_begin_attempt(args: argparse.Namespace) -> int:
    return _each(
        lambda a, p: _emit_result(service.begin_attempt(a.state_dir, p, a.now)),
        args, args.providers,
    )


def cmd_record_attempt(args: argparse.Namespace) -> int:
    return _each(
        lambda a, p: _emit_result(service.record_attempt(a.state_dir, p, a.now)),
        args, args.providers,
    )


def cmd_commit_success(args: argparse.Namespace) -> int:
    return _each(
        lambda a, p: _emit_result(
            service.commit_success(a.state_dir, p, a.now), log=True
        ),
        args, args.providers,
    )


def cmd_last_window(args: argparse.Namespace) -> int:
    _emit_result(
        service.record_last_window(args.state_dir, args.provider, args.reset)
    )
    return 0


def _parse_pairs(values, label: str) -> dict:
    """``--flag provider=value`` pairs, validated into a plain dict."""
    result = {}
    for item in values or ():
        if "=" not in item:
            raise SystemExit(f"{label} expects provider=value, got {item!r}")
        provider, _, value = item.partition("=")
        result[provider] = value
    return result


def cmd_decide_all(args: argparse.Namespace) -> int:
    """Phase C for the whole roster in ONE bridge process.

    Each provider still gets its own transition and its own whole-document
    commit; only the process spawn is shared. The alternative — one call per
    provider — costs a fixed ~60ms of interpreter start-up on a path the
    precision timer walks every minute.
    """
    from .observation import observation_for

    quotas = _parse_pairs(args.quota, "--quota")
    fresh = _parse_pairs(args.fresh, "--fresh")
    for provider in args.providers:
        quota_file = quotas.get(provider)
        fresh_value = fresh.get(provider)
        print(f"provider={provider}")
        state = service.load_state(args.state_dir, provider)
        if state.retry_pending:
            blocked = policy.retry_blocked(state, args.now)
            print("decision=retry")
            print("changed=0")
            print(f"reason={blocked.reason}")
            print(f"end={provider}")
            continue
        observation = observation_for(
            Path(quota_file) if quota_file else None,
            None if fresh_value is None else fresh_value == "1",
        )
        result = service.decide_due(args.state_dir, provider, args.now, observation)
        print(f"decision={result.decision.value}")
        print(f"changed={1 if result.changed else 0}")
        _emit_changes(provider, result.before_state, result.state)
        print(f"log=info\t{result.reason}")
        print(f"end={provider}")
    return 0


def cmd_retry_due(args: argparse.Namespace) -> int:
    providers = (
        [args.provider] if args.provider else _roster(args.providers)
    )
    for provider in service.retry_due_providers(
        args.state_dir, providers, args.now, args.gap
    ):
        print(provider)
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    providers = [args.provider] if args.provider else _roster(args.providers)
    for provider in service.pending_providers(args.state_dir, providers):
        print(provider)
    return 0


def cmd_deadlines(args: argparse.Namespace) -> int:
    """Per-provider deadlines for the "nothing due" log line, in one call."""
    states = service.load_roster(args.state_dir, _roster(args.providers))
    for provider, state in states.items():
        print(f"{provider}\t{state.next_due_at if state.next_due_at is not None else ''}")
    return 0


def cmd_next_due(args: argparse.Namespace) -> int:
    value = service.next_due(args.state_dir, _roster(args.providers))
    if value is None:
        return 1
    print(value)
    return 0


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def register(sub: argparse._SubParsersAction) -> None:
    """Attach the scheduler verbs to the top-level parser."""
    config = sub.add_parser("scheduler-config", help="print scheduler policy constants")
    config.set_defaults(handler=cmd_config)

    ensure = sub.add_parser(
        "scheduler-ensure-authority",
        help="cut over to the JSON backend if that has not happened yet",
    )
    ensure.set_defaults(handler=cmd_ensure_authority)

    valid = sub.add_parser("scheduler-valid-reset", help="plausible fresh reset, if any")
    valid.add_argument("--provider", required=True)
    _add_now(valid)
    _add_quota(valid)
    valid.set_defaults(handler=cmd_valid_reset)

    fallback = sub.add_parser("scheduler-fallback-due", help="no-quota fallback deadline")
    fallback.add_argument("--provider", required=True)
    _add_now(fallback)
    fallback.set_defaults(handler=cmd_fallback_due)

    block = sub.add_parser("scheduler-block-reason", help="why writes are blocked")
    block.add_argument("--provider", required=True)
    _add_now(block)
    block.set_defaults(handler=cmd_block_reason)

    sync = sub.add_parser("scheduler-sync", help="recalibrate a deadline from quota")
    sync.add_argument("--provider", required=True)
    _add_now(sync)
    _add_quota(sync)
    sync.set_defaults(handler=cmd_sync)

    decide = sub.add_parser("scheduler-decide", help="the due decision for one provider")
    decide.add_argument("--provider", required=True)
    _add_now(decide)
    _add_quota(decide)
    decide.set_defaults(handler=cmd_decide)

    for name, handler, helptext in (
        ("scheduler-begin-attempt", cmd_begin_attempt,
         "raise the debt and stamp the attempt before the model runs"),
        ("scheduler-record-attempt", cmd_record_attempt,
         "stamp a later attempt of the same burst"),
        ("scheduler-commit-success", cmd_commit_success,
         "commit a verified model success"),
    ):
        parser = sub.add_parser(name, help=helptext)
        parser.add_argument(
            "--provider", action="append", required=True, dest="providers"
        )
        _add_now(parser)
        parser.set_defaults(handler=handler)

    window = sub.add_parser("scheduler-last-window", help="record the window a run belonged to")
    window.add_argument("--provider", required=True)
    window.add_argument("--reset", type=int, required=True)
    window.set_defaults(handler=cmd_last_window)

    retry = sub.add_parser("scheduler-retry-due", help="providers whose debt may burst now")
    retry.add_argument("--provider", default=None)
    retry.add_argument("providers", nargs="*", default=None)
    _add_now(retry)
    retry.add_argument("--gap", type=int, required=True)
    retry.set_defaults(handler=cmd_retry_due)

    pending = sub.add_parser("scheduler-pending", help="providers carrying an unpaid debt")
    pending.add_argument("--provider", default=None)
    pending.add_argument("providers", nargs="*", default=None)
    pending.set_defaults(handler=cmd_pending)

    decide_all = sub.add_parser(
        "scheduler-decide-all", help="the due decision for a whole roster"
    )
    _add_now(decide_all)
    decide_all.add_argument("--quota", action="append", default=None)
    decide_all.add_argument("--fresh", action="append", default=None)
    decide_all.add_argument("providers", nargs="*", default=DEFAULT_PROVIDERS_LIST,
                             help="roster to decide (defaults to the full roster)")
    decide_all.set_defaults(handler=cmd_decide_all)

    deadlines = sub.add_parser(
        "scheduler-deadlines", help="per-provider deadlines for the run log"
    )
    deadlines.add_argument("providers", nargs="*", default=None)
    deadlines.set_defaults(handler=cmd_deadlines)

    nxt = sub.add_parser("scheduler-next-due", help="earliest schedulable deadline")
    nxt.add_argument("providers", nargs="*", default=None)
    nxt.set_defaults(handler=cmd_next_due)


__all__ = ["register", "LOGGED_SLOTS", "SYNC_EXIT", "SYNC_LOG_LEVEL"]
