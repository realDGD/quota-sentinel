#!/usr/bin/env python3
"""Local task orchestration for Quota-Sentinel.

This module deliberately owns *when* the scheduler is invoked, not *how*
provider deadlines are calculated. Deadline policy lives in
``quota_sentinel.scheduler``; the Python CLI drives model execution and
quota fetches. SQLite provides durable run history and crash visibility, and the
scheduler's authoritative state is read through ``quota_sentinel.state``'s
backend router — which is what keeps this module from computing wake times
off a retired backend after a cutover.
"""

from __future__ import annotations

import logging
import math
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TypeVar

# The outer `check` bound is DERIVED from these modules, never re-typed: the
# numbers that decide how long a legal check may run live with the channels
# that spend them and with the Application that drives them. All of these are
# import-cheap and stdlib-only, which is what keeps this module inside the
# launchd/system-interpreter graph (pinned by
# tests/python-entrypoint-regression.py E11).
# `quota_sentinel.runtime.factory` is deliberately NOT imported here: it is the
# composition root, a module this one has no business depending on, and its own
# graph already carries urllib and the probe plumbing.
# The light `runtime/probe_budget.py` IS imported: it is stdlib-only, it owns
# the per-tier probe budgets the composition root now reads from it too, and it
# derives the per-phase probe bound from the collector's own structure.
from quota_sentinel.app import AppConfig
from quota_sentinel.runtime import agy_exec, codex_exec, models, probe_budget
from quota_sentinel.runtime.direct import DIRECT_TIMEOUT_SECONDS
from quota_sentinel.runtime.models import ModelRunnerConfig
from quota_sentinel.state import AuthoritativeStateStore, StateStoreError
from quota_sentinel.state.migration import DEFAULT_PROVIDERS


logger = logging.getLogger("task_orchestrator")

DEFAULT_STATE_DIR = Path(
    os.environ.get(
        "QUOTA_SENTINEL_STATE_DIR",
        Path.home() / "Library/Application Support/Quota-Sentinel",
    )
)
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "task-orchestrator.sqlite3"
REPO_DIR = Path(__file__).resolve().parent
# The scheduler is the Python CLI. This process already runs INSIDE the
# project environment (the LaunchAgent starts the listener through
# `uv run --frozen --no-sync`), so the cheapest correct invocation is the
# same interpreter with `-m`: no uv start-up, no lock re-resolution, and no
# dependence on PATH inside launchd.
DEFAULT_SCHEDULER_COMMAND: tuple[str, ...] = (
    sys.executable, "-m", "quota_sentinel", "check",
)

WATCHDOG_INTERVAL_SECONDS = 900
DEADLINE_BACKOFF_SECONDS = 60
EXTERNAL_STATE_RECHECK_SECONDS = 60
LOOP_ERROR_BACKOFF_SECONDS = 60

# ---------------------------------------------------------------------------
# The outer `check` bound.
#
# The orchestrator runs `check` as ONE child process and kills its whole process
# tree when the bound expires, so the bound has to sit above the longest wall
# clock a LEGAL check can spend. That number is not a matter of taste: it is the
# sum of what the channels themselves allow, and it grew the day Codex and
# Antigravity stopped being delivered by Pi alone. The retired literal (2100s)
# was hand-computed for the Pi-only world as `2x310 + 3x310 + 2x207 ≈ 1964`s;
# with the codex/agy channels in place a legal check can run far past it, and the
# outer kill would cut down a run that was still inside its own rules.
#
# So the bound is DERIVED, in one place, from the modules that own the numbers —
# never re-typed. Raising a channel's timeout or retry count, or the
# Application's attempt limits, moves the bound in the same commit:
#
#   one agy attempt = (AGY_TRANSIENT_RETRIES + 1) turns x (AGY_EXEC_TIMEOUT_SECONDS
#                     + agy kill grace)          = 4 x 130 = 520s
#                   + the agent-listing guard (its wrapper's deadline plus the
#                     grace it needs to reap the CLI, plus a 1s parent margin;
#                     always <= AGY_EXEC_TIMEOUT_SECONDS + kill grace)
#                                                = 130s
#                   + one Pi fallback (ModelRunnerConfig.timeout + kill grace)
#                                                = 310s
#                   = 960s
#   ...which is the most expensive of the four transports (Pi 310 + its Codex
#   terminal 130; Codex 130 + Pi 310; direct DIRECT_TIMEOUT_SECONDS = 120 with
#   no fallback at all). Providers inside one round run in PARALLEL
#   (Application._burst), so a round costs that MAXIMUM, not a sum over the
#   roster.
#
#   watchdog burst  = 2 x 960 + 1 x 30 (retry_interval between rounds) = 1950s
#   initial burst   = 3 x 960 + 2 x 30                                 = 2940s
#   Pi prepare      = 2 bursts x (auth_timeout + kill grace) = 2 x 40  =   80s
#   quota probes    = 2 phases x 307 (the probe bound below)           =  614s
#   ------------------------------------------------------------------------
#   legal worst case                                                   = 5584s
#
# The last two terms are DERIVED, not literals, and both used to be missing:
#
#   * the probe term comes from `runtime/probe_budget.py`, which walks the
#     collector's real structure (providers SEQUENTIALLY, each ladder's native
#     and CodexBar rungs, the Keychain reads, the process start-up and the
#     bounded reap every spawned probe owes) and reads the SAME per-tier
#     budgets `factory.quota_probe_options` builds its options from. A fixed
#     300s allowance could not do that: with QUOTA_SENTINEL_CODEXBAR_TIMEOUT
#     set to 4000 the collector's real budget for one CodexBar call became
#     4000s while this bound stayed at 5490s, so the outer kill could cut down
#     a probe that was still inside its own rules;
#   * the prepare term is the bounded Pi credential refresh `ModelRunner.
#     prepare` runs for codex. `Application._burst` prepares every provider
#     once per burst, sequentially and BEFORE the rounds, so one burst pays one
#     refresh (plus its kill grace) — not one per attempt round — and a check
#     can enter two bursts (watchdog retry, then initial).
#
# and the DEFAULT applied below adds CHECK_TIMEOUT_SAFETY_FRACTION on top of
# that, which is what keeps lock waits (`quota_wait`), Keychain reads, state I/O
# and card delivery from eating into the margin. `QUOTA_SENTINEL_CHECK_TIMEOUT`
# still overrides the whole computation; a value that is not a positive finite
# number falls back to the derived default instead of failing the import.
# ---------------------------------------------------------------------------

# One quota-probe phase is bounded by `runtime.probe_budget`, never typed here.
# The retired 300s literal carried a comment claiming an operator's per-tier
# override "is not a bound this module can know" — but the composition root
# hands exactly those overrides to the collector, so the claim was false and the
# bound it produced could be too small. The derived bound keeps a documented
# floor of that same 300s, so this change can only ever raise the bound.
# `Application.check` runs the probe once before deciding what is due and once
# after the initial burst, to calibrate the deadlines it just moved.
QUOTA_PROBE_PHASES_PER_CHECK = 2
# `Application._burst` calls `model_runner.prepare()` once per provider per
# burst, sequentially, before the rounds. Only the codex prepare spends a
# deadline (the bounded Pi credential refresh); every other provider's prepare
# is private-directory work. A check can enter two bursts — the watchdog retry
# burst when debt is due and the initial burst — and even a burst whose attempt
# limit is zero still prepares, so the term is TWO refresh budgets, not one per
# attempt round.
PI_PREPARE_BURSTS_PER_CHECK = 2
# Last-resort value for the Pi prepare term, used only when neither
# `ModelRunnerConfig.auth_timeout` nor `models.PI_AUTH_TIMEOUT_SECONDS` exists
# on the tree being imported. It exists so the term — and with it a real part of
# the bound — cannot silently vanish on a checkout where the credential fix has
# not landed; the moment either source exists, its value is what counts.
PI_AUTH_TIMEOUT_FALLBACK_SECONDS = 30.0
# Applied where the default is formed, not inside the worst case: the callers
# that assert the default covers the bound must be able to compare the two.
CHECK_TIMEOUT_SAFETY_FRACTION = 0.10
CHECK_TIMEOUT_ENV = "QUOTA_SENTINEL_CHECK_TIMEOUT"
# The direct transport's timeout override, applied by the composition root when
# it builds the direct runner (see `_positive_seconds`).
DIRECT_TIMEOUT_ENV = "QUOTA_SENTINEL_DIRECT_TIMEOUT"


def _positive_seconds(
    environment: Mapping[str, str] | None, name: str, default: float,
) -> float:
    """`${VAR:-default}` with the composition root's exact tolerance.

    The run path applies this one override itself (``factory._seconds_override``
    when it builds the direct runner), so only its NAME is pinned here; every
    other term is read through the owning channel's own ``from_env``, which
    keeps its own names next to its own defaults. A value that is not a positive
    finite number means the default, exactly as the run path decides it.
    """
    env = os.environ if environment is None else environment
    raw = env.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def worst_case_attempt_seconds(
    environment: Mapping[str, str] | None = None,
) -> float:
    """The longest ONE provider attempt may legally take, in seconds.

    Every term is read from the module that owns it, so this cannot drift from
    the channels it describes:

    * Pi — one bounded turn (``ModelRunnerConfig.timeout``) plus its kill
      grace. The shipped Pi runner hands a failed turn to the Codex terminal, so
      that one hop is part of the Pi budget too.
    * Codex — one bounded turn (``CODEX_EXEC_TIMEOUT_SECONDS``) plus its kill
      grace, plus a Pi credential refresh and turn as its one-hop fallback.
      ``CodexExecRunner._fallback_to_pi`` prepares Pi on EVERY hand-over.
    * agy — the agent-listing guard and every turn run under the same channel
      timeout, so its budget is ``AGY_TRANSIENT_RETRIES + 1`` turns (plus the
      guard while ``preflight`` is on), plus Pi as its one-hop fallback.
    * direct — one bounded HTTP attempt (``DIRECT_TIMEOUT_SECONDS``, or the
      operator's override of it) and no fallback at all.

    The chain is always ONE hop deep (a runner that has just taken an attempt
    can never hand it back), which is why the fallback is added, not chained.
    """
    pi = ModelRunnerConfig.from_env(environment)
    codex = codex_exec.CodexExecConfig.from_env(
        environment, state_dir=DEFAULT_STATE_DIR
    )
    agy = agy_exec.AgyExecConfig.from_env(environment, state_dir=DEFAULT_STATE_DIR)
    direct = _positive_seconds(
        environment, DIRECT_TIMEOUT_ENV, float(DIRECT_TIMEOUT_SECONDS)
    )

    pi_turn = pi.timeout + pi.kill_grace
    codex_turn = codex.timeout + codex.kill_grace
    codex_pi_prepare = worst_case_prepare_seconds(environment)
    agy_turn = agy.timeout + agy.kill_grace
    # The free guard runs BEFORE the turn and is a real subprocess, so leaving
    # it out would understate a legal agy attempt by a whole turn. Its own
    # deadline is `wrapper (timeout - grace)` + `grace` + a 1s parent margin,
    # which stays inside the `timeout + kill_grace` reserved here.
    agy_guard = agy_turn if agy.preflight else 0.0

    return max(
        pi_turn + codex_turn,             # Pi primary, Codex as its one-hop fallback
        codex_turn + codex_pi_prepare + pi_turn,  # Codex -> Pi prepares on each failure
        agy_guard + (agy.transient_retries + 1) * agy_turn + pi_turn,
        direct,                           # direct: one attempt, no fallback
    )


def pi_auth_timeout_seconds(
    environment: Mapping[str, str] | None = None,
) -> float:
    """The ceiling of ONE bounded Pi credential refresh, in seconds.

    `ModelRunner.prepare` for codex asks Pi for a fresh bearer token
    (``pi auth print-bearer-token``) before it copies the credential into the
    attempt's private agent directory. That call is the one prepare step that
    can spend a deadline, and it is bounded by
    ``ModelRunnerConfig.auth_timeout`` plus the runner's kill grace.

    Two sources are read and the LARGER wins. The configured value is the
    runtime contract (`from_env` reads whatever name the models module chose);
    the module constant is what that field is seeded from, so reading it too
    keeps the term alive on a checkout where the field has not landed yet. A
    bound may over-state, never under-state: an operator who lowers the runtime
    value below its own ceiling leaves the bound where the ceiling was.
    """
    config = ModelRunnerConfig.from_env(environment)
    configured = getattr(config, "auth_timeout", None)
    seeded = getattr(models, "PI_AUTH_TIMEOUT_SECONDS", None)
    candidates = [
        float(value)
        for value in (configured, seeded)
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    ]
    return max(candidates) if candidates else PI_AUTH_TIMEOUT_FALLBACK_SECONDS


def worst_case_prepare_seconds(
    environment: Mapping[str, str] | None = None,
) -> float:
    """One burst's sequential prepare pass, in seconds.

    `Application._burst` prepares every provider once per burst, before any
    round runs. This allowance covers that per-BURST pass; any extra Pi
    prepare during a Codex fallback is counted separately in the per-attempt
    bound above. Only the codex prepare can spend a deadline (the Pi credential
    refresh above); the other providers' prepares are private-directory work
    bounded by nothing worth adding here.
    """
    pi = ModelRunnerConfig.from_env(environment)
    return pi_auth_timeout_seconds(environment) + pi.kill_grace


def worst_case_burst_seconds(
    rounds: int, per_attempt: float, retry_interval: float,
) -> float:
    """One burst: `rounds` parallel attempt rounds and the sleeps between them.

    `Application._burst` sleeps `retry_interval` after every round except the
    last, and only while providers remain — counting every gap is the bound.
    """
    return max(0, rounds) * per_attempt + max(0, rounds - 1) * retry_interval


def worst_case_check_seconds(
    environment: Mapping[str, str] | None = None,
    config: AppConfig | None = None,
) -> float:
    """The legal worst case of ONE `check`, in seconds, without the margin.

    A check that has debt to repay runs a watchdog retry burst first, then a
    full initial burst for the providers that are due, with one quota-probe
    phase before the decision and one after the initial burst. Both bursts are
    bounded by the Application's own attempt limits, read from `AppConfig`
    (the same object `create_application` builds) so an operator override moves
    the bound too.

    Three terms, each read where it is spent:

    * the model rounds, including any per-attempt fallback prepare, from the
      channel configs and the attempt limits;
    * one prepare pass per burst — the codex prepare's bounded Pi credential
      refresh, plus its kill grace;
    * one probe phase per phase, from `runtime/probe_budget`, which reads the
      same per-tier budgets the composition root hands the collector. Raising
      ANY of the eight ``QUOTA_SENTINEL_*`` probe budgets moves this number.
    """
    limits = config if config is not None else AppConfig.from_env(environment)
    per_attempt = worst_case_attempt_seconds(environment)
    model_phase = (
        worst_case_burst_seconds(
            limits.watchdog_attempts, per_attempt, limits.retry_interval
        )
        + worst_case_burst_seconds(
            limits.initial_attempts, per_attempt, limits.retry_interval
        )
    )
    prepare_phase = PI_PREPARE_BURSTS_PER_CHECK * worst_case_prepare_seconds(
        environment
    )
    probe_phase = QUOTA_PROBE_PHASES_PER_CHECK * (
        probe_budget.worst_case_probe_phase_seconds(environment)
    )
    return model_phase + prepare_phase + probe_phase


def check_command_timeout(
    environment: Mapping[str, str] | None = None,
    config: AppConfig | None = None,
    *, software_config=None, runtime_plan=None,
) -> float:
    """`QUOTA_SENTINEL_CHECK_TIMEOUT`, or the derived bound plus its margin.

    The override is an operator escape hatch and wins whenever it is a positive
    finite number. Anything else — unset, empty, non-numeric, zero, negative —
    falls through to the derived default rather than failing the import: this
    module is loaded by launchd, so a typo in an environment variable must not
    take the listener down.
    """
    if software_config is not None:
        from quota_sentinel.runtime.budgets import check_budget
        derived = check_budget(software_config, runtime_plan)
    else:
        derived = worst_case_check_seconds(environment, config) * (1.0 + CHECK_TIMEOUT_SAFETY_FRACTION)
    env = os.environ if environment is None else environment
    raw = env.get(CHECK_TIMEOUT_ENV, "").strip()
    if not raw:
        return derived
    try:
        override = float(raw)
    except ValueError:
        return derived
    if not math.isfinite(override) or override <= 0:
        return derived
    return override


CHECK_COMMAND_TIMEOUT_SECONDS = check_command_timeout()

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
    """Provider deadlines, read through the authoritative backend router.

    Reading the legacy slot files directly was correct only while they were
    always authoritative. After a cutover they are a FROZEN rollback
    artifact, so a direct read would compute wake times from state the
    scheduler has already superseded: the listener would sleep through a
    deadline that moved, or wake for one that is long gone. Every read now
    goes through ``AuthoritativeStateStore``, which selects the backend
    from the durable authority manifest and re-checks that manifest around
    the read.

    Failure stays non-fatal in the same DIRECTION as before: an
    uninitialized, missing or unreadable authority yields no deadlines and
    the orchestrator falls back to its watchdog grid, which still runs
    ``check`` and therefore cannot lose a due task. What must never happen
    is the silent alternative — reading a retired backend and reporting
    its stale deadlines as if they were current. So the failure is logged
    at ERROR with the reason, and no deadline is returned.
    """

    def __init__(
        self,
        state_dir: Path = DEFAULT_STATE_DIR,
        providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.providers = providers

    def _load(self) -> dict[str, Any] | None:
        """Load every provider through the router, or None when unavailable."""
        try:
            return AuthoritativeStateStore(self.state_dir).load_all(self.providers)
        except StateStoreError as exc:
            logger.error(
                "scheduler state is unavailable through the authoritative "
                "backend (%s); falling back to the watchdog grid rather than "
                "reading a possibly retired backend",
                exc,
            )
            return None

    def snapshot(self) -> dict[str, int | None]:
        states = self._load()
        if states is None:
            return {provider: None for provider in self.providers}
        return {
            provider: states[provider].next_due_at
            for provider in self.providers
        }

    def next_due(self) -> int | None:
        states = self._load()
        if states is None:
            return None
        values = [
            state.next_due_at
            for provider, state in states.items()
            if state.next_due_at is not None and not state.retry_pending
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
        scheduler_command: Sequence[str] = DEFAULT_SCHEDULER_COMMAND,
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
        self.scheduler_command = tuple(scheduler_command)
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
            result = self.runner.run(self.scheduler_command, self.check_timeout)
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

    def wait(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(0.5):
            if self._thread is not None and not self._thread.is_alive():
                raise RuntimeError("scheduler thread stopped unexpectedly")


def create_default_orchestrator(
    *, task_logger: logging.Logger | None = None
) -> TaskOrchestrator:
    return TaskOrchestrator(task_logger=task_logger)
