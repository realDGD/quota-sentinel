"""Isolated Pi model attempts for the three quota-sentinel providers."""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

__all__ = [
    "PI_AUTH_TIMEOUT_SECONDS",
    "AttemptResult",
    "ModelRunner",
    "ModelRunnerConfig",
    "PreparedPaths",
]


def _env_value(env: Mapping[str, str], key: str, default: str) -> str:
    """Reproduce the shell's ``${VAR:-default}``: an empty value is unset."""
    value = env.get(key)
    if value is None or value == "":
        return default
    return value


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    """``${VAR:-default}`` for a numeric variable; junk falls back, never raises."""
    try:
        return float(_env_value(env, key, str(default)))
    except ValueError:
        return default


def _env_positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    """``${VAR:-default}`` for a value that is used as a wall-clock bound.

    Unset, empty, non-numeric, non-positive, NaN and infinite values all fall
    back: a bound of zero, of a negative number or of infinity is not a bound,
    and this one is what keeps the Pi credential refresh from waiting forever.
    """
    value = _env_float(env, key, default)
    if not math.isfinite(value) or value <= 0:
        return default
    return value


# The Pi credential refresh -- ``pi auth print-bearer-token`` -- is a provider
# call in its own right and runs before every codex attempt on this transport,
# so it is bounded on its own clock. Its inner deadline is this many seconds;
# prepare() adds a parent-side guard of ``kill_grace`` on top, which makes
# ``auth_timeout + kill_grace`` the most one codex prepare() can legally cost.
# The orchestrator's worst-case attempt bound reads that sum -- it takes
# ``auth_timeout`` from the config below -- so the two move together.
PI_AUTH_TIMEOUT_SECONDS = 30


@dataclass
class ModelRunnerConfig:
    pi_bin: Path
    auth_file: Path
    repo_dir: Path
    antigravity_provider_extension: Path
    timeout: float = 300
    kill_grace: float = 10
    # The credential refresh runs before every codex attempt, hangs for reasons
    # of its own (a locked keyring, a wedged network stack) and is the one call
    # a manual `run` cannot reach an outer guard for, so it carries a bound of
    # its own rather than sharing the attempt's.
    auth_timeout: float = PI_AUTH_TIMEOUT_SECONDS
    environment: Optional[Mapping[str, str]] = None
    # Optional run-log seam; ``None`` keeps every existing caller silent.
    logger: Optional[Callable[[str], None]] = None
    plugin_entries: Optional[Mapping[str, Path]] = None
    plugin_timeout: float = 15
    verify_plugins: bool = False
    capture_providers: Optional[frozenset] = None

    @classmethod
    def from_env(cls, environment: Optional[Mapping[str, str]] = None) -> "ModelRunnerConfig":
        env = os.environ if environment is None else environment
        home = Path(_env_value(env, "HOME", str(Path.home())))
        repo = Path(__file__).resolve().parents[2]
        return cls(
            pi_bin=Path(_env_value(env, "QUOTA_SENTINEL_PI_BIN", "/opt/homebrew/bin/pi")),
            auth_file=Path(
                _env_value(env, "QUOTA_SENTINEL_PI_AUTH_FILE", str(home / ".pi/agent/auth.json"))
            ),
            repo_dir=repo,
            antigravity_provider_extension=home / ".pi/agent/npm/node_modules/pi-antigravity/src/index.ts",
            timeout=_env_float(env, "QUOTA_SENTINEL_MODEL_TIMEOUT", 300),
            kill_grace=_env_float(env, "QUOTA_SENTINEL_MODEL_KILL_GRACE", 10),
            auth_timeout=_env_positive_float(
                env, "QUOTA_SENTINEL_PI_AUTH_TIMEOUT", PI_AUTH_TIMEOUT_SECONDS
            ),
            environment=environment,
            plugin_entries={p: Path(env["QUOTA_SENTINEL_"+p.upper()+"_PLUGIN"]) for p in ("antigravity", "clinepass") if env.get("QUOTA_SENTINEL_"+p.upper()+"_PLUGIN")},
            plugin_timeout=_env_positive_float(env, "QUOTA_SENTINEL_PI_PLUGIN_TIMEOUT", 15),
            verify_plugins=env.get("QUOTA_SENTINEL_VERIFY_PI_PLUGINS") == "1",
        )


@dataclass(frozen=True)
class PreparedPaths:
    agent_dir: Path
    stdout_path: Path
    stderr_path: Path
    quota_path: Path


@dataclass(frozen=True)
class AttemptResult:
    success: bool
    exit_code: int
    timed_out: bool
    elapsed: float
    stdout_path: Path
    stderr_path: Path
    quota_path: Path
    error_summary: str


@dataclass(frozen=True)
class PiProviderSpec:
    provider_id: str
    model: str
    thinking: str
    capture_env: Optional[str]
    capture_extension: Optional[Path]
    plugin_id: Optional[str]

_PI_PROVIDER = {
    'codex': PiProviderSpec('openai-codex','gpt-6-luna','off','PI_CODEX_QUOTA_FILE',Path('capture-codex-quota.ts'),None),
    'antigravity': PiProviderSpec('antigravity','gemini-3.7-flash','low','PI_ANTIGRAVITY_QUOTA_FILE',Path('capture-antigravity-quota.ts'),'pi-antigravity'),
    'opencode': PiProviderSpec('opencode-go','deepseek-v4.1-flash','off','PI_OPENCODE_QUOTA_FILE',Path('capture-opencode-quota.ts'),None),
    'clinepass': PiProviderSpec('clinepass','cline-pass/deepseek-v4.1-flash','off',None,None,'pi-clinepass-provider'),
}
PI_PROVIDERS = frozenset(_PI_PROVIDER)
_QUOTA_ENV_KEYS = tuple(spec.capture_env for spec in _PI_PROVIDER.values() if spec.capture_env)
_PROMPT = "不用思考，只回复我 1"


def _open_private(path: Path, *, append: bool = False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab" if append else "wb")


_STDERR_TAIL_BYTES = 8192


def _safe_error_summary(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "(no stderr output)"
    if size == 0:
        return "(no stderr output)"
    # `tail -n 3` semantics without slurping a provider's whole stderr file:
    # only the last ~8 KiB can matter, so seek there and read that window. Its
    # first line may be cut short; the third-from-last line is kept anyway.
    with path.open("rb") as handle:
        handle.seek(max(0, size - _STDERR_TAIL_BYTES))
        tail = handle.read().decode("utf-8", errors="replace")
    lines = tail.splitlines()[-3:]
    summary = " ".join(lines).strip()
    if not summary:
        return "(empty stderr)"
    # The shell collapses the three lines, cuts to 300 chars, then masks; doing
    # it in that order keeps the byte-identical summary and cannot expose a
    # credential that straddles the cut.
    summary = " ".join(summary.split())[:300]
    return _redact_secrets(summary) or "(empty stderr)"


# The shell's masking pass, which the separator-shaped patterns above do not
# cover: `perl -pe 's/(token|secret|authorization|bearer|password|api[_-]?key|sk-)\S*(\s+\S+)?/\1***/gi'`.
# It eats the keyword, the rest of that token, and one optional following
# whitespace-separated token -- so "bearer X", "token X" and bare "sk-X" are
# masked even when no `:` or `=` separator is present.
_SHELL_SECRET_RE = re.compile(
    r"(token|secret|authorization|bearer|password|api[_-]?key|sk-)\S*(\s+\S+)?",
    re.IGNORECASE,
)


def _redact_secrets(summary: str) -> str:
    summary = re.sub(r"(?i)\b(authorization)\s*[:=]\s*(?:bearer\s+)?\S+", r"\1: ***", summary)
    summary = re.sub(r"(?i)\b(bearer)\s+\S+", r"\1 ***", summary)
    summary = re.sub(
        r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|token|secret|password|api[_-]?key)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+",
        r"\1=***",
        summary,
    )
    summary = re.sub(r"(?i)\bsk-[a-z0-9._-]+", "sk-***", summary)
    return _SHELL_SECRET_RE.sub(r"\1***", summary)


# run_with_timeout.py's GNU-timeout convention: the helper ran the child, hit
# its own deadline, reaped the group and reported the expiry with this code.
_HELPER_TIMEOUT_EXIT = 124

# The last-resort reap reads the process table once; `ps` is bounded too, so a
# wedged reader cannot outlive the guard it serves.
_PROCESS_TABLE_TIMEOUT_SECONDS = 2


def _auth_refresh_timeout_line(seconds: float) -> bytes:
    """The one diagnostic a cut-short credential refresh leaves behind.

    It carries the deadline and nothing else: never a traceback and never the
    bearer token (which is read from the child's discarded stdout anyway).
    """
    return (
        "Pi auth refresh timed out after %gs; continuing without a fresh credential\n"
        % seconds
    ).encode()


def _sigkill(pid: int) -> None:
    """SIGKILL one process, and the group it leads when it leads one.

    The refresh helper gives its child its own session, so a survivor is a
    group leader whose group is exactly the tree below it -- signalling that
    group is what reaches grandchildren the walk has not named. The helper's
    own fail-safe is repeated here: never signal this process's group, even if
    a victim is reported to share it.
    """
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    if pgid == pid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _process_descendants(root: int) -> list[int]:
    """Every live process below ``root``, parents before children.

    ``ps`` is the portable process-table reader on both targets; a table that
    cannot be read (or holds nothing) only means the guard below has nothing
    extra to name, never an exception out of prepare(). The read is bounded like
    every other external process here: a wedged ``ps`` must not turn a
    last-resort reap into the very hang it exists to end.
    """
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="], capture_output=True, text=True,
            check=False, timeout=_PROCESS_TABLE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    queue = [root]
    while queue:
        for child in children.get(queue.pop(0), ()):
            found.append(child)
            queue.append(child)
    return found


def _reap_wedged_helper(pid: int) -> None:
    """Last resort for a refresh helper that outlived its own deadline.

    The parent-side guard is a real last resort, so it cannot assume the helper
    is alive enough to escalate for itself, and it cannot signal the helper's
    group either: the helper puts the refresh child in a session of its own.
    The tree is therefore read from the process table first, killed
    deepest-first, and the helper is signalled last so nothing is reparented
    before it is named.
    """
    for survivor in reversed(_process_descendants(pid)):
        _sigkill(survivor)
    _sigkill(pid)


class ModelRunner:
    """The Pi transport.

    ``fallback_for`` is the cheap-path-first policy: a provider listed there is
    delivered by Pi, and when Pi cannot deliver it the attempt is handed to the
    named transport instead of being reported as a failure. The fallback runner
    is expected to be terminal (no fallback of its own), so a chain can never
    loop, and it may be a different transport entirely.
    """

    TRANSPORT = "pi"

    def __init__(
        self,
        config: ModelRunnerConfig,
        *,
        logger: Optional[Callable[[str], None]] = None,
        fallback_for: Optional[Mapping[str, object]] = None,
    ) -> None:
        self.config = config
        # Mirrors the quota probe's seam: an injected logger, else the config's,
        # else a no-op so existing callers keep working unchanged.
        self.logger = logger or config.logger or (lambda _message: None)
        self.fallback_for = dict(fallback_for or {})
        self._prepared: dict[tuple[str, Path], PreparedPaths] = {}

    def _base_environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.config.environment is not None:
            env.update(self.config.environment)
        return env

    def _environment(self, provider: str, paths: PreparedPaths) -> dict[str, str]:
        env = self._base_environment()
        for key in _QUOTA_ENV_KEYS:
            env.pop(key, None)
        env.pop("ANTIGRAVITY_NO_PREWARM", None)
        env["PI_CODING_AGENT_DIR"] = str(paths.agent_dir)
        env["PI_OFFLINE"] = "1"
        if _PI_PROVIDER[provider].capture_env and (self.config.capture_providers is None or provider in self.config.capture_providers):
            env[_PI_PROVIDER[provider].capture_env] = str(paths.quota_path)
        if provider == "antigravity":
            env["ANTIGRAVITY_NO_PREWARM"] = "1"
        return env

    def prepare(self, provider: str, workspace: Path) -> PreparedPaths:
        if provider not in _PI_PROVIDER:
            raise ValueError(f"unknown model provider: {provider}")
        if _PI_PROVIDER[provider].plugin_id and (self.config.verify_plugins or provider == 'clinepass'):
            from .pi_plugins import check_pi_plugin
            from quota_sentinel.config import ConfigurationError
            check = check_pi_plugin(provider, self.config)
            if not check.available:
                raise ConfigurationError(check.reason)
        workspace = Path(os.path.abspath(workspace))
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        workspace.chmod(0o700)
        paths = PreparedPaths(
            agent_dir=workspace / f"{provider}-agent",
            stdout_path=workspace / f"{provider}-stdout",
            stderr_path=workspace / f"{provider}-stderr",
            quota_path=workspace / f"{provider}-quota.json",
        )
        paths.agent_dir.mkdir(mode=0o700, exist_ok=True)
        paths.agent_dir.chmod(0o700)
        if provider == "codex":
            # Pi may refresh OAuth here. Its bearer token must never enter a log.
            with open(os.devnull, "wb") as discard, _open_private(paths.stderr_path) as stderr:
                try:
                    self._refresh_pi_credentials(discard, stderr)
                except subprocess.TimeoutExpired:
                    # The parent-side guard fired, so the helper itself wedged
                    # and the refresh has been cut short. That stays non-fatal,
                    # exactly like the refresh's own timeout: one diagnosable
                    # line, then the credential already on disk is copied.
                    stderr.write(_auth_refresh_timeout_line(self.config.auth_timeout))
                except OSError as exc:
                    stderr.write(f"Pi auth refresh could not start: {exc.strerror or type(exc).__name__}\n".encode())
        try:
            with Path(self.config.auth_file).open("rb") as source, _open_private(paths.agent_dir / "auth.json") as dest:
                shutil.copyfileobj(source, dest)
        except OSError as exc:
            # The shell's `cp -p` is non-fatal at this call site: a missing or
            # rotated credential is left for the provider CLI to fail on, and
            # that failure is recorded as an ordinary failed attempt. The error
            # still reaches stderr, exactly like the shell's cp diagnostic.
            print(
                "cp: %s: %s" % (self.config.auth_file, exc.strerror or type(exc).__name__),
                file=sys.stderr,
            )
        settings = '{"transport":"sse"}\n' if provider == "codex" else '{}\n'
        with _open_private(paths.agent_dir / "settings.json") as dest:
            dest.write(settings.encode())
        self._prepared[(provider, workspace)] = paths
        return paths

    def _refresh_pi_credentials(self, discard, stderr) -> None:
        """Run the Pi credential refresh under the shared timeout helper.

        The refresh is the one provider call this module makes outside an
        attempt, and it used to run with no bound at all: a ``pi`` waiting on a
        locked keyring or a wedged network stack blocked prepare(), and with it
        every codex attempt -- including a manual ``run``, which has no outer
        guard to be rescued by. ``run_with_timeout.py`` gives the child a
        session of its own, SIGTERMs the whole group at ``auth_timeout``,
        escalates to SIGKILL after ``kill_grace`` and reaps it; the parent-side
        guard at ``auth_timeout + kill_grace`` repeats that deadline as a real
        last resort, and is the legal cost of one codex prepare() that the
        orchestrator's worst-case bound is built from.

        A refresh that is cut short is not an error for this call site: the
        caller reports the one bounded line and the attempt continues with the
        credential already on disk. The bearer token is never read -- the
        child's stdout is discarded, exactly as before.
        """
        helper = Path(self.config.repo_dir) / "run_with_timeout.py"
        command = [
            sys.executable, str(helper),
            "--timeout", str(self.config.auth_timeout),
            "--kill-grace", str(self.config.kill_grace),
            "--", str(self.config.pi_bin), "auth", "print-bearer-token",
            "--provider", "openai-codex",
        ]
        guard = self.config.auth_timeout + self.config.kill_grace
        # The helper's stderr is captured, not inherited: on either expiry path
        # the prepared stderr file must carry exactly one line, and on every
        # other path the bytes are copied through unchanged below, so the
        # child's own diagnostics still land where a direct invocation put
        # them. The capture is a private temporary file rather than a pipe: a
        # child that leaked a background process holding stderr could stall a
        # pipe read, while nothing here reads until the helper is gone.
        with tempfile.TemporaryFile() as captured:
            with subprocess.Popen(
                command,
                stdout=discard,
                stderr=captured,
                env=self._base_environment(),
            ) as process:
                try:
                    process.wait(timeout=guard)
                except subprocess.TimeoutExpired:
                    # The helper wedged before it could escalate, so the guard
                    # reaps the tree here; the caller writes the single line.
                    _reap_wedged_helper(process.pid)
                    process.kill()
                    process.wait()
                    raise
            if process.returncode == _HELPER_TIMEOUT_EXIT:
                # The helper timed out and reaped the group itself; its own
                # diagnostics are replaced by the one line the caller's
                # contract promises, so the file never grows two lines about a
                # single expiry.
                stderr.write(_auth_refresh_timeout_line(self.config.auth_timeout))
                return
            captured.seek(0)
            captured_bytes = captured.read()
        if captured_bytes:
            stderr.write(captured_bytes)

    def _command(self, provider: str) -> list[str]:
        spec = _PI_PROVIDER[provider]
        pi_provider, model, thinking = spec.provider_id, spec.model, spec.thinking
        command = [
            str(self.config.pi_bin), "--provider", pi_provider, "--model", model,
            "--thinking", thinking, "--mode", "text", "--print", "--no-session",
            "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-themes", "--no-context-files", "--no-approve", "--offline",
            "--system-prompt", "忽略上下文",
        ]
        if spec.plugin_id:
            from .pi_plugins import plugin_entry
            entry = plugin_entry(provider, self.config)
            if entry is None:
                raise ValueError("missing selected Pi plugin")
            command.extend(["--extension", str(entry)])
        if spec.capture_extension is not None and (self.config.capture_providers is None or provider in self.config.capture_providers):
            command.extend(["--extension", str(Path(self.config.repo_dir) / spec.capture_extension)])
        command.extend(["--", _PROMPT])
        return command

    def run(self, provider: str, workspace: Path, phase: str, attempt: int, limit: int) -> AttemptResult:
        if provider not in _PI_PROVIDER:
            raise ValueError(f"unknown model provider: {provider}")
        workspace = Path(os.path.abspath(workspace))
        paths = self._prepared.get((provider, workspace)) or self.prepare(provider, workspace)
        helper = Path(self.config.repo_dir) / "run_with_timeout.py"
        command = [
            sys.executable, str(helper), "--timeout", str(self.config.timeout),
            "--kill-grace", str(self.config.kill_grace), "--", *self._command(provider),
        ]
        started = time.monotonic()
        with _open_private(paths.stdout_path) as stdout, _open_private(paths.stderr_path, append=True) as stderr:
            completed = subprocess.run(
                command,
                cwd="/private/tmp",
                env=self._environment(provider, paths),
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
        elapsed = time.monotonic() - started
        # Shell command substitution strips trailing newlines, then tests exact
        # 1. Reading in text mode would translate CRLF to LF and accept "1\r\n",
        # which the shell scores as a failure; decode the raw bytes instead.
        output = paths.stdout_path.read_bytes().decode("utf-8", errors="replace").rstrip("\n")
        success = completed.returncode == 0 and output == "1"
        result = AttemptResult(
            success=success,
            exit_code=completed.returncode,
            timed_out=completed.returncode == 124,
            elapsed=elapsed,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            quota_path=paths.quota_path,
            error_summary="" if success else _safe_error_summary(paths.stderr_path),
        )
        self._log_attempt(provider, phase, attempt, limit, result)
        fallback = self.fallback_for.get(provider)
        if not success and fallback is not None:
            # Pi is the cheap path, not the only one: when it cannot deliver,
            # the configured transport takes the attempt rather than the window
            # being reported as a failure. The target is terminal by
            # construction, so this can never bounce back here.
            self.logger(
                "model %s transport=pi -> fallback=%s reason=%s"
                % (provider, getattr(fallback, "TRANSPORT", type(fallback).__name__),
                   "timeout" if result.timed_out else
                   ("exit=%s" % result.exit_code if result.exit_code else "reply!=1"))
            )
            fallback.prepare(provider, workspace)
            return fallback.run(provider, workspace, phase, attempt, limit)
        return result

    def _log_attempt(
        self, provider: str, phase: str, attempt: int, limit: int, result: AttemptResult
    ) -> None:
        """Emit the shell's per-attempt run-log lines.

        ``run_codex``/``run_antigravity``/``run_opencode`` each log exactly one
        result line per attempt -- plus one redacted error line on failure --
        with the elapsed time truncated to whole seconds. Other suites pin
        these strings, so the wording and the integer ``elapsed=<n>s`` matter.
        """
        elapsed = int(result.elapsed)
        if result.success:
            self.logger(
                "model %s phase=%s attempt=%s/%s result=success elapsed=%ss"
                % (provider, phase, attempt, limit, elapsed)
            )
            return
        if result.timed_out:
            self.logger(
                "model %s phase=%s attempt=%s/%s result=timeout rc=124 elapsed=%ss"
                % (provider, phase, attempt, limit, elapsed)
            )
        else:
            self.logger(
                "model %s phase=%s attempt=%s/%s result=failed rc=%s elapsed=%ss"
                % (provider, phase, attempt, limit, result.exit_code, elapsed)
            )
        self.logger(
            "model %s attempt=%s error: %s" % (provider, attempt, result.error_summary)
        )
