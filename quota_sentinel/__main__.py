"""Thin CLI for the Python state store (strangler seam).

Reads (`next-due`, `dump`, `json-dump`) plus ONE explicit bootstrap write:
`migrate` seeds shadow v1 JSON documents from the shell's per-slot files
(never overwriting existing documents; the slot files themselves are left
untouched). The shell remains the sole writer of AUTHORITATIVE scheduler
state; JSON documents are shadow snapshots until a cutover phase.

The shell's `status` verb routes its next-due display through `next-due`;
store and shell getters agree on every value project writers produce
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
    FileStateStore,
    ProviderState,
    ResetCandidate,
    StateStoreError,
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


def run_next_due(store: FileStateStore, provider: str) -> int:
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


def run_json_dump(store: JsonStateStore, provider: str) -> int:
    return _print_state_lines(store.load(provider), provider)


def run_migrate(state_dir: Path, providers: Optional[List[str]]) -> int:
    for provider, action in migrate_all(state_dir, providers).items():
        print(f"{provider}: {action}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m quota_sentinel")
    parser.add_argument("--state-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("next-due", "print the provider's next_due_at epoch (rc 1 when unset)"),
        ("dump", "print all scheduler-state slots from the legacy slot files"),
        ("json-dump", "print all scheduler-state slots from the v1 JSON document"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("provider")
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
            return run_next_due(FileStateStore(state_dir), args.provider)
        if args.command == "dump":
            return run_dump(FileStateStore(state_dir), args.provider)
        if args.command == "json-dump":
            return run_json_dump(JsonStateStore(state_dir), args.provider)
        return run_migrate(state_dir, args.providers or None)
    except StateStoreError as exc:
        print(f"quota_sentinel: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
