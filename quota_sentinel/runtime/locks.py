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

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            self.path.unlink()
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
    """Acquire `<state_dir>/quota.lock` using the legacy shlock protocol."""
    directory = Path(state_dir)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise LockError(f"could not prepare quota lock directory: {exc}") from exc
    if not os.access(SHLOCK_BIN, os.X_OK):
        raise LockError(f"{SHLOCK_BIN} is not executable")

    path = directory / QUOTA_LOCK_FILENAME
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            result = subprocess.run(
                [SHLOCK_BIN, "-p", str(os.getpid()), "-f", str(path)],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LockError(f"could not run {SHLOCK_BIN}: {exc}") from exc
        if result.returncode == 0:
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                path.unlink(missing_ok=True)
                raise LockError(f"could not secure {path}: {exc}") from exc
            return QuotaLock(path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LockBusyError(f"quota.lock is busy at {path}")
        time.sleep(min(poll, remaining))


__all__ = ["LockError", "LockBusyError", "QuotaLock", "acquire_quota_lock"]
