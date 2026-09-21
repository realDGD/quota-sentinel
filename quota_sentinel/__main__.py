"""Thin CLI for the Python state store (strangler seam).

Verbs, grouped by what they are allowed to do:

* AUTHORITY-AWARE reads — `next-due` and `state-dump` go through
  :class:`AuthoritativeStateStore`, so the shell's `status` display
  follows the durable ownership fact without knowing which backend is
  live. `dump` (legacy files) and `json-dump` (v1 documents) stay as
  explicit per-backend diagnostics; naming the backend is the whole
  point of those two.
* AUTHORITY-AWARE writes — `cutover` and `rollback` perform the durable
  ownership switch described in quota_sentinel.state.cutover. Both
  require the caller to hold the shell's run.lock (a contract this CLI
  cannot verify and does not pretend to).
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
    cutover_to_json,
    read_authority,
    rollback_to_legacy,
)
from quota_sentinel.state.json_store import JsonStateStore
from quota_sentinel.state.migration import migrate_all


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


def run_cutover(state_dir: Path, providers: Optional[List[str]]) -> int:
    result = cutover_to_json(state_dir, providers or None)
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


def run_rollback(state_dir: Path, providers: Optional[List[str]]) -> int:
    result = rollback_to_legacy(state_dir, providers or None)
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
    for name, help_text in (
        ("cutover", "make the JSON backend authoritative (caller holds run.lock)"),
        ("rollback", "return ownership to the legacy backend (pure undo only)"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("providers", nargs="*", default=None,
                             help="override the default provider roster")
    state_dump = sub.add_parser(
        "state-dump",
        help="print scheduler slots from whichever backend is authoritative",
    )
    state_dump.add_argument("provider")
    migrate = sub.add_parser(
        "migrate",
        help="seed shadow JSON documents from legacy slot files "
             "(existing documents always win; slot files untouched)",
    )
    migrate.add_argument("providers", nargs="*", default=None,
                         help="override the default provider roster")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = args.state_dir or default_state_dir()
    try:
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
        if args.command == "cutover":
            return run_cutover(state_dir, args.providers or None)
        if args.command == "rollback":
            return run_rollback(state_dir, args.providers or None)
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
