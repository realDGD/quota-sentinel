"""Thin read-only CLI for the Python state store (Phase 1 strangler seam).

Only reads are exposed: the shell remains the sole writer of scheduler
state until the scheduler-policy phase. The shell's `status` verb routes
its next-due display through `next-due` to prove the store's read
semantics match the shell getters exactly (fallback is built into the
caller so this can never change what status reports).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from quota_sentinel.state import FileStateStore, ProviderState, ResetCandidate


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


def run_dump(store: FileStateStore, provider: str) -> int:
    state: ProviderState = store.load(provider)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m quota_sentinel")
    parser.add_argument("--state-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("next-due", "print the provider's next_due_at epoch (rc 1 when unset)"),
        ("dump", "print all scheduler-state slots as key=value lines"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("provider")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = FileStateStore(args.state_dir or default_state_dir())
    if args.command == "next-due":
        return run_next_due(store, args.provider)
    return run_dump(store, args.provider)


if __name__ == "__main__":
    sys.exit(main())
