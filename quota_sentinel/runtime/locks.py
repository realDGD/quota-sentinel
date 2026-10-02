"""Quota probe lock compatible with the existing macOS shlock files."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from quota_sentinel.state.runlock import SHLOCK_BIN
from quota_sentinel.state.store import StateStoreError

QUOTA_LOCK_FILENAME = "quota.lock"


class LockError(StateStoreError):
    """A probe lock cannot be safely acquired or released."""


class LockBusyError(LockError):
    """Another process still owns the probe lock."""


@dataclass
class QuotaLock:
    path: Path
    released: bool = False
    held: object = None

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.held is not None:
            try:self.held.release()
            except OSError as exc:raise LockError('could not release quota.lock') from exc
            return
        try:
            if self.path.read_text().strip()==str(os.getpid()):self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise LockError(f"could not release {self.path}: {exc}") from exc

    def __enter__(self) -> "QuotaLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_quota_lock(
    state_dir: Path, *, timeout: float = 0, poll: float = 0.25
) -> QuotaLock:
    """Acquire quota.lock using the recorded native coordination protocol."""
    directory = Path(state_dir)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise LockError(f"could not prepare quota lock directory: {exc}") from exc
    from quota_sentinel.platform.locks import acquire_lock,state_protocol,LockBusy,LockError as PlatformLockError
    try:
        protocol=state_protocol(directory,allow_empty_init=True)
        held=acquire_lock(directory/QUOTA_LOCK_FILENAME,timeout=timeout,protocol=protocol,poll=poll)
    except LockBusy as exc:raise LockBusyError('quota.lock: '+str(exc)) from exc
    except (PlatformLockError,OSError) as exc:raise LockError(str(exc)) from exc
    return QuotaLock(directory/QUOTA_LOCK_FILENAME,held=held)


__all__ = ["LockError", "LockBusyError", "QuotaLock", "acquire_quota_lock"]
