"""Acquiring the scheduler's run.lock from Python (P1-1).

The serialization boundary for every authoritative scheduler mutation is
``<state_dir>/run.lock``, and the shell owns it with ``/usr/bin/shlock``:
the holder writes its PID into the file, and shlock refuses when a LIVE
process already owns it (a dead owner's file is stolen, which is what makes
a crashed run recoverable without an operator).

This module does not reimplement that protocol. It executes THE SAME BINARY
with the same arguments on the same file, using this process's PID — so a
Python holder and a shell holder exclude each other exactly as two shell
holders do. Reimplementing pid-liveness in Python would create a second,
subtly different lock for the same file, which is the one thing the
operator lifecycle CLI must not introduce.

WHY THIS EXISTS
---------------
The public lifecycle verbs (``cutover``, ``rollback``,
``bootstrap-authority``) are safe to run standalone precisely because they
acquire this lock before touching authority. The verbs the SHELL calls
(``scheduler-*``) deliberately do NOT acquire it: the shell already holds
it, and taking it twice from two different PIDs would deadlock against
itself.

SAFETY NET BEYOND THE LOCK
--------------------------
Cutover and rollback additionally re-read the authority fact after their
write and compare, so a mutation that somehow ran unserialized is detected
rather than reported as success.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .store import StateStoreError

# The shell's RUN_LOCK_FILE name. One name, one file, one protocol.
RUN_LOCK_FILENAME = "run.lock"

# The lock utility the shell uses. Not configurable by default: a different
# binary would be a different lock protocol over the same file.
SHLOCK_BIN = "/usr/bin/shlock"

DEFAULT_LOCK_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 0.25


class RunLockError(StateStoreError):
    """run.lock could not be acquired or could not be acquired safely."""


class RunLockBusyError(RunLockError):
    """A live process held run.lock for the whole wait budget.

    Not an error condition in the "something is broken" sense: an
    in-flight ``check`` or model run holds the lock for real work, and the
    operator is expected to retry. It is raised rather than returned so a
    caller cannot accidentally continue into a mutation.
    """


class RunLockUnsupportedError(RunLockError):
    """The lock utility is missing, so ownership cannot be established."""


@dataclass
class RunLock:
    """An acquired run.lock. Use as a context manager, or release() on exit.

    ``released`` guards double-release: the file is removed once, and a
    second release is a no-op rather than deleting a lock some other
    process has since acquired. That window is real — release() then
    another process acquires, then our second release would delete THEIR
    lock — so the guard is a correctness requirement, not tidiness.
    """

    path: Path
    pid: int
    released: bool = False
    held: object = None

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.held is not None:
            try:self.held.release()
            except OSError as exc:raise RunLockError('could not release run.lock') from exc
            return
        try:
            if self.path.read_text().strip() == str(self.pid):
                os.unlink(self.path)
        except FileNotFoundError:
            # Already gone. Either we never wrote it or someone cleaned up;
            # never treat this as a reason to keep going loudly.
            pass
        except OSError as exc:
            raise RunLockError(
                f"could not release run.lock {self.path}: {exc}"
            ) from exc

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _shlock_available() -> bool:
    return os.access(SHLOCK_BIN, os.X_OK)


def _try_acquire(state_dir: Path) -> bool:
    """One non-blocking shlock attempt with this process's PID."""
    directory = Path(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise RunLockError(
            f"cannot prepare state dir {directory} for run.lock: {exc}"
        ) from exc
    path = directory / RUN_LOCK_FILENAME
    try:
        completed = subprocess.run(
            [SHLOCK_BIN, "-p", str(os.getpid()), "-f", str(path)],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunLockError(f"could not run {SHLOCK_BIN}: {exc}") from exc
    if completed.returncode == 0:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise RunLockError(
                f"acquired run.lock {path} but could not set its mode: {exc}"
            ) from exc
        return True
    return False


def acquire_run_lock(
    state_dir: Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll: float = DEFAULT_POLL_SECONDS,
    on_wait: Optional[callable] = None,
) -> RunLock:
    """Acquire run.lock, waiting up to ``timeout`` seconds for a busy owner.

    A timeout of 0 means "one attempt, fail immediately", which is what an
    opportunistic caller wants. The operator lifecycle verbs use the
    default: an in-flight ``check`` finishes, and the cutover runs next
    rather than the operator having to retry by hand.
    """
    from quota_sentinel.platform.locks import acquire_lock,state_protocol,LockBusy,LockUnavailable,LockError
    directory=Path(state_dir)
    try:
        protocol=state_protocol(directory,allow_empty_init=True)
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(directory,0o700)
        held=acquire_lock(directory/RUN_LOCK_FILENAME,timeout=timeout,protocol=protocol,poll=poll,on_wait=on_wait)
    except LockBusy as exc:raise RunLockBusyError('run.lock: '+str(exc)) from exc
    except LockUnavailable as exc:raise RunLockUnsupportedError(str(exc)) from exc
    except (LockError,OSError) as exc:raise RunLockError(str(exc)) from exc
    return RunLock(directory/RUN_LOCK_FILENAME,os.getpid(),held=held)


__all__ = [
    "RUN_LOCK_FILENAME",
    "SHLOCK_BIN",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "RunLock",
    "RunLockError",
    "RunLockBusyError",
    "RunLockUnsupportedError",
    "acquire_run_lock",
]
