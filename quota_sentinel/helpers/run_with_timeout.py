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
  convention), 127 when the child cannot be spawned, 125 for helper misuse,
  78 when the optional macOS agy guard detects interactive authentication.
"""

import os
import math
import signal
import subprocess
import sys
from pathlib import Path as _Path
if not __package__:sys.path.insert(0,str(_Path(__file__).resolve().parents[2]))
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
    argv = sys.argv[1:]
    separator = argv.index('--') if '--' in argv else len(argv)
    guarded = '--agy-auth-guard' in argv[:separator]
    argv = [arg for arg in argv[:separator] if arg != '--agy-auth-guard'] + argv[separator:]
    separator = argv.index('--') if '--' in argv else len(argv)
    log_path = None
    if '--agy-log-file' in argv[:separator]:
        index = argv.index('--agy-log-file')
        if not guarded or index + 1 >= separator:
            fail('--agy-log-file requires the agy guard and a path')
            return HELPER_ERROR_EXIT
        log_path = argv[index+1]
        argv = argv[:index] + argv[index+2:]
    timeout, grace, command = parse_args(argv)
    if timeout is None or not command:
        return HELPER_ERROR_EXIT
    if not math.isfinite(timeout) or not math.isfinite(grace) or timeout <= 0 or grace < 0:
        fail("--timeout must be > 0 and --kill-grace >= 0")
        return HELPER_ERROR_EXIT

    from contextlib import nullcontext
    from quota_sentinel.platform.background_auth import AgyAuthGuard
    from quota_sentinel.diagnostics.trace import Invocation
    kind = 'agy-usage' if '/usage' in command else 'agy-agents' if '/agents' in command else 'agy-model'
    audit = Invocation(kind, os.environ, reuse=True) if guarded else None
    code = SPAWN_ERROR_EXIT
    try:
        with AgyAuthGuard(log_path) if guarded else nullcontext() as guard:
            code = execute(command, timeout, grace, guard, audit)
            return code
    except (ValueError, OSError):
        if audit is not None:
            audit.note('guard_unavailable', reason='background_auth_guard_unavailable')
        fail('background_auth_guard_unavailable')
        return SPAWN_ERROR_EXIT
    finally:
        if audit is not None:
            audit.note('wrapper_finished', exit_code=code)


def execute(command, timeout, grace, guard, audit=None):
    from quota_sentinel.diagnostics.trace import process_identity, ancestors
    if audit is not None:
        audit.note('wrapper_started', ancestors=ancestors(), requested_executable=str(command[0]))
    try:
        from quota_sentinel.platform.process import spawn_owned
        from pathlib import Path
        if guard is not None:
            command = guard.command(command)
        proc = spawn_owned(command, cwd=Path.cwd(), environment=os.environ)
    except OSError as exc:
        fail(f"could not spawn child: {exc}")
        return SPAWN_ERROR_EXIT

    if audit is not None:
        audit.note('process_started', target_pid=proc.pid, target_identity=process_identity(proc.pid))
        if guard.active:
            audit.note('browser_protection_enabled', target_pid=proc.pid, guard='macos-sandbox')
    code = None
    started = time.monotonic()
    try:
        code = wait_command(proc, timeout, grace, guard, audit)
        return code
    finally:
        proc.close()
        if audit is not None:
            audit.note('process_finished', target_pid=proc.pid, exit_code=code,
                       elapsed=round(time.monotonic()-started, 3))


def wait_command(proc, timeout, grace, guard, audit):
    from quota_sentinel.diagnostics.trace import process_identity
    try:
        if guard is not None and guard.active:
            from quota_sentinel.platform.background_auth import AUTH_REQUIRED_EXIT
            deadline = time.monotonic() + timeout
            while True:
                if guard.auth_required():
                    if audit is not None:
                        audit.note('auth_required', target_pid=proc.pid,
                            marker=guard.auth_marker, target_identity=process_identity(proc.pid),
                            action='abort-interactive-auth', guard='macos-sandbox')
                    proc.stop(min(grace, 1))
                    fail('background authentication required; browser launch blocked')
                    return AUTH_REQUIRED_EXIT
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('background agy', timeout)
                try:
                    exit_code = proc.wait(timeout=min(.05, remaining))
                    if guard.auth_required(force=True):
                        if audit is not None:
                            audit.note('auth_required', target_pid=proc.pid,
                                marker=guard.auth_marker, target_identity=process_identity(proc.pid),
                                action='abort-interactive-auth', guard='macos-sandbox')
                        fail('background authentication required; browser launch blocked')
                        return AUTH_REQUIRED_EXIT
                    return exit_code
                except subprocess.TimeoutExpired:
                    pass
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if guard is not None and guard.active:
            proc.stop(grace)
            fail('background agy timed out')
            return TIMEOUT_EXIT
        operation = 'owned Job terminated' if os.name == 'nt' else 'SIGTERM sent to owned process groups'
        fail(f"timed out after {timeout:g}s; {operation}")
        proc.stop(grace)
        if os.name != 'nt':
            fail(f"SIGKILL cleanup completed after waiting up to {grace:g}s grace")
        return TIMEOUT_EXIT


if __name__ == "__main__":
    sys.exit(main())
