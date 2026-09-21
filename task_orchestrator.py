#!/usr/bin/env python3
"""Local task orchestration for quota-sentinel.

This module deliberately owns *when* the existing shell scheduler is invoked,
not *how* provider deadlines are calculated.  The shell remains the sole
scheduling-policy adapter, preserving its Fresh/Stale, reset-buffer, blocking,
fallback, and retry semantics.  SQLite provides durable run history and crash
visibility while the existing provider state files remain authoritative.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar


logger = logging.getLogger("task_orchestrator")

DEFAULT_STATE_DIR = Path(
    os.environ.get(
        "QUOTA_SENTINEL_STATE_DIR",
        "/Users/__USER__/Library/Application Support/quota-sentinel",
    )
)
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "task-orchestrator.sqlite3"
DEFAULT_SCRIPT_PATH = Path(
    "/Users/__USER__/code/quota-sentinel/"
    "quota-sentinel.sh"
)

WATCHDOG_INTERVAL_SECONDS = 900
DEADLINE_BACKOFF_SECONDS = 60
EXTERNAL_STATE_RECHECK_SECONDS = 60
LOOP_ERROR_BACKOFF_SECONDS = 60
CHECK_COMMAND_TIMEOUT_SECONDS = float(
    # One check may first repay a two-attempt pending debt and then run the
    # remaining providers' three-attempt initial burst, with two
    # quota-collection phases. Providers within a round run in parallel, so
    # three providers still cost 2x310s + 3x310s + 2x207s ≈ 1964s: 2100s stays
    # above that legal worst case while remaining finite.
    os.environ.get("QUOTA_SENTINEL_CHECK_TIMEOUT", "2100")
)

T = TypeVar("T")


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    timed_out: bool
    elapsed: float


class SubprocessRunner:
    """Run one scheduler command with a cancellable process-group lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: subprocess.Popen[bytes] | None = None

    @staticmethod
    def _process_groups_for_tree(root_pid: int) -> set[int]:
        """Snapshot every process group in root_pid's descendant tree.

        The shell uses nested timeout helpers whose children intentionally
        start independent sessions. Killing only the shell's group therefore
        misses those grandchildren. A process-tree snapshot lets the outer
        timeout terminate each isolated group without changing inner timeout
        semantics.
        """
        groups = {root_pid}
        try:
            result = subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid=,pgid="],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return groups

        children: dict[int, list[int]] = {}
        pgids: dict[int, int] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3:
                continue
            try:
                pid, ppid, pgid = map(int, fields)
            except ValueError:
                continue
            children.setdefault(ppid, []).append(pid)
            pgids[pid] = pgid

        stack = [root_pid]
        descendants: set[int] = set()
        while stack:
            parent = stack.pop()
            for child in children.get(parent, []):
                if child in descendants:
                    continue
                descendants.add(child)
                stack.append(child)

        own_group = os.getpgrp()
        groups.update(
            pgids[pid]
            for pid in descendants
            if pgids.get(pid, 0) > 0 and pgids[pid] != own_group
        )
        groups.discard(own_group)
        return groups

    @staticmethod
    def _signal_groups(groups: set[int], sig: signal.Signals) -> None:
        for pgid in groups:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    @staticmethod
    def _living_groups(groups: set[int]) -> set[int]:
        living: set[int] = set()
        for pgid in groups:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                continue
            living.add(pgid)
        return living

    @classmethod
    def _terminate_group(cls, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        groups = cls._process_groups_for_tree(process.pid)
        cls._signal_groups(groups, signal.SIGTERM)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not cls._living_groups(groups):
                break
            time.sleep(0.05)

        living = cls._living_groups(groups)
        if living:
            cls._signal_groups(living, signal.SIGKILL)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # The root group was already included above; this is a final
            # defensive reap for an unexpected process-state race.
            cls._signal_groups({process.pid}, signal.SIGKILL)
            process.wait()

    def run(self, args: tuple[str, ...], timeout: float) -> CommandResult:
        started = time.monotonic()
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with self._lock:
            self._active = process
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_group(process)
        finally:
            with self._lock:
                if self._active is process:
                    self._active = None
        return CommandResult(
            exit_code=124 if timed_out else int(process.returncode or 0),
            timed_out=timed_out,
            elapsed=time.monotonic() - started,
        )

    def cancel(self) -> None:
        with self._lock:
            process = self._active
        if process is not None:
            self._terminate_group(process)


class ScheduleState:
    """Read the shell scheduler's provider-specific deadline state."""

    def __init__(
        self,
        state_dir: Path = DEFAULT_STATE_DIR,
        providers: tuple[str, ...] = ("codex", "antigravity", "opencode"),
    ) -> None:
        self.state_dir = Path(state_dir)
        self.providers = providers

    def _read_epoch(self, provider: str) -> int | None:
        try:
            raw = (self.state_dir / f"{provider}-next-due-at").read_text().strip()
            value = int(raw)
            return value if value >= 0 else None
        except (OSError, ValueError):
            return None

    def snapshot(self) -> dict[str, int | None]:
        return {provider: self._read_epoch(provider) for provider in self.providers}

    def _is_pending(self, provider: str) -> bool:
        try:
            return (
                self.state_dir / f"{provider}-retry-pending"
            ).read_text().strip() == "1"
        except OSError:
            return False

    def next_due(self) -> int | None:
        values = [
            value
            for provider, value in self.snapshot().items()
            if value is not None and not self._is_pending(provider)
        ]
        return min(values) if values else None


class TaskStore:
    """SQLite task history with short, atomic transactions."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.db_path.parent, 0o700)
        self._initialize()
        self._recover_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback one short transaction and always release its FDs."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _secure_files(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_name TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    scheduled_for INTEGER,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    timed_out INTEGER NOT NULL DEFAULT 0,
                    elapsed REAL,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS task_runs_started_idx
                    ON task_runs(started_at DESC);

                CREATE TABLE IF NOT EXISTS schedule_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_run_id INTEGER,
                    provider TEXT NOT NULL,
                    next_due_at INTEGER,
                    captured_at INTEGER NOT NULL,
                    FOREIGN KEY(task_run_id) REFERENCES task_runs(id)
                );
                CREATE INDEX IF NOT EXISTS schedule_snapshots_provider_idx
                    ON schedule_snapshots(provider, captured_at DESC);
                """
            )
        self._secure_files()

    def _recover_interrupted(self) -> None:
        recovered_at = int(time.time())
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE task_runs
                   SET status = 'interrupted', finished_at = ?,
                       detail = 'orchestrator restarted before completion'
                 WHERE status = 'running'
                """,
                (recovered_at,),
            )
        self._secure_files()

    def begin_run(
        self,
        task_name: str,
        trigger: str,
        scheduled_for: int | None,
        started_at: int,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO task_runs(
                    task_name, trigger, scheduled_for, started_at, status
                ) VALUES (?, ?, ?, ?, 'running')
                """,
                (task_name, trigger, scheduled_for, started_at),
            )
            run_id = int(cursor.lastrowid)
        self._secure_files()
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: str,
        finished_at: int,
        *,
        exit_code: int | None = None,
        timed_out: bool = False,
        elapsed: float | None = None,
        detail: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE task_runs
                   SET finished_at = ?, status = ?, exit_code = ?, timed_out = ?,
                       elapsed = ?, detail = ?
                 WHERE id = ?
                """,
                (
                    finished_at,
                    status,
                    exit_code,
                    int(timed_out),
                    elapsed,
                    detail,
                    run_id,
                ),
            )
        self._secure_files()

    def record_snapshot(
        self,
        run_id: int | None,
        snapshot: dict[str, int | None],
        captured_at: int,
    ) -> None:
        rows = [
            (run_id, provider, next_due, captured_at)
            for provider, next_due in snapshot.items()
        ]
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO schedule_snapshots(
                    task_run_id, provider, next_due_at, captured_at
                ) VALUES (?, ?, ?, ?)
                """,
                rows,
            )
        self._secure_files()

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_runs ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]


class TaskOrchestrator:
    """One local scheduler for watchdog, precise deadline, and task history."""

    def __init__(
        self,
        *,
        script_path: Path = DEFAULT_SCRIPT_PATH,
        schedule_state: ScheduleState | None = None,
        store: TaskStore | None = None,
        runner: SubprocessRunner | None = None,
        clock: Callable[[], float] = time.time,
        watchdog_interval: int = WATCHDOG_INTERVAL_SECONDS,
        deadline_backoff: int = DEADLINE_BACKOFF_SECONDS,
        external_recheck: int = EXTERNAL_STATE_RECHECK_SECONDS,
        loop_error_backoff: float = LOOP_ERROR_BACKOFF_SECONDS,
        check_timeout: float = CHECK_COMMAND_TIMEOUT_SECONDS,
        task_logger: logging.Logger | None = None,
    ) -> None:
        self.script_path = Path(script_path)
        self.schedule_state = schedule_state or ScheduleState()
        self.store = store or TaskStore()
        self.runner = runner or SubprocessRunner()
        self.clock = clock
        self.watchdog_interval = watchdog_interval
        self.deadline_backoff = deadline_backoff
        self.external_recheck = external_recheck
        self.loop_error_backoff = loop_error_backoff
        self.check_timeout = check_timeout
        self.log = task_logger or logger

        self._condition = threading.Condition()
        self._iteration_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_done = False
        self._next_watchdog_at: int | None = None
        self._deadline_not_before = 0

    def _next_watchdog_after(self, epoch: int) -> int:
        return ((epoch // self.watchdog_interval) + 1) * self.watchdog_interval

    def _run_check(self, trigger: str, scheduled_for: int) -> CommandResult:
        started_at = int(self.clock())
        run_id = self.store.begin_run("check", trigger, scheduled_for, started_at)
        self.log.info(
            "orchestrator check start trigger=%s scheduled_for=%s", trigger, scheduled_for
        )
        try:
            result = self.runner.run(
                ("/bin/zsh", str(self.script_path), "check"), self.check_timeout
            )
            status = "succeeded" if result.exit_code == 0 else "failed"
            self.store.finish_run(
                run_id,
                status,
                int(self.clock()),
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                elapsed=result.elapsed,
                detail="check timed out" if result.timed_out else None,
            )
            self.log.info(
                "orchestrator check finish trigger=%s status=%s exit=%s elapsed=%.1fs",
                trigger,
                status,
                result.exit_code,
                result.elapsed,
            )
            return result
        except BaseException as exc:
            self.store.finish_run(
                run_id,
                "failed",
                int(self.clock()),
                detail=f"{type(exc).__name__} while invoking check",
            )
            raise
        finally:
            self.store.record_snapshot(
                run_id, self.schedule_state.snapshot(), int(self.clock())
            )

    def run_startup(self) -> None:
        with self._iteration_lock:
            if self._startup_done:
                return
            started = int(self.clock())
            self._run_check("startup", started)
            finished = int(self.clock())
            self._deadline_not_before = finished + self.deadline_backoff
            self._next_watchdog_at = self._next_watchdog_after(finished)
            self._startup_done = True

    def run_ready_once(self) -> bool:
        with self._iteration_lock:
            if not self._startup_done:
                self.run_startup()
                return True

            now = int(self.clock())
            due = self.schedule_state.next_due()
            deadline_ready = (
                due is not None
                and due <= now
                and now >= self._deadline_not_before
            )
            watchdog_ready = (
                self._next_watchdog_at is not None
                and now >= self._next_watchdog_at
            )
            if not deadline_ready and not watchdog_ready:
                return False

            if deadline_ready and watchdog_ready:
                trigger = "deadline+watchdog"
                scheduled_for = min(due, self._next_watchdog_at)  # type: ignore[arg-type]
            elif deadline_ready:
                trigger = "deadline"
                scheduled_for = int(due)  # type: ignore[arg-type]
            else:
                trigger = "watchdog"
                scheduled_for = int(self._next_watchdog_at)  # type: ignore[arg-type]

            self._run_check(trigger, scheduled_for)
            finished = int(self.clock())
            if deadline_ready:
                self._deadline_not_before = finished + self.deadline_backoff
            if watchdog_ready:
                self._next_watchdog_at = self._next_watchdog_after(finished)
            return True

    def next_wake_at(self) -> int:
        now = int(self.clock())
        candidates = [now + self.external_recheck]
        if self._next_watchdog_at is not None:
            candidates.append(self._next_watchdog_at)
        due = self.schedule_state.next_due()
        if due is not None:
            if due <= now:
                candidates.append(max(now, self._deadline_not_before))
            else:
                candidates.append(due)
        return min(candidates)

    def notify_state_changed(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def run_external_task(
        self, task_name: str, trigger: str, action: Callable[[], T]
    ) -> T:
        started_at = int(self.clock())
        run_id = self.store.begin_run(task_name, trigger, None, started_at)
        started_mono = time.monotonic()
        try:
            value = action()
        except BaseException as exc:
            self.store.finish_run(
                run_id,
                "failed",
                int(self.clock()),
                elapsed=time.monotonic() - started_mono,
                detail=f"{type(exc).__name__} in external task",
            )
            raise
        else:
            self.store.finish_run(
                run_id,
                "succeeded",
                int(self.clock()),
                exit_code=0,
                elapsed=time.monotonic() - started_mono,
            )
            return value
        finally:
            self.store.record_snapshot(
                run_id, self.schedule_state.snapshot(), int(self.clock())
            )
            self.notify_state_changed()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not self._startup_done:
                    self.run_startup()
                    continue
                if self.run_ready_once():
                    continue
                timeout = max(0.1, self.next_wake_at() - self.clock())
                with self._condition:
                    self._condition.wait(timeout=timeout)
            except Exception:
                if self._stop_event.is_set():
                    return
                self.log.exception(
                    "orchestrator iteration failed; retrying in %.1fs",
                    self.loop_error_backoff,
                )
                with self._condition:
                    self._condition.wait(timeout=self.loop_error_backoff)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="quota-sentinel-orchestrator",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.runner.cancel()
        self.notify_state_changed()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)


def create_default_orchestrator(
    *, task_logger: logging.Logger | None = None
) -> TaskOrchestrator:
    return TaskOrchestrator(task_logger=task_logger)
