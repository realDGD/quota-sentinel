"""Antigravity task attempts: one minimal ``agy -p`` run per try.

The fourth transport. ``runtime.models.ModelRunner`` drives the Pi agent,
``runtime.direct.DirectRunner`` talks to a vendor HTTP API, and
``runtime.codex_exec.CodexExecRunner`` drives OpenAI's own CLI; this one drives
**Google's own Antigravity CLI** against the Antigravity subscription. For
antigravity it is the SHIPPED path, with Pi as its one-hop fallback: the same
shape the codex provider has, in the other direction.

The saving is entirely structural. ``agy`` is an agent, so every turn otherwise
carries its agent scaffolding. Measured on 2026-09-27 against agy 1.2.12, model
``gemini-3.8-flash-low``:

* a stock ``agy -p "1"`` costs 22,311 input / 28 output tokens, of which ~20.3k
  is the schema of the 57 built-in tools alone;
* this profile costs 564 input / 1 output / 0 thinking, and the model still
  replies exactly ``1``, so the success criterion is unchanged.

The profile is one markdown agent (``AGENT_MARKER``), written per attempt into
an empty cwd so nothing else can be discovered alongside it:
``excludeDefaultComponents: true`` drops the default prompt sections and the
built-in tools, ``inheritCustomizations: false`` keeps the machine's rules,
skills, plugins, subagents and MCP servers out, and the body is one line.
``profile_fingerprint`` hashes it, so editing the profile invalidates the smoke
record automatically.

Read-only slash commands are answered by the CLI itself and cost nothing
(measured: ``input_tokens`` 0, ``num_turns`` 0), which is what makes two free
guards possible before any token is spent:

* **agent presence** — the agent is confirmed with ``agy -p /agents``. Without
  this check a renamed or undiscoverable agent is not an error at all: the
  *default* agent answers, at ~40x the cost. The cost ceiling would catch it,
  but only after the tokens were gone.
* **transient handshake** — ``Eligibility check failed`` happens before the turn
  starts and reports 0 tokens (measured 6 failures in a row during one burst),
  so it is retried rather than being reported as a delivery failure. Two fences
  keep that retry honest: only the CURRENT turn's stderr is read (the file is
  append-only across turns, so an old marker would otherwise make a later,
  unrelated failure look free), and the current turn must have spent nothing —
  a handshake fails before the turn starts, so anything that reported tokens is
  not a free retry.

Two failure classes are treated differently, exactly as in the codex transport:

* **functional** — non-zero exit, a non-``SUCCESS`` status, a reply that is not
  ``1``, a missing agent, or a ``SUCCESS`` turn whose ``usage`` cannot prove it
  spent input (``input_tokens >= 1``): the attempt failed and the Pi transport is
  used instead. The last one is not pedantry — the input ceiling is the only
  structural proof that the 564-token profile rather than the 22,311-token stock
  agent answered, so an unverifiable ignition is not a verified success;
* **cost regression** — the run succeeded but reports more input than the
  measured profile, or a runaway reply (a renamed frontmatter key, a dropped
  ``--agent``, or a server-side change that reintroduces the scaffolding): the
  attempt is **accepted as a success**. The reply was already delivered and the
  window already anchored, so handing the same attempt to Pi would spend MORE
  quota to reach the same fact and could only turn a success into a failure
  (which the scheduler would then pay for again). It is logged as a regression
  and deliberately NOT recorded as a verified profile, so the next attempt
  re-tests the profile instead of trusting it. Thinking tokens are reported and
  not policed — see the ceilings below.

One deliberate omission: this transport writes no quota snapshot for
``AttemptResult.quota_path``. The Pi transport's capture file is normalized as
a *Pi* document, and fabricating that shape from the CLI's own ``/usage``
payload would be a different client's claim in Pi's clothing. Antigravity's
tier-1 native probe already reads that same ``/usage`` payload for free in the
same tick, so the reading is not lost — only the redundant copy is.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from quota_sentinel.runtime.models import AttemptResult, PreparedPaths

__all__ = [
    "AGENT_MARKER",
    "AGENT_NAME",
    "AGY_EFFORT",
    "AGY_EXEC_TIMEOUT_SECONDS",
    "AGY_INPUT_CEILING",
    "AGY_MODEL",
    "AGY_OUTPUT_CEILING",
    "AGY_PROVIDER",
    "AGY_TRANSIENT_RETRIES",
    "SYSTEM_PROMPT_BODY",
    "TRANSIENT_MARKERS",
    "USER_PROMPT",
    "AgyExecConfig",
    "AgyExecRunner",
    "agent_document",
    "document_error",
    "is_transient_error",
    "parse_result",
    "profile_fingerprint",
]

AGY_PROVIDER = "antigravity"
AGY_MODEL = "gemini-3.8-flash-low"
AGY_EFFORT = "low"
AGY_EXEC_TIMEOUT_SECONDS = 120
# The agent's own name, as the CLI lists it and as --agent selects it.
AGENT_NAME = "quota-primer"
# The fence that keeps the profile honest: the CLI's project-agent name is the
# directory name, and a rename is a different agent, not a different spelling.
AGENT_MARKER = "excludeDefaultComponents"
# Measured: 564 input / 1 output / 0 thinking. The ceilings are where a run
# stops being "the profile we measured" — the stock agent path is 22,311 input
# and a conversational reply, so the gap between the two is enormous and the
# ceiling can sit far from both.
#
# Thinking is REPORTED but deliberately not policed. Unlike the codex profile,
# where reasoning is explicitly switched off and any of it means the switch
# stopped applying, `--effort low` still leaves thinking to the model's
# discretion: identical invocations of this profile measured 0 and 34 thinking
# tokens. Failing on that would hand a fifth of all attempts to the fallback for
# no reason. What proves the profile is intact is the INPUT side, which is
# structural — and the output ceiling is set to catch a runaway reply, not to
# police a model's mood.
AGY_INPUT_CEILING = 1500
AGY_OUTPUT_CEILING = 200
# The baseline the profile is measured against: a stock `agy -p "1"` with the
# default agent and its 57 tools, measured on 2026-09-27 against agy 1.2.12.
# It exists so the run log can state the saving instead of leaving an operator
# to remember it, and so a future edit that gives the saving away is visible as
# a number on every attempt.
AGY_STOCK_INPUT_TOKENS = 22311
AGY_TRANSIENT_RETRIES = 3
USER_PROMPT = "1"
SYSTEM_PROMPT_BODY = "Reply with exactly the single character: 1"

# The handshake failure the CLI reports when its own eligibility probe cannot
# reach the backend. It happens BEFORE a turn starts, so it costs nothing to
# retry — measured `input_tokens` 0 on every occurrence.
TRANSIENT_MARKERS: Tuple[str, ...] = (
    "eligibility check failed",
    "service unavailable",
    "connection reset",
    "deadline exceeded",
    "eof",
)

# The alternate sign-in path. This transport exists to spend the Antigravity
# *subscription* quota and to anchor its window; an API key would bill a
# different product and would not be the same fact. Same reasoning, and the
# same shape, as the codex transport's API-key removal.
API_KEY_VARS: Tuple[str, ...] = ("GEMINI_API_KEY",)

# The free ``/agents`` guard is bounded TWICE, and the two numbers are not the
# same one. The wrapper gets the SHORTER deadline and gives the CLI its own
# session, so the wrapper is normally the one that kills and reaps the CLI;
# the parent holds a later, last-resort deadline for the case where the wrapper
# itself wedges, and reaps the whole tree when it fires. Handing both the SAME
# deadline — which is what this guard used to do — meant the parent killed the
# wrapper (a Python process) at the very moment the wrapper began its own
# SIGTERM→SIGKILL escalation, so a CLI that ignores SIGTERM outlived the
# bounded call: one leaked CLI per timed-out guard, against the promise that
# every external process is bounded and cleaned up.
#
# The floors keep a tiny or zero-grace config from collapsing the two deadlines
# back into one: a guard shorter than ~0.2s cannot bound a Python interpreter's
# startup anyway, and a zero kill grace would hand the wrapper the same deadline
# as the parent, which is the defect being fenced here.
_GUARD_MIN_TOTAL_SECONDS = 0.2
_GUARD_MIN_DEADLINE_SECONDS = 0.1
_GUARD_MIN_GRACE_SECONDS = 0.1
# How long the parent waits past the wrapper's own worst case (its deadline plus
# its kill grace) before it stops trusting the wrapper and reaps the tree itself.
# It is also the reap wait after that SIGKILL.
_GUARD_REAP_MARGIN_SECONDS = 1.0


def agent_document() -> str:
    """The whole minimal profile, as the markdown the CLI loads.

    ``excludeDefaultComponents`` removes the default prompt sections and every
    built-in tool; ``inheritCustomizations: false`` refuses the machine's own
    rules, skills, plugins, subagents and MCP servers. Together they are what
    turns a 22,311-token turn into a 564-token one, and the body is deliberately
    one instruction: it is the only part of the prompt we choose.
    """
    return (
        "---\n"
        "name: %s\n"
        "description: Anchors the Antigravity quota window with the fewest tokens\n"
        "mainAgent: true\n"
        "subagent: false\n"
        "excludeDefaultComponents: true\n"
        "inheritCustomizations: false\n"
        "tools: []\n"
        "---\n"
        "\n"
        "# Role\n"
        "%s\n" % (AGENT_NAME, SYSTEM_PROMPT_BODY)
    )


def profile_fingerprint() -> str:
    """A stable hash of the minimal profile.

    Version detection alone is not enough: the same CLI can build a different
    prompt after a server-side model-metadata change, and a local edit to the
    agent document can change it without any version change at all. Hashing the
    profile covers the local half of that; the per-attempt token ceilings cover
    the half nobody can see.
    """
    payload = json.dumps(
        {
            "agent": agent_document(),
            "name": AGENT_NAME,
            "model": AGY_MODEL,
            "effort": AGY_EFFORT,
            "prompt": USER_PROMPT,
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def parse_result(raw: str) -> Tuple[Optional[str], Optional[str], Optional[dict], List[str]]:
    """``(status, response, usage, problems)`` from one ``--output-format json`` run.

    The CLI prints exactly one JSON object on stdout and keeps its own logging
    on stderr, but stdout is still external input: anything that is not a JSON
    object is reported as a problem instead of raising.
    """
    text = raw.strip()
    if not text:
        return None, None, None, ["no output"]
    try:
        document = json.loads(text)
    except ValueError:
        return None, None, None, ["stdout is not JSON"]
    if not isinstance(document, dict):
        return None, None, None, ["stdout is not a JSON object"]
    status = document.get("status")
    response = document.get("response")
    usage = document.get("usage")
    problems: List[str] = []
    if not isinstance(status, str):
        problems.append("no status")
        status = None
    if response is not None and not isinstance(response, str):
        problems.append("response is not text")
        response = None
    if not isinstance(usage, dict):
        if usage is not None:
            problems.append("usage is not an object")
        usage = None
    return status, response, usage, problems


def is_transient_error(*texts: Optional[str]) -> bool:
    """Whether a failure is the pre-turn handshake, which costs nothing to retry.

    A pure marker matcher on the text it is given: the two conditions that make a
    marker mean "free retry" — the text must be the CURRENT turn's stderr, and
    the turn must have spent nothing — are decided by the caller, which is the
    only place that knows both.
    """
    joined = " ".join(text for text in texts if text).lower()
    return any(marker in joined for marker in TRANSIENT_MARKERS)


def _saved_percent(input_tokens: int) -> float:
    """How much of the stock agent's input this ignition avoided, in percent.

    Arithmetic on two measured numbers, not a claim about the future: the
    baseline is the constant above, so the line stops reporting the saving the
    day the profile stops earning it.
    """
    if AGY_STOCK_INPUT_TOKENS <= 0:
        return 0.0
    return 100.0 * (AGY_STOCK_INPUT_TOKENS - input_tokens) / AGY_STOCK_INPUT_TOKENS


def _usage_numbers(usage: Optional[Mapping[str, object]]) -> Dict[str, int]:
    numbers: Dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"):
        value = (usage or {}).get(key)
        numbers[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    numbers["output_total"] = numbers["output_tokens"] + numbers["thinking_tokens"]
    return numbers


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
class AgyExecConfig:
    agy_bin: Path
    state_dir: Path
    timeout: float = AGY_EXEC_TIMEOUT_SECONDS
    kill_grace: float = 10
    input_ceiling: int = AGY_INPUT_CEILING
    output_ceiling: int = AGY_OUTPUT_CEILING
    transient_retries: int = AGY_TRANSIENT_RETRIES
    preflight: bool = True
    environment: Optional[Mapping[str, str]] = None
    logger: Optional[Callable[[str], None]] = None

    @classmethod
    def from_env(
        cls,
        environment: Optional[Mapping[str, str]] = None,
        *,
        state_dir: Path,
    ) -> "AgyExecConfig":
        env = os.environ if environment is None else environment
        return cls(
            agy_bin=Path(_env_value(env, "QUOTA_SENTINEL_AGY_BIN", "/opt/homebrew/bin/agy")),
            state_dir=Path(state_dir),
            timeout=_env_float(env, "QUOTA_SENTINEL_AGY_TIMEOUT", AGY_EXEC_TIMEOUT_SECONDS),
            kill_grace=_env_float(env, "QUOTA_SENTINEL_AGY_KILL_GRACE", 10),
            input_ceiling=_env_int(env, "QUOTA_SENTINEL_AGY_INPUT_CEILING", AGY_INPUT_CEILING),
            output_ceiling=_env_int(env, "QUOTA_SENTINEL_AGY_OUTPUT_CEILING", AGY_OUTPUT_CEILING),
            transient_retries=_env_int(env, "QUOTA_SENTINEL_AGY_TRANSIENT_RETRIES", AGY_TRANSIENT_RETRIES),
            environment=environment,
        )


@dataclass(frozen=True)
class _Attempt:
    """One parsed ``agy -p`` run, before any policy is applied."""

    status: Optional[str]
    response: Optional[str]
    usage: Dict[str, int]
    problems: List[str]
    exit_code: int
    timed_out: bool
    elapsed: float
    transient: bool = False


class AgyExecRunner:
    """``prepare``/``run`` for the agy transport, with an optional Pi fallback."""

    TRANSPORT = "agy"

    def __init__(
        self,
        config: AgyExecConfig,
        *,
        logger: Optional[Callable[[str], None]] = None,
        fallback: Optional[object] = None,
    ) -> None:
        self.config = config
        self.logger = logger or config.logger or (lambda _message: None)
        # The Pi runner, used when this profile stops being the profile we
        # measured, or when the CLI cannot deliver at all. Optional so the
        # transport can be exercised on its own.
        self.fallback = fallback
        self._prepared: dict = {}

    # ------------------------------------------------------------------ paths
    def _profile_path(self) -> Path:
        return Path(self.config.state_dir) / "agy-exec-profile.json"

    def _agent_path(self, agent_dir: Path) -> Path:
        return Path(agent_dir) / "cwd" / ".agents" / "agents" / AGENT_NAME / "agent.md"

    def prepare(self, provider: str, workspace: Path) -> PreparedPaths:
        if provider != AGY_PROVIDER:
            raise ValueError(f"agy transport does not serve {provider!r}")
        workspace = Path(os.path.abspath(workspace))
        from quota_sentinel.platform.files import private_directory
        private_directory(workspace)
        agent_dir = workspace / f"{provider}-agy"
        private_directory(agent_dir)
        # An empty cwd, exactly like the Pi and codex transports: no AGENTS.md,
        # no repository, and therefore nothing this run can discover that the
        # profile did not put there. The agent lives inside it because a
        # project agent is only found by walking up from the cwd.
        cwd = agent_dir / "cwd"
        private_directory(cwd)
        agent_path = self._agent_path(agent_dir)
        private_directory(agent_path.parent)
        from quota_sentinel.platform.files import private_open
        with private_open(agent_path,"wb") as handle:
            handle.write(agent_document().encode())
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
        for key in API_KEY_VARS:
            env.pop(key, None)
        return env

    def _cwd(self, paths: PreparedPaths) -> Path:
        return Path(paths.agent_dir) / "cwd"

    def _helper_command(
        self,
        inner: List[str],
        *,
        timeout: Optional[float] = None,
        kill_grace: Optional[float] = None,
        log_path: Optional[Path] = None,
    ) -> List[str]:
        """One CLI invocation, wrapped by ``run_with_timeout.py``.

        The turn path passes no overrides: there the wrapper's deadline IS the
        bound, which is why ``_run_cli`` gives ``subprocess.run`` no deadline of
        its own — the wrapper therefore always outlives its own deadline and
        gets to do its own process-group cleanup. Only the guard overrides both
        numbers, because only the guard also holds a parent-side deadline; see
        ``_guard_deadlines`` for why the wrapper's must be the shorter one.
        """
        from quota_sentinel.helpers import resource_path
        helper = resource_path("run_with_timeout.py")
        deadline = self.config.timeout if timeout is None else timeout
        grace = self.config.kill_grace if kill_grace is None else kill_grace
        log_option = [] if log_path is None else ['--agy-log-file', str(log_path)]
        return [
            sys.executable, str(helper), "--timeout", str(deadline),
            "--kill-grace", str(grace), "--agy-auth-guard", *log_option, "--", *inner,
        ]

    def _run_cli(self, inner: List[str], paths: PreparedPaths):
        """Run one CLI invocation; stdout is per-turn, stderr accumulates.

        stdout is TRUNCATED for every turn, including a retry: the CLI prints
        exactly one JSON object there, and appending a second one would make the
        file unparseable — a transient retry would then be reported as a
        malformed reply instead of the success it was. stderr is appended
        because the CLI's own logging is the diagnosis, and it is not parsed.

        The size of stderr is measured BEFORE the run and returned with the exit
        code, so the caller can read back exactly the bytes this turn appended.
        The file stays append-only; only the slice is per-turn. Without that, a
        marker written by any earlier turn would still be in the tail and would
        make a later, unrelated failure look like the free handshake.
        """
        started = time.monotonic()
        stderr_offset = _file_size(paths.stderr_path)
        from quota_sentinel.platform.process import run_bounded,CLEANUP_ALLOWANCE_SECONDS,CommandResult
        from quota_sentinel.platform.files import private_open
        from quota_sentinel.platform.background_auth import AgyAuthGuard
        try:
            with AgyAuthGuard() as guard:
                command = self._helper_command(inner, log_path=guard.log)
                completed=run_bounded(command,cwd=self._cwd(paths),environment=self._environment(),
                    timeout=self.config.timeout+self.config.kill_grace+CLEANUP_ALLOWANCE_SECONDS,kill_grace=0)
        except (OSError, ValueError):
            # Guard setup failures use the same configured fallback as CLI failures.
            completed = CommandResult(b'', b'background agy command unavailable\n', 127, False)
        with private_open(paths.stdout_path,'wb') as stdout,private_open(paths.stderr_path,'ab') as stderr:
            stdout.write(completed.stdout);stderr.write(completed.stderr)
        return completed.returncode,time.monotonic()-started,stderr_offset

    def agy_version(self) -> Optional[str]:
        from quota_sentinel.platform.process import run_bounded
        from quota_sentinel.platform.paths import resolve_launcher
        try:
            result=run_bounded((*resolve_launcher('agy',explicit=self.config.agy_bin),'--version'),
                cwd=Path.cwd(),environment=self._environment(),timeout=20,kill_grace=0,max_bytes=4096)
        except (OSError,ValueError):return None
        text=result.stdout.decode('utf-8','replace').strip().splitlines()
        return text[0][:64] if not result.returncode and text else None

    def _command(self) -> List[str]:
        # ``--print-timeout`` takes a Go duration string; a bare number is a
        # hard usage error. It is set just inside the outer wrapper so the CLI
        # can end its own turn and still exit 0.
        inner_timeout = max(1, int(self.config.timeout) - 5)
        from quota_sentinel.platform.paths import resolve_launcher
        return [
            *resolve_launcher("agy",explicit=self.config.agy_bin),
            "--agent", AGENT_NAME,
            "--model", AGY_MODEL,
            "--effort", AGY_EFFORT,
            "--print-timeout", "%ds" % inner_timeout,
            "-p", USER_PROMPT,
            "--output-format", "json",
        ]

    # ------------------------------------------------------------ agent guard
    def _guard_deadlines(self) -> Tuple[float, float, float]:
        """``(wrapper_deadline, wrapper_grace, parent_deadline)`` for the guard.

        The wrapper runs the CLI in its OWN session, so the wrapper's own
        timeout is what normally collects the CLI: SIGTERM to that group, then
        SIGKILL once its grace is spent. For that the wrapper has to outlive its
        deadline, so the parent — which can only kill the wrapper — must wait
        past ``wrapper_deadline + wrapper_grace`` before it takes over. The
        grace is clamped to half the timeout so a large ``kill_grace`` cannot
        push the guard far past the timeout it promises, and the parent's
        deadline is that total plus one reap margin, which is what makes it a
        last resort instead of a race the wrapper loses.

        End to end this lands where the old, single deadline landed — the guard
        still returns at about ``timeout`` — but on the normal path it is the
        wrapper, not the parent, that ends the CLI, and therefore the wrapper
        that reaps the process group it created.
        """
        total = max(_GUARD_MIN_TOTAL_SECONDS, float(self.config.timeout))
        grace = max(
            _GUARD_MIN_GRACE_SECONDS,
            min(max(0.0, float(self.config.kill_grace)), total / 2.0),
        )
        deadline = max(_GUARD_MIN_DEADLINE_SECONDS, total - grace)
        return deadline, grace, deadline + grace + _GUARD_REAP_MARGIN_SECONDS

    def _guard_command(self, inner: List[str], *, log_path=None) -> List[str]:
        """The guard's wrapper command, carrying the SHORTER inner deadline."""
        deadline, grace, _parent = self._guard_deadlines()
        return self._helper_command(inner, timeout=deadline, kill_grace=grace, log_path=log_path)

    def _guard_timeout(self) -> float:
        """The parent's last-resort deadline for the guard."""
        return self._guard_deadlines()[2]

    def agent_listed(self, paths: PreparedPaths) -> Optional[bool]:
        """Whether ``--agent`` will resolve, at zero token cost.

        ``/agents`` is answered by the CLI itself (measured: 0 input, 0 output,
        ``num_turns`` 0), so this is free. It returns ``None`` when the CLI
        cannot answer at all — an unreachable backend is not evidence that the
        agent is missing, and treating it as such would turn a network blip
        into a permanent fallback.

        The guard is bounded twice and the order of the two is the point: the
        wrapper's deadline is strictly shorter than the parent's (see
        ``_guard_deadlines``), so the wrapper — which owns the CLI's process
        group — is the one that kills and reaps the CLI on a timeout. The
        parent's deadline is a genuine last resort: it waits past the wrapper's
        whole deadline plus its kill grace, and if it fires anyway it reaps the
        wrapper's entire tree rather than killing the wrapper and orphaning the
        grandchild it can no longer reach.
        """
        from quota_sentinel.platform.paths import resolve_launcher
        from quota_sentinel.platform.process import run_bounded
        try:
            inner=[*resolve_launcher('agy',explicit=self.config.agy_bin),'-p','/agents','--output-format','json']
            from quota_sentinel.platform.background_auth import AgyAuthGuard
            with AgyAuthGuard() as guard:
                result=run_bounded(self._guard_command(inner, log_path=guard.log),cwd=self._cwd(paths),environment=self._environment(),
                    timeout=self._guard_timeout(),kill_grace=0)
        except (OSError,ValueError):return None
        if result.returncode:return None
        raw=result.stdout.decode('utf-8','replace')
        try:
            document = json.loads(raw.strip() or "null")
        except ValueError:
            return None
        if not isinstance(document, dict):
            return None
        command = document.get("command")
        if not isinstance(command, dict):
            return None
        data = command.get("data")
        if not isinstance(data, dict):
            return None
        agents = data.get("agents")
        if not isinstance(agents, list):
            return None
        return AGENT_NAME in [name for name in agents if isinstance(name, str)]

    def _one_turn(self, paths: PreparedPaths) -> _Attempt:
        exit_code, elapsed, stderr_offset = self._run_cli(self._command(), paths)
        raw = paths.stdout_path.read_bytes().decode("utf-8", "replace")
        status, response, usage, problems = parse_result(raw)
        stderr_this_turn = _stderr_tail(paths.stderr_path, stderr_offset)
        numbers = _usage_numbers(usage)
        # Two fences, both required. The text is this turn's stderr only, so an
        # old marker cannot make a fresh failure look free; and the turn must
        # have spent nothing, because the handshake fails BEFORE the turn starts
        # and therefore reports no usage. A failure that reported tokens is a
        # real turn and must not be replayed for free.
        spent = numbers["input_tokens"] != 0 or numbers["output_total"] != 0
        return _Attempt(
            status=status,
            response=response,
            usage=numbers,
            problems=problems,
            exit_code=exit_code,
            timed_out=exit_code == 124,
            elapsed=elapsed,
            transient=(status != "SUCCESS" and not spent and is_transient_error(
                document_error(raw), stderr_this_turn,
            )),
        )

    def _run_agy(self, paths: PreparedPaths) -> _Attempt:
        attempt = self._one_turn(paths)
        retries = max(0, self.config.transient_retries)
        for retry in range(1, retries + 1):
            if not attempt.transient:
                break
            # Handshake-only: nothing was generated, nothing was spent.
            self.logger(
                "model %s transport=agy transient handshake failure; retry %s/%s"
                % (AGY_PROVIDER, retry, retries)
            )
            attempt = self._one_turn(paths)
        return attempt

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
            from quota_sentinel.platform.files import publish_private
            record=dict(version=version,fingerprint=profile_fingerprint(),usage=dict(usage),checked_at=int(time.time()))
            publish_private(path,(json.dumps(record,ensure_ascii=False)+'\n').encode())
        except OSError:
            # Diagnostics, not correctness: a read-only state directory must
            # not fail an attempt that already succeeded.
            pass

    # ------------------------------------------------------------------ policy
    def _functional_problems(self, attempt: _Attempt) -> List[str]:
        problems = list(attempt.problems)
        if attempt.timed_out:
            problems.append("timeout")
        elif attempt.exit_code != 0:
            problems.append("exit=%s" % attempt.exit_code)
        if attempt.status != "SUCCESS":
            problems.append("status=%s" % attempt.status)
        elif attempt.usage.get("input_tokens", 0) < 1:
            # A SUCCESS turn that cannot show what it spent is not a verified
            # ignition, so it is functional, not a success. `parse_result` does
            # not make a missing `usage` a parse problem on its own — a
            # non-SUCCESS turn has no usage to report and keeps its own
            # handling — but the policy here needs the number: `input_tokens`
            # is the only structural proof that the 564-token minimal profile
            # and not the 22,311-token stock agent answered, and the input
            # ceiling cannot be evaluated without it.
            problems.append("usage=unverified")
        if attempt.response is None:
            problems.append("no response")
        elif attempt.response.strip() != USER_PROMPT:
            problems.append("reply=%r" % attempt.response.strip()[:40])
        return problems

    def _cost_regressions(self, attempt: _Attempt) -> List[str]:
        problems = []
        if attempt.usage.get("input_tokens", 0) >= self.config.input_ceiling:
            problems.append(
                "input=%s>=%s" % (attempt.usage["input_tokens"], self.config.input_ceiling)
            )
        if attempt.usage.get("output_total", 0) >= self.config.output_ceiling:
            problems.append(
                "output=%s>=%s" % (attempt.usage["output_total"], self.config.output_ceiling)
            )
        return problems

    def _log_attempt(
        self, phase: str, attempt: int, limit: int, result: _Attempt, outcome: str,
    ) -> None:
        numbers = result.usage
        self.logger(
            "model %s transport=agy phase=%s attempt=%s/%s result=%s "
            "input=%s cached=%s output=%s thinking=%s elapsed=%ss"
            % (AGY_PROVIDER, phase, attempt, limit, outcome,
               numbers.get("input_tokens"), numbers.get("cache_read_tokens"),
               numbers.get("output_tokens"), numbers.get("thinking_tokens"),
               int(result.elapsed))
        )

    def _fallback_to_pi(
        self, workspace: Path, phase: str, attempt: int, limit: int, reason: str,
    ) -> Optional[AttemptResult]:
        if self.fallback is None:
            return None
        self.logger(
            "model %s transport=agy -> fallback=pi reason=%s" % (AGY_PROVIDER, reason)
        )
        self.fallback.prepare(AGY_PROVIDER, workspace)
        return self.fallback.run(AGY_PROVIDER, workspace, phase, attempt, limit)

    def _failed(
        self, paths: PreparedPaths, result: Optional[_Attempt], reason: str,
    ) -> AttemptResult:
        return AttemptResult(
            success=False,
            exit_code=0 if result is None else result.exit_code,
            timed_out=False if result is None else result.timed_out,
            elapsed=0.0 if result is None else result.elapsed,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            quota_path=paths.quota_path,
            error_summary=reason,
        )

    def run(self, provider: str, workspace: Path, phase: str, attempt: int, limit: int) -> AttemptResult:
        if provider != AGY_PROVIDER:
            raise ValueError(f"agy transport does not serve {provider!r}")
        workspace = Path(os.path.abspath(workspace))
        paths = self._prepared.get((provider, workspace)) or self.prepare(provider, workspace)

        # Free guard, before any token: an agent the CLI cannot resolve would be
        # answered by the default agent instead (22,311 tokens against 564).
        if self.config.preflight:
            listed = self.agent_listed(paths)
            # Logged on SUCCESS too, not only on refusal: an operator reading the
            # run log has to be able to tell "the guard ran and passed" from "the
            # guard never ran", and the refusal path is the only one that used to
            # say anything at all.
            self.logger(
                "model %s transport=agy preflight agent=%s listed=%s"
                % (AGY_PROVIDER, AGENT_NAME,
                   {True: "yes", False: "no", None: "unknown"}[listed])
            )
            if listed is False:
                reason = "functional:agent-missing"
                self.logger(
                    "model %s transport=agy agent %s is not discoverable in %s; "
                    "refusing before the turn (0 tokens spent)"
                    % (AGY_PROVIDER, AGENT_NAME, self._cwd(paths))
                )
                fallback = self._fallback_to_pi(workspace, phase, attempt, limit, reason)
                return fallback if fallback is not None else self._failed(paths, None, reason)
        else:
            self.logger(
                "model %s transport=agy preflight agent=%s listed=skipped"
                % (AGY_PROVIDER, AGENT_NAME)
            )

        version = self.agy_version()
        previous = self._read_smoke()
        changed = (
            previous.get("version") != version
            or previous.get("fingerprint") != profile_fingerprint()
        )
        if changed:
            self.logger(
                "model %s transport=agy profile changed (version %s -> %s, fingerprint %s -> %s); "
                "this attempt is the smoke test"
                % (AGY_PROVIDER, previous.get("version"), version,
                   previous.get("fingerprint"), profile_fingerprint())
            )

        result = self._run_agy(paths)
        functional = self._functional_problems(result)
        regression = self._cost_regressions(result)

        if not functional and not regression:
            self._write_smoke(version, result.usage)
            self._log_attempt(phase, attempt, limit, result, "success")
            # One greppable line per ignition: what it cost, which profile
            # produced it, and what that is worth against the stock agent. The
            # per-attempt line above is the retry/debug record; this is the
            # "did the window get anchored cheaply" record an operator greps for.
            self.logger(
                "model %s transport=agy ignition input=%s output=%s thinking=%s "
                "total=%s baseline=%s saved=%.1f%% profile=%s version=%s"
                % (AGY_PROVIDER, result.usage.get("input_tokens"),
                   result.usage.get("output_tokens"), result.usage.get("thinking_tokens"),
                   result.usage.get("total_tokens"), AGY_STOCK_INPUT_TOKENS,
                   _saved_percent(result.usage.get("input_tokens", 0)),
                   profile_fingerprint(), version or "unknown")
            )
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
        if functional:
            self._log_attempt(phase, attempt, limit, result, "failed")
            fallback = self._fallback_to_pi(workspace, phase, attempt, limit, reason)
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

        # Cost regression with no functional problem: ACCEPTED as a success, and
        # deliberately NOT handed to Pi. The reply was already delivered and the
        # window already anchored, so the fact this attempt exists to produce is
        # already true; re-delivering it through Pi would spend more quota to
        # reach the same fact, and if Pi then failed the scheduler would retry
        # and spend a third time. Measured, not guessed: the run worked, so this
        # is information about the profile, not about the network. The smoke
        # record stays untouched because a regressed profile is not a verified
        # one — the next attempt re-tests instead of trusting it.
        self._log_attempt(phase, attempt, limit, result, "cost-regression")
        self.logger(
            "model %s transport=agy cost regression (%s); expected input<%s output<%s "
            "— the delivery is accepted and the profile needs attention"
            % (AGY_PROVIDER, ",".join(regression),
               self.config.input_ceiling, self.config.output_ceiling)
        )
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


_STDERR_TAIL_BYTES = 4096


def _file_size(path: Path) -> int:
    """The current size of ``path``, or 0 when it does not exist yet."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _stderr_tail(path: Path, offset: int = 0) -> str:
    """The last ``_STDERR_TAIL_BYTES`` of ``path`` at or after ``offset``.

    ``offset`` is where the current turn started writing, so the result never
    reaches back into an earlier turn's logging; the byte cap is applied on top
    of that slice, exactly as before.
    """
    try:
        size = path.stat().st_size
        if size <= offset:
            return ""
        with path.open("rb") as handle:
            handle.seek(max(offset, size - _STDERR_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ---------------------------------------------------- last-resort guard reaping
def _tree_process_groups(root_pid: int) -> List[int]:
    """Every process group in ``root_pid``'s descendant tree, plus its own.

    The same technique the orchestrator's ``SubprocessRunner`` uses for the
    outer ``check`` bound, and for the same reason: the wrapper starts the CLI
    in its OWN session (that is the helper's contract), so a kill aimed at the
    wrapper's group cannot reach the CLI at all. A ``ps`` snapshot taken while
    the wrapper is still alive is the only thing that links the two — once the
    wrapper dies the CLI is reparented and that link is gone, which is exactly
    how a killed wrapper orphans a grandchild. Our own group is never returned:
    the caller signals these with SIGKILL, and this process must not be in the
    set.
    """
    from quota_sentinel.platform.posix_process import _children
    groups={root_pid};pending=[root_pid];seen=set();deadline=time.monotonic()+2
    while pending and len(seen)<4096 and time.monotonic()<deadline:
        pid=pending.pop()
        if pid in seen:continue
        seen.add(pid);pending.extend(_children(pid))
        try:
            group=os.getpgid(pid)
            if group>0 and group!=os.getpgrp():groups.add(group)
        except ProcessLookupError:pass
    return list(groups)


def _reap_guard_tree(process: subprocess.Popen) -> None:
    """SIGKILL the guard's whole tree, then reap the wrapper.

    This is the parent's last resort, so it does not ask politely first: the
    wrapper has already had its own deadline plus the grace it was told to give
    its child, and anything still alive after that is by definition ignoring the
    signals the wrapper sent. The descendant groups are collected BEFORE the
    wrapper is killed, because that snapshot is the only handle on a CLI that
    the wrapper started in a session of its own; killing the wrapper first would
    orphan it. Non-fatal by construction: every step tolerates a process that
    has already exited, and the caller only ever returns ``None`` from here.
    """
    for pgid in _tree_process_groups(process.pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    # The wrapper by PID as well. It leads the group above because
    # ``agent_listed`` starts it with ``start_new_session=True``, but the reap
    # must not silently depend on the caller having done that.
    try:
        os.kill(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.communicate(timeout=_GUARD_REAP_MARGIN_SECONDS)
    except subprocess.TimeoutExpired:
        # A descendant SIGKILL has not yet collected can still hold the pipe
        # write end open; the wrapper itself must not stay unreaped for it.
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_GUARD_REAP_MARGIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, subprocess.SubprocessError):
        pass


def document_error(raw: str) -> Optional[str]:
    """The ``error`` field of a failed ``--output-format json`` run, if any."""
    try:
        document = json.loads(raw.strip() or "null")
    except ValueError:
        return None
    if isinstance(document, dict) and isinstance(document.get("error"), str):
        return document["error"]
    return None
