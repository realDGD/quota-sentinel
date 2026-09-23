"""Isolated Pi model attempts for the three quota-sentinel providers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional


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


@dataclass
class ModelRunnerConfig:
    pi_bin: Path
    auth_file: Path
    repo_dir: Path
    antigravity_provider_extension: Path
    timeout: float = 300
    kill_grace: float = 10
    environment: Optional[Mapping[str, str]] = None
    # Optional run-log seam; ``None`` keeps every existing caller silent.
    logger: Optional[Callable[[str], None]] = None

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
            environment=environment,
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


_PI_PROVIDER = {
    "codex": ("openai-codex", "gpt-5.6-luna", "off", "PI_CODEX_QUOTA_FILE"),
    "antigravity": ("antigravity", "gemini-3.7-flash", "low", "PI_ANTIGRAVITY_QUOTA_FILE"),
    # OpenCode Go is delivered by runtime/direct.py in production; this row
    # stays because the Pi transport remains selectable, and the model is kept
    # identical to the direct one so a transport A/B compares transports only.
    "opencode": ("opencode-go", "deepseek-v4.1-flash", "off", "PI_OPENCODE_QUOTA_FILE"),
}
_QUOTA_ENV_KEYS = tuple(item[3] for item in _PI_PROVIDER.values())
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


class ModelRunner:
    def __init__(
        self,
        config: ModelRunnerConfig,
        *,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        # Mirrors the quota probe's seam: an injected logger, else the config's,
        # else a no-op so existing callers keep working unchanged.
        self.logger = logger or config.logger or (lambda _message: None)
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
        env[_PI_PROVIDER[provider][3]] = str(paths.quota_path)
        if provider == "antigravity":
            env["ANTIGRAVITY_NO_PREWARM"] = "1"
        return env

    def prepare(self, provider: str, workspace: Path) -> PreparedPaths:
        if provider not in _PI_PROVIDER:
            raise ValueError(f"unknown model provider: {provider}")
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
                    subprocess.run(
                        [str(self.config.pi_bin), "auth", "print-bearer-token", "--provider", "openai-codex"],
                        stdout=discard,
                        stderr=stderr,
                        env=self._base_environment(),
                        check=False,
                    )
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

    def _command(self, provider: str) -> list[str]:
        pi_provider, model, thinking, _ = _PI_PROVIDER[provider]
        command = [
            str(self.config.pi_bin), "--provider", pi_provider, "--model", model,
            "--thinking", thinking, "--mode", "text", "--print", "--no-session",
            "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-themes", "--no-context-files", "--no-approve", "--offline",
            "--system-prompt", "忽略上下文",
        ]
        if provider == "antigravity":
            command.extend(["--extension", str(self.config.antigravity_provider_extension)])
        command.extend(["--extension", str(Path(self.config.repo_dir) / f"capture-{provider}-quota.ts")])
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
