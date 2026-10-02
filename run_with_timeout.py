#!/usr/bin/env python3
"""Run a command in its own session under a hard timeout.

Contract (kept minimal on purpose):
- The child inherits the helper's stdin/stdout/stderr, cwd, and environment.
  The helper itself never writes to stdout, so a caller that inspects the
  child's stdout (e.g. the "reply 1" model check) sees the child's bytes only.
  All helper diagnostics go to stderr and never echo the command line.
- The child starts a new session (setsid), so its PID is the process group
  ID and the whole tree can be signalled: SIGTERM first, then wait up to the
  grace period for the child. Any remaining group members receive SIGKILL,
  including descendants left behind by a child that exited on SIGTERM.
- Exit codes: the child's own exit code, 124 on timeout (GNU timeout
  convention), 127 when the child cannot be spawned, 125 for helper misuse.
"""

import os
import math
import signal
import subprocess
import sys
import time

TIMEOUT_EXIT = 124
HELPER_ERROR_EXIT = 125
SPAWN_ERROR_EXIT = 127


def fail(message):
    print(f"run_with_timeout: {message}", file=sys.stderr)


def parse_args(argv):
    timeout = None
    grace = 10.0
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--timeout":
            if i + 1 >= len(argv):
                fail("--timeout requires a value")
                return None, None, None
            try:
                timeout = float(argv[i + 1])
            except ValueError:
                fail(f"invalid --timeout value: {argv[i + 1]}")
                return None, None, None
            i += 2
        elif arg == "--kill-grace":
            if i + 1 >= len(argv):
                fail("--kill-grace requires a value")
                return None, None, None
            try:
                grace = float(argv[i + 1])
            except ValueError:
                fail(f"invalid --kill-grace value: {argv[i + 1]}")
                return None, None, None
            i += 2
        elif arg == "--":
            return timeout, grace, argv[i + 1:]
        else:
            fail(f"unknown argument: {arg} (command must follow --)")
            return None, None, None
    fail("no command given (usage: run_with_timeout.py --timeout N -- cmd ...)")
    return None, None, None


def group_differs_from_ours(pgid):
    """Use the group ID established by start_new_session, even after exit.

    Looking up the child's current group fails once its leader is reaped,
    although surviving descendants may still belong to that original group.
    Never signal the helper's own group or a non-positive group ID.
    """
    return pgid > 0 and pgid != os.getpgrp()


def kill_group(pgid, sig):
    if not group_differs_from_ours(pgid):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def main():
    timeout, grace, command = parse_args(sys.argv[1:])
    if timeout is None or not command:
        return HELPER_ERROR_EXIT
    if not math.isfinite(timeout) or not math.isfinite(grace) or timeout <= 0 or grace < 0:
        fail("--timeout must be > 0 and --kill-grace >= 0")
        return HELPER_ERROR_EXIT

    try:
        from quota_sentinel.platform.process import spawn_owned
        from pathlib import Path
        proc = spawn_owned(command, cwd=Path.cwd(), environment=os.environ)
    except OSError as exc:
        fail(f"could not spawn child: {exc}")
        return SPAWN_ERROR_EXIT

    try:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            operation = 'owned Job terminated' if os.name == 'nt' else 'SIGTERM sent to owned process groups'
            fail(f"timed out after {timeout:g}s; {operation}")
            proc.stop(grace)
            if os.name != 'nt':
                fail(f"SIGKILL cleanup completed after waiting up to {grace:g}s grace")
            return TIMEOUT_EXIT
    finally:
        proc.close()


if __name__ == "__main__":
    sys.exit(main())
