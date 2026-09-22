"""Command line for quota_sentinel.

TWO SURFACES, and the difference is a correctness property, not
decoration.

**Public operator verbs** are safe to run standalone. The lifecycle verbs
(``cutover``, ``rollback``, ``bootstrap-authority``) acquire the
scheduler's real ``run.lock`` through the same ``shlock`` protocol the
shell uses (see ``quota_sentinel.state.runlock``), so an operator cannot
interleave a backend switch with an in-flight ``check`` or model run. The
read verbs acquire nothing because they mutate nothing.

**Internal bridge verbs** (``scheduler-*``) are what ``quota-sentinel.sh``
calls *while it already holds run.lock*. They deliberately do NOT acquire
the lock themselves — the shell owns it, and a second acquisition from a
different PID would deadlock against its own caller. They are named
INTERNAL in their help for that reason: running one by hand bypasses the
serialization the whole design rests on. There is no way for a CLI to
verify that a caller holds a lock it did not take, so no such check is
faked here; the safety comes from the public surface not needing one.

Verbs, grouped by what they are allowed to do:

* AUTHORITY-AWARE reads — `next-due` and `state-dump` go through
  :class:`AuthoritativeStateStore`, so the shell's `status` display
  follows the durable ownership fact without knowing which backend is
  live. `dump` (legacy files) and `json-dump` (v1 documents) stay as
  explicit per-backend diagnostics; naming the backend is the whole
  point of those two.
* LOCK-SAFE lifecycle writes — `cutover`, `rollback` and
  `bootstrap-authority` perform the durable ownership changes described
  in quota_sentinel.state.cutover, each under a real run.lock this
  process acquires and releases itself. `cutover` and `rollback` always
  act on the WHOLE provider roster: the authority manifest is one global
  fact, so neither accepts a provider subset.
* `migrate` — the Phase 2 bootstrap that seeds shadow documents from the
  legacy slot files without overwriting anything.

Store and shell getters agree on every value project writers produce
(divergences on non-canonical/pathological content are registered by
tests/state-store-parity-regression.zsh) and the fallback is built into
the caller, so this can never change what status reports.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from quota_sentinel.state import (
    AuthoritativeStateStore,
    FileStateStore,
    ProviderState,
    ResetCandidate,
    StateStoreError,
    acquire_run_lock,
    bootstrap_legacy_authority,
    cutover_to_json,
    read_authority,
    rollback_to_legacy,
)
from quota_sentinel.state import runlock
from quota_sentinel.state.json_store import JsonStateStore
from quota_sentinel.state.migration import migrate_all
from quota_sentinel.scheduler import cli as scheduler_cli
from quota_sentinel.notifications import plan as notification_plan
from quota_sentinel.quota import cli as quota_cli


def default_state_dir() -> Path:
    env = os.environ.get("QUOTA_SENTINEL_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / "Library/Application Support/Quota-Sentinel"


def _show(value: object) -> str:
    if isinstance(value, ResetCandidate):
        return value.as_compound()
    return "unset" if value is None else str(value)


def run_next_due(store: AuthoritativeStateStore, provider: str) -> int:
    next_due = store.load(provider).next_due_at
    if next_due is None:
        return 1
    print(next_due)
    return 0


def _print_state_lines(state: ProviderState, provider: str) -> int:
    print(f"provider={provider}")
    print(f"last_attempt_at={_show(state.last_attempt_at)}")
    print(f"last_task_at={_show(state.last_task_at)}")
    print(f"next_due_at={_show(state.next_due_at)}")
    print(f"retry_pending={1 if state.retry_pending else 0}")
    print(f"last_known_reset={_show(state.last_known_reset)}")
    print(f"last_triggered_window={_show(state.last_triggered_window)}")
    print(f"reset_anchor={_show(state.reset_anchor)}")
    candidate = state.reset_candidate
    print(f"reset_candidate={candidate.as_compound() if candidate else 'unset'}")
    return 0


def run_dump(store: FileStateStore, provider: str) -> int:
    return _print_state_lines(store.load(provider), provider)


def run_authority(state_dir: Path) -> int:
    authority = read_authority(state_dir)
    print(f"backend={authority.backend}")
    print(f"epoch={authority.epoch}")
    return 0


def _lock_timeout(args: argparse.Namespace) -> float:
    return float(getattr(args, "lock_timeout", runlock.DEFAULT_LOCK_TIMEOUT_SECONDS))


def _lifecycle_lock(state_dir: Path, args: argparse.Namespace, what: str):
    """run.lock for a public lifecycle verb, with operator-facing waiting."""
    def announce() -> None:
        print(
            f"quota_sentinel: run.lock is busy; waiting up to "
            f"{_lock_timeout(args):.0f}s for the in-flight scheduler run "
            f"before {what}",
            file=sys.stderr,
        )

    return acquire_run_lock(
        state_dir, timeout=_lock_timeout(args), on_wait=announce
    )


def run_bootstrap_authority(state_dir: Path, args: argparse.Namespace) -> int:
    """Record a LEGACY ownership fact that the operator explicitly asserted.

    The mandatory flag is the whole point. A missing manifest means the
    system does not know who owns the state — it does not mean "legacy" —
    and the two situations are indistinguishable from the state directory
    alone. So the operator has to say it out loud, and the tool refuses
    otherwise.
    """
    if not args.assume_legacy:
        print(
            "quota_sentinel: refusing to bootstrap the authority manifest.\n"
            "\n"
            "A missing manifest means the owner is UNKNOWN, not that it is\n"
            "legacy: a deployment that already cut over and then lost the\n"
            "manifest looks exactly the same from here. Bootstrapping it as\n"
            "legacy would silently re-legitimize stale deadlines and retry\n"
            "debt the authoritative documents have moved past.\n"
            "\n"
            "If — and only if — you have confirmed this is a pre-protocol\n"
            "legacy deployment that never cut over, re-run with:\n"
            "\n"
            "    quota-sentinel bootstrap-authority --assume-legacy\n"
            "\n"
            "Otherwise the manifest was lost: restore it from backup rather\n"
            "than recreating it.",
            file=sys.stderr,
        )
        return 3
    with _lifecycle_lock(state_dir, args, "bootstrapping authority"):
        result = bootstrap_legacy_authority(state_dir)
    if result.created:
        print(
            f"authority bootstrapped as {result.authority.backend} "
            f"(epoch {result.authority.epoch}); this asserted that the "
            "deployment is a pre-protocol legacy one"
        )
    else:
        print(
            f"authority already present as {result.authority.backend} "
            f"(epoch {result.authority.epoch}); nothing written"
        )
    return 0


def run_cutover(state_dir: Path, args: argparse.Namespace) -> int:
    with _lifecycle_lock(state_dir, args, "cutover"):
        result = cutover_to_json(state_dir)
    if not result.changed:
        print(f"authority already {result.current.backend} "
              f"(epoch {result.current.epoch}); nothing written")
        return 0
    print(f"authority {result.previous.backend} -> {result.current.backend} "
          f"(epoch {result.current.epoch})")
    for provider in sorted(result.states):
        refreshed = "refreshed" if result.prepared.get(provider) else "already current"
        print(f"{provider}: document {refreshed}")
    return 0


def run_notification_plan(args: argparse.Namespace) -> int:
    """The selection policy behind one notification.

    Prints machine records, not prose: the shell reads ``layout`` and
    ``providers`` and keeps owning transport and rendering.
    """
    providers = args.providers or []
    if args.event == "task":
        plan = notification_plan.plan_task(providers)
    elif args.event == "usage":
        plan = notification_plan.plan_usage(providers)
    elif args.event == "recovery":
        plan = notification_plan.plan_recovery(providers)
    else:  # pragma: no cover - argparse constrains the choices
        print(f"quota_sentinel: unknown notification event {args.event!r}",
              file=sys.stderr)
        return 3
    if plan is None:
        # "Nothing to report" is a legitimate answer, not an error: the
        # caller must be able to distinguish it from a failed plan.
        print("layout=silent")
        print("providers=")
        print("reason=nothing to report")
        return 0
    print(f"layout={plan.layout.value}")
    print(f"providers={','.join(plan.providers)}")
    print(f"reason={plan.reason}")
    return 0


def run_rollback(state_dir: Path, args: argparse.Namespace) -> int:
    with _lifecycle_lock(state_dir, args, "rollback"):
        result = rollback_to_legacy(state_dir)
    if not result.changed:
        print(f"authority already {result.current.backend} "
              f"(epoch {result.current.epoch}); nothing written")
        return 0
    print(f"authority {result.previous.backend} -> {result.current.backend} "
          f"(epoch {result.current.epoch})")
    return 0


def run_json_dump(store: JsonStateStore, provider: str) -> int:
    return _print_state_lines(store.load(provider), provider)


def run_migrate(state_dir: Path, providers: Optional[List[str]]) -> int:
    """Seed shadow documents — legacy backend only.

    Refuses under JSON authority: a missing document there is corruption,
    and "repairing" it from the retired legacy files would silently
    resurrect state the authoritative document has moved past. That is the
    same failure mode the missing-manifest rule closes, one layer down.
    """
    authority = read_authority(state_dir)
    if authority.is_json:
        print(
            "quota_sentinel: refusing to seed shadow documents: the JSON "
            "backend is authoritative, so a missing document is corruption "
            "to investigate, not a bootstrap to re-run",
            file=sys.stderr,
        )
        return 4
    for provider, action in migrate_all(state_dir, providers).items():
        print(f"{provider}: {action}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Stable product prog: identical help text whether invoked as the
    # console script or `python -m quota_sentinel` (parity is pinned by
    # tests/uv-project-regression.py).
    parser = argparse.ArgumentParser(prog="quota-sentinel")
    parser.add_argument("--state-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("next-due", "print the provider's next_due_at epoch (rc 1 when unset)"),
        ("dump", "print all scheduler-state slots from the legacy slot files"),
        ("json-dump", "print all scheduler-state slots from the v1 JSON document"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("provider")
    sub.add_parser(
        "authority",
        help="print the durable authoritative backend and its epoch",
    )
    # Public lifecycle verbs. Each acquires the scheduler's real run.lock
    # (same shlock protocol, same file) for the duration of the switch, and
    # each operates on the WHOLE provider roster — the authority manifest is
    # one global fact, so no verb here accepts a provider subset. Passing one
    # is an argparse usage error, before any state is touched.
    for name, help_text, handler in (
        ("cutover",
         "make the JSON backend authoritative for the whole roster "
         "(acquires run.lock)",
         run_cutover),
        ("rollback",
         "return ownership to the legacy backend for the whole roster; "
         "pure undo only (acquires run.lock)",
         run_rollback),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--lock-timeout", type=float,
            default=runlock.DEFAULT_LOCK_TIMEOUT_SECONDS,
            help="seconds to wait for an in-flight scheduler run "
                 "(default: %(default)s)",
        )
        command.set_defaults(
            handler=lambda a, h=handler: h(a.state_dir, a)
        )
    bootstrap = sub.add_parser(
        "bootstrap-authority",
        help="ONE-TIME, and only for a confirmed pre-protocol legacy "
             "deployment: record the legacy ownership fact "
             "(requires --assume-legacy; acquires run.lock)",
    )
    bootstrap.add_argument(
        "--assume-legacy", action="store_true",
        help="assert that this deployment predates the authority protocol "
             "and never cut over. Required: a missing manifest means the "
             "owner is unknown, not legacy.",
    )
    bootstrap.add_argument(
        "--lock-timeout", type=float,
        default=runlock.DEFAULT_LOCK_TIMEOUT_SECONDS,
        help="seconds to wait for an in-flight scheduler run "
             "(default: %(default)s)",
    )
    bootstrap.set_defaults(
        handler=lambda a: run_bootstrap_authority(a.state_dir, a)
    )
    state_dump = sub.add_parser(
        "state-dump",
        help="print scheduler slots from whichever backend is authoritative",
    )
    state_dump.add_argument("provider")
    notify = sub.add_parser(
        "notification-plan",
        help="the selection policy for one notification (layout + roster)",
    )
    notify.add_argument(
        "--event", required=True, choices=("task", "usage", "recovery")
    )
    notify.add_argument("providers", nargs="*", default=None)
    notify.set_defaults(handler=run_notification_plan)

    migrate = sub.add_parser(
        "migrate",
        help="seed shadow JSON documents from legacy slot files "
             "(existing documents always win; slot files untouched)",
    )
    migrate.add_argument("providers", nargs="*", default=None,
                         help="override the default provider roster")
    # Scheduler-domain verbs (Phase 3C bridge). Registered from their own
    # module so the parser stays a table of contents, not a second API.
    scheduler_cli.register(sub)
    quota_cli.register(sub)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = args.state_dir or default_state_dir()
    args.state_dir = state_dir
    try:
        handler = getattr(args, "handler", None)
        if handler is not None:
            return handler(args)
        if args.command == "next-due":
            return run_next_due(AuthoritativeStateStore(state_dir), args.provider)
        if args.command == "state-dump":
            return run_dump(AuthoritativeStateStore(state_dir), args.provider)
        if args.command == "dump":
            return run_dump(FileStateStore(state_dir), args.provider)
        if args.command == "json-dump":
            return run_json_dump(JsonStateStore(state_dir), args.provider)
        if args.command == "authority":
            return run_authority(state_dir)
        return run_migrate(state_dir, args.providers or None)
    except StateStoreError as exc:
        print(f"quota_sentinel: {exc}", file=sys.stderr)
        return 4
    except ValueError as exc:
        # e.g. an invalid provider name: a concise typed CLI error, not a
        # traceback. The LIBRARY keeps raising ValueError (it is a
        # programmer error); only the CLI edge converts it.
        print(f"quota_sentinel: invalid argument: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
