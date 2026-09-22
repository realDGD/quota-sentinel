"""The append-only daily run log the shell kept, so operators keep reading it.

The shell wrote one file per day under ``logs/``, named by the Shanghai date,
mode 0600 in a 0700 directory, with ``[timestamp] [LEVEL] [pid] message``
lines. `wait` runs for weeks, so the file has to roll over at midnight rather
than at handler construction — the shell printed each line with a fresh date
lookup, and losing that would silently split a run across two files at the
wrong boundary.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from quota_sentinel.runtime.cards import SHANGHAI

DEFAULT_LOG_DIR_NAME = "logs"
LOG_FILE_SUFFIX = ".log"


class _DailyFileHandler(logging.Handler):
    """One file per Shanghai day, created lazily and reopened on rollover."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = Path(directory)
        self._day: Optional[str] = None
        self._stream = None

    def _day_name(self) -> str:
        return datetime.now(SHANGHAI).strftime("%Y-%m-%d")

    def _ensure_stream(self):
        day = self._day_name()
        if self._stream is not None and day == self._day:
            return self._stream
        self._close()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self.directory / (day + LOG_FILE_SUFFIX)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(descriptor, 0o600)
        self._stream = os.fdopen(descriptor, "a", encoding="utf-8")
        self._day = day
        return self._stream

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._ensure_stream()
            stream.write(self.format(record) + "\n")
            stream.flush()
        except OSError:
            # Logging must never take the scheduler down: the shell's
            # log_line() also swallowed every write failure.
            self._close()

    def close(self) -> None:
        self._close()
        super().close()


class _RunLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, SHANGHAI).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return "[%s] [%s] [%d] %s" % (
            stamp, record.levelname, record.process, record.getMessage(),
        )


def configure(log_dir: Optional[Path] = None, *, level: int = logging.INFO) -> None:
    """Route the port's logging into the operator's run log.

    Idempotent: the CLI may build several applications in one process, and a
    second call must not double every line.
    """
    root = logging.getLogger("quota_sentinel")
    root.setLevel(level)
    for handler in list(root.handlers):
        if isinstance(handler, _DailyFileHandler):
            return
    handler = _DailyFileHandler(
        Path(log_dir) if log_dir is not None else default_log_dir()
    )
    handler.setFormatter(_RunLogFormatter())
    root.addHandler(handler)


def default_log_dir() -> Path:
    env = os.environ.get("QUOTA_SENTINEL_LOG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / DEFAULT_LOG_DIR_NAME


__all__ = ["configure", "default_log_dir"]
