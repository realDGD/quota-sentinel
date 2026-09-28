"""Codex-official task attempts: one tiny ``codex exec`` run per try.

The third transport. ``runtime.models.ModelRunner`` drives the Pi agent (a
third-party client) and ``runtime.direct.DirectRunner`` talks to a vendor HTTP
API with an API key; this one drives **OpenAI's own CLI** against the ChatGPT
Codex subscription. It exists because the transport decides not only cost but
also who the client appears to be, and the official client is the only one
whose use cannot be described as an unapproved client.

Measured on 2026-09-27 against codex-cli 0.157.1, model ``gpt-6-luna``,
``model_reasoning_effort="none"``:

* a default ``codex exec`` costs ~9,658 tokens per attempt;
* this profile costs ~1,687 (1,682 input / 5 output, reasoning 0) and the
  model still replies exactly ``1``, so the success criterion is unchanged.

The saving is entirely structural: Codex is an agent, and every turn otherwise
carries its agent scaffolding. ``PROFILE_OVERRIDES`` / ``PROFILE_DISABLED``
below are one canonical list, hashed into a fingerprint, so editing them
invalidates the smoke record automatically.

Two failure classes are treated differently, because they mean different
things:

* **functional** — non-zero exit, no ``turn.completed``, a completed turn whose
  usage reports no input tokens, a final message that is not ``1``, or
  reasoning tokens above zero: nothing was verified as delivered, so the
  attempt failed and the Pi transport is used instead;
* **cost regression** — the run succeeded but reports more input or output
  than the measured profile (a renamed feature flag, or a server-side change
  to the model metadata, silently reintroduces the scaffolding): the attempt is
  *still usable*, and by the time the numbers are known it is already used —
  the reply was delivered and the window anchored — so it is accepted as a
  success, logged as a regression, and **not** written down as a verified
  profile. Handing the same attempt to Pi would spend a second window on a turn
  that already succeeded and could only convert a success into a failure, so the
  ceiling stays an alarm rather than a second delivery.

The token counts come from the stream itself, so every attempt writes its own
cost into the run log. Nothing here has to be trusted to be observed — and no
verdict is ever read from an invented zero: a turn that reported no usage is
a functional failure (``usage=unverified``), not a free one.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from quota_sentinel.runtime.models import AttemptResult, PreparedPaths

__all__ = [
    "CODEX_EXEC_TIMEOUT_SECONDS",
    "CODEX_INPUT_CEILING",
    "CODEX_OUTPUT_CEILING",
    "CODEX_MODEL",
    "CODEX_PROVIDER",
    "PROFILE_DISABLED_FEATURES",
    "PROFILE_OVERRIDES",
    "SYSTEM_INSTRUCTIONS",
    "USER_PROMPT",
    "CodexExecConfig",
    "CodexExecRunner",
    "parse_events",
    "profile_fingerprint",
]

CODEX_PROVIDER = "codex"
CODEX_MODEL = "gpt-6-luna"
CODEX_EXEC_TIMEOUT_SECONDS = 120
# The measured profile reports 1,682 input / 5 output. The ceilings are the
# point where a run stops being "the profile we measured": a renamed feature
# flag or a server-side model change shows up here as a number, not as a guess.
CODEX_INPUT_CEILING = 2500
CODEX_OUTPUT_CEILING = 50
SYSTEM_INSTRUCTIONS = "Reply with exactly: 1"
USER_PROMPT = "1"

# ``-c key=value`` overrides (TOML syntax, exactly as the official docs
# require). model_instructions_file is per-run (it points into the workspace)
# so it is appended at call time.
PROFILE_OVERRIDES: Tuple[str, ...] = (
    'model="%s"' % CODEX_MODEL,
    'model_reasoning_effort="none"',
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_environment_context=false",
    "skills.include_instructions=false",
    "skills.bundled.enabled=false",
    "agents.enabled=false",
    'web_search="disabled"',
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
)

# ``--disable <feature>`` names, taken from `codex features list`. Every one of
# them exists in the installed CLI: an unknown flag is a hard error, so this
# list is also a compatibility statement about the version it was measured on.
PROFILE_DISABLED_FEATURES: Tuple[str, ...] = (
    "multi_agent", "multi_agent_v2", "shell_tool", "view_image", "sleep_tool",
    "apps", "plugins", "tool_suggest", "image_generation", "skill_search",
    "unified_exec", "request_permissions_tool", "standalone_web_search",
    "memories", "hooks", "token_budget", "shell_snapshot", "code_mode",
    "code_mode_host", "code_mode_only", "browser_use", "computer_use",
    "guardianv2", "goals", "search_tool", "context_management", "js_repl",
)


def profile_fingerprint() -> str:
    """A stable hash of the minimal profile.

    Version detection alone is not enough: the same CLI can build a different
    prompt after a server-side model metadata change, and a local edit to these
    lists can change it without any version change at all. Hashing the profile
    covers the local half of that; the per-attempt token ceilings cover the
    half nobody can see.
    """
    payload = json.dumps(
        {
            "overrides": list(PROFILE_OVERRIDES),
            "disabled": list(PROFILE_DISABLED_FEATURES),
            "instructions": SYSTEM_INSTRUCTIONS,
            "prompt": USER_PROMPT,
            "model": CODEX_MODEL,
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def parse_events(raw: str) -> Tuple[Optional[str], Optional[dict], int, bool]:
    """The final assistant text, the ``turn.completed`` usage, event count, and
    whether a ``turn.completed`` event was seen at all.

    ``codex exec --json`` writes one JSON object per line. Nothing about the
    product's success criterion is read from the raw stream: the caller decides
    what the parsed values mean.

    Presence is returned separately from the usage on purpose. ``turn.completed``
    is the only event that says the turn actually finished, and its usage is the
    only measurement of what the turn cost; folding the two into one optional
    dict would make "the turn never completed" indistinguishable from "the turn
    completed without reporting tokens", and the caller has to tell those apart
    to name the failure (``no turn.completed`` versus ``usage=unverified``).
    """
    final: Optional[str] = None
    usage: Optional[dict] = None
    events = 0
    completed = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        events += 1
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final = text
        elif kind == "turn.completed":
            completed = True
            value = event.get("usage")
            if isinstance(value, dict):
                usage = value
    return final, usage, events, completed


def _env_value(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    if value is None or value == "":
        return default
    return value


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key)
    if value is None or not value.strip().isdigit():
        return default
    return int(value)


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(_env_value(env, key, str(default)))
    except ValueError:
        return default


@dataclass
class CodexExecConfig:
    codex_bin: Path
    codex_home: Path
    state_dir: Path
    timeout: float = CODEX_EXEC_TIMEOUT_SECONDS
    kill_grace: float = 10
    input_ceiling: int = CODEX_INPUT_CEILING
    output_ceiling: int = CODEX_OUTPUT_CEILING
    environment: Optional[Mapping[str, str]] = None
    logger: Optional[Callable[[str], None]] = None

    @classmethod
    def from_env(
        cls,
        environment: Optional[Mapping[str, str]] = None,
        *,
        state_dir: Path,
    ) -> "CodexExecConfig":
        env = os.environ if environment is None else environment
        home = Path(_env_value(env, "HOME", str(Path.home())))
        return cls(
            codex_bin=Path(_env_value(env, "QUOTA_SENTINEL_CODEX_BIN", "/opt/homebrew/bin/codex")),
            codex_home=Path(_env_value(env, "QUOTA_SENTINEL_CODEX_HOME", str(home / ".codex"))),
            state_dir=Path(state_dir),
            timeout=_env_float(env, "QUOTA_SENTINEL_CODEX_TIMEOUT", CODEX_EXEC_TIMEOUT_SECONDS),
            kill_grace=_env_float(env, "QUOTA_SENTINEL_CODEX_KILL_GRACE", 10),
            input_ceiling=_env_int(env, "QUOTA_SENTINEL_CODEX_INPUT_CEILING", CODEX_INPUT_CEILING),
            output_ceiling=_env_int(env, "QUOTA_SENTINEL_CODEX_OUTPUT_CEILING", CODEX_OUTPUT_CEILING),
            environment=environment,
        )


@dataclass(frozen=True)
class _Attempt:
    """One parsed ``codex exec --json`` run, before any policy is applied.

    ``turn_completed`` and ``usage_verified`` are carried next to ``usage``
    rather than derived from it, because ``usage`` has to invent a zero for
    every number the stream did not report, and a verdict must not be read from
    an invention: the two booleans are what says whether a number is a
    measurement at all.
    """

    final_response: Optional[str]
    usage: Dict[str, int]
    events: int
    exit_code: int
    timed_out: bool
    elapsed: float
    turn_completed: bool
    usage_verified: bool


class CodexExecRunner:
    """``prepare``/``run`` for the codex transport, with an optional Pi fallback."""

    TRANSPORT = "codex"

    def __init__(
        self,
        config: CodexExecConfig,
        *,
        logger: Optional[Callable[[str], None]] = None,
        fallback: Optional[object] = None,
    ) -> None:
        self.config = config
        self.logger = logger or config.logger or (lambda _message: None)
        # The Pi runner, used when this profile stops being the profile we
        # measured. Optional so the transport can be exercised on its own.
        self.fallback = fallback
        self._prepared: dict[tuple[str, Path], PreparedPaths] = {}

    # ------------------------------------------------------------------ paths
    def _profile_path(self) -> Path:
        return Path(self.config.state_dir) / "codex-exec-profile.json"

    def _instructions_path(self, provider: str, workspace: Path) -> Path:
        return Path(workspace) / f"{provider}-instructions.txt"

    def prepare(self, provider: str, workspace: Path) -> PreparedPaths:
        if provider != CODEX_PROVIDER:
            raise ValueError(f"codex transport does not serve {provider!r}")
        workspace = Path(os.path.abspath(workspace))
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        workspace.chmod(0o700)
        agent_dir = workspace / f"{provider}-codex"
        agent_dir.mkdir(mode=0o700, exist_ok=True)
        agent_dir.chmod(0o700)
        # An empty cwd, exactly like the Pi transport: no AGENTS.md, no repo.
        (agent_dir / "cwd").mkdir(mode=0o700, exist_ok=True)
        instructions = self._instructions_path(provider, workspace)
        with open(os.open(instructions, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
            handle.write((SYSTEM_INSTRUCTIONS + "\n").encode())
        paths = PreparedPaths(
            agent_dir=agent_dir,
            stdout_path=workspace / f"{provider}-stdout",
            stderr_path=workspace / f"{provider}-stderr",
            quota_path=workspace / f"{provider}-quota.json",
        )
        self._prepared[(provider, workspace)] = paths
        return paths

    # -------------------------------------------------------------- execution
    def _environment(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.config.environment is not None:
            env.update(self.config.environment)
        env["CODEX_HOME"] = str(self.config.codex_home)
        # The Codex CLI falls back to the platform API when an API key is
        # present, and a ChatGPT model then 401s there instead of running. The
        # subscription credential is the only one this transport means to use.
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(key, None)
        return env

    def codex_version(self) -> Optional[str]:
        try:
            completed = subprocess.run(
                [str(self.config.codex_bin), "--version"],
                capture_output=True, timeout=20, check=False,
                env=self._environment(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        text = completed.stdout.decode("utf-8", "replace").strip()
        return text or None

    def _command(self, provider: str, workspace: Path) -> list:
        instructions = self._instructions_path(provider, workspace)
        command = [
            str(self.config.codex_bin), "exec", "--json", "--ephemeral",
            "--ignore-user-config", "--skip-git-repo-check",
        ]
        for override in PROFILE_OVERRIDES:
            command.extend(["-c", override])
        command.extend(["-c", f'model_instructions_file="{instructions}"'])
        for feature in PROFILE_DISABLED_FEATURES:
            command.extend(["--disable", feature])
        command.append(USER_PROMPT)
        return command

    def _run_codex(self, provider: str, workspace: Path, paths: PreparedPaths) -> _Attempt:
        helper = Path(__file__).resolve().parents[2] / "run_with_timeout.py"
        command = [
            sys.executable, str(helper), "--timeout", str(self.config.timeout),
            "--kill-grace", str(self.config.kill_grace), "--",
            *self._command(provider, workspace),
        ]
        started = time.monotonic()
        with open(paths.stdout_path, "wb") as stdout, open(paths.stderr_path, "ab") as stderr:
            completed = subprocess.run(
                command,
                cwd=str(paths.agent_dir / "cwd"),
                env=self._environment(),
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
        elapsed = time.monotonic() - started
        raw = paths.stdout_path.read_bytes().decode("utf-8", "replace")
        final, usage, events, turn_completed = parse_events(raw)
        numbers = {
            "input": int(usage.get("input_tokens") or 0) if usage else 0,
            "cached": int(usage.get("cached_input_tokens") or 0) if usage else 0,
            "output": int(usage.get("output_tokens") or 0) if usage else 0,
            "reasoning": int(usage.get("reasoning_output_tokens") or 0) if usage else 0,
        }
        numbers["total"] = numbers["input"] + numbers["output"]
        return _Attempt(
            final_response=final,
            usage=numbers,
            events=events,
            exit_code=completed.returncode,
            timed_out=completed.returncode == 124,
            elapsed=elapsed,
            turn_completed=turn_completed,
            # ``input_tokens`` is the number every ceiling in this module is
            # stated against, so a completion that does not report it cannot be
            # checked against any of them. Such a turn is not a verified success
            # however good its reply looks: the zeros in ``numbers`` are the
            # absence of a measurement, not a measurement of zero.
            usage_verified=turn_completed and numbers["input"] >= 1,
        )

    # ------------------------------------------------------------- smoke state
    def _read_smoke(self) -> Dict[str, object]:
        try:
            return json.loads(self._profile_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_smoke(self, version: Optional[str], usage: Mapping[str, int]) -> None:
        path = self._profile_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": version,
                        "fingerprint": profile_fingerprint(),
                        "usage": dict(usage),
                        "checked_at": int(time.time()),
                    },
                    handle,
                    ensure_ascii=False,
                )
            os.chmod(name, 0o600)
            os.replace(name, path)
        except OSError:
            # The record is diagnostics, not correctness: a read-only state
            # directory must not fail an attempt that already succeeded.
            pass

    # ------------------------------------------------------------------ policy
    def _functional_problems(self, attempt: _Attempt) -> list:
        problems = []
        if attempt.timed_out:
            problems.append("timeout")
        elif attempt.exit_code != 0:
            problems.append("exit=%s" % attempt.exit_code)
        # A reply is only evidence of a finished turn when the turn says it
        # finished: a stream that ends after an agent message can be a killed
        # process, a truncated pipe, or a CLI that changed its event names, and
        # all three look like a perfect answer.
        if not attempt.turn_completed:
            problems.append("no turn.completed")
        elif not attempt.usage_verified:
            # The turn finished but reported no input tokens. The profile's whole
            # claim is a number of input tokens, so a turn that reports none is
            # unverifiable rather than free: treating the missing value as zero
            # would silently certify an unmeasured run.
            problems.append("usage=unverified")
        if attempt.final_response is None:
            problems.append("no agent message")
        elif attempt.final_response.strip() != USER_PROMPT:
            problems.append("reply=%r" % attempt.final_response.strip()[:40])
        if attempt.usage.get("reasoning"):
            problems.append("reasoning=%s" % attempt.usage["reasoning"])
        return problems

    def _cost_regressions(self, attempt: _Attempt) -> list:
        # Only a verified usage can be above a ceiling. An unverified one is
        # already a functional failure, and its invented zeros must not be the
        # thing that decides whether the profile regressed.
        if not attempt.usage_verified:
            return []
        problems = []
        if attempt.usage.get("input", 0) >= self.config.input_ceiling:
            problems.append(
                "input=%s>=%s" % (attempt.usage["input"], self.config.input_ceiling)
            )
        if attempt.usage.get("output", 0) >= self.config.output_ceiling:
            problems.append(
                "output=%s>=%s" % (attempt.usage["output"], self.config.output_ceiling)
            )
        return problems

    def _log_attempt(
        self, provider: str, phase: str, attempt: int, limit: int,
        result: _Attempt, outcome: str,
    ) -> None:
        numbers = result.usage
        self.logger(
            "model %s transport=codex phase=%s attempt=%s/%s result=%s "
            "input=%s cached=%s output=%s reasoning=%s elapsed=%ss"
            % (provider, phase, attempt, limit, outcome,
               numbers.get("input"), numbers.get("cached"), numbers.get("output"),
               numbers.get("reasoning"), int(result.elapsed))
        )

    def _fallback_to_pi(
        self, provider: str, workspace: Path, phase: str, attempt: int, limit: int,
        reason: str,
    ) -> Optional[AttemptResult]:
        if self.fallback is None:
            return None
        self.logger(
            "model %s transport=codex -> fallback=pi reason=%s" % (provider, reason)
        )
        self.fallback.prepare(provider, workspace)
        return self.fallback.run(provider, workspace, phase, attempt, limit)

    def run(self, provider: str, workspace: Path, phase: str, attempt: int, limit: int) -> AttemptResult:
        if provider != CODEX_PROVIDER:
            raise ValueError(f"codex transport does not serve {provider!r}")
        workspace = Path(os.path.abspath(workspace))
        paths = self._prepared.get((provider, workspace)) or self.prepare(provider, workspace)
        version = self.codex_version()
        previous = self._read_smoke()
        changed = (
            previous.get("version") != version
            or previous.get("fingerprint") != profile_fingerprint()
        )
        if changed:
            self.logger(
                "model %s transport=codex profile changed (version %s -> %s, fingerprint %s -> %s); "
                "this attempt is the smoke test"
                % (provider, previous.get("version"), version,
                   previous.get("fingerprint"), profile_fingerprint())
            )

        result = self._run_codex(provider, workspace, paths)
        functional = self._functional_problems(result)
        regression = self._cost_regressions(result)

        if not functional and not regression:
            self._write_smoke(version, result.usage)
            self._log_attempt(provider, phase, attempt, limit, result, "success")
            return AttemptResult(
                success=True,
                exit_code=result.exit_code,
                timed_out=False,
                elapsed=result.elapsed,
                stdout_path=paths.stdout_path,
                stderr_path=paths.stderr_path,
                quota_path=paths.quota_path,
                error_summary="",
            )

        if not functional:
            # Cost regression on an otherwise good turn: accepted, never
            # re-delivered. The reply was already handed to the caller and the
            # window already anchored, so Pi can only add a second charge for
            # the same work — and, when it fails, turn a delivered message into
            # a reported failure that makes the scheduler retry the provider and
            # spend a third time. The ceiling therefore stays an alarm about the
            # profile, not a second delivery.
            self._log_attempt(provider, phase, attempt, limit, result, "cost-regression")
            self.logger(
                "model %s transport=codex cost regression (%s); the delivery is accepted, "
                "and the profile needs attention: expected input<%s output<%s — "
                "the minimal profile no longer applies"
                % (provider, ",".join(regression),
                   self.config.input_ceiling, self.config.output_ceiling)
            )
            # Deliberately no ``_write_smoke``: a regressed profile is not a
            # verified one, so the next attempt must measure again instead of
            # trusting a number that just proved wrong.
            return AttemptResult(
                success=True,
                exit_code=result.exit_code,
                timed_out=False,
                elapsed=result.elapsed,
                stdout_path=paths.stdout_path,
                stderr_path=paths.stderr_path,
                quota_path=paths.quota_path,
                error_summary="",
            )

        reason = "functional:" + ",".join(functional)
        self._log_attempt(provider, phase, attempt, limit, result, "failed")
        fallback = self._fallback_to_pi(provider, workspace, phase, attempt, limit, reason)
        if fallback is not None:
            return fallback
        return AttemptResult(
            success=False,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            elapsed=result.elapsed,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            quota_path=paths.quota_path,
            error_summary=reason,
        )
