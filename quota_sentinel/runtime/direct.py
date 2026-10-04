"""Direct vendor-API task attempts: one tiny chat completion per try.

This is the second transport in the port. ``ModelRunner`` drives the Pi agent
for providers whose subscription is not a plain HTTP endpoint (Codex OAuth,
Antigravity's local ``agy`` service); this module talks to the vendor's
OpenAI-compatible ``chat/completions`` directly for providers whose whole
credential is an API key (OpenCode Go, ClinePass).

Both transports publish the same contract -- ``prepare(provider, workspace)``
and ``run(provider, workspace, phase, attempt, limit) -> AttemptResult`` -- so
the coordinator, the retry debt, the locks, the cards and the run log are
unaware of which one ran.

Credential and payload discipline, inherited from ``opencode_usage.py``:

* the API key reaches curl through its own stdin config, never argv, never the
  environment, never a log line;
* the request body is not secret, but it still travels through a 0600 temp
  file so no prompt text lands in a process argument list;
* every child runs in its own process group under a deadline, so a hung vendor
  cannot hold ``quota.lock`` open;
* the persisted stdout is the model's own reply, not the vendor envelope: the
  ClinePass gateway attaches routing metadata to its responses, and none of it
  belongs in the operator's workspace.

``reasoning_effort: "none"`` is not cosmetic. Measured against both vendors on
2026-09-23, the same prompt costs 16 total tokens with it and 86 without
(44 of them reasoning tokens): turning thinking off is a 5x-to-12x difference
on the small requests this scheduler spends every five hours.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from quota_sentinel.runtime import keychain
from quota_sentinel.runtime.models import AttemptResult, PreparedPaths, _redact_secrets

__all__ = [
    "DIRECT_PROVIDERS",
    "DirectProvider",
    "DirectRunner",
    "DIRECT_TIMEOUT_SECONDS",
]

# The prompt and system text are byte-identical to the Pi transport's, so a
# transport A/B compares transports rather than wording.
SYSTEM_PROMPT = "忽略上下文"
USER_PROMPT = "不用思考，只回复我 1"
USER_AGENT = "quota-sentinel/1.0"
# Enough headroom that a model which ignores the reasoning switch still emits
# its one-character answer instead of being truncated into a failed attempt.
MAX_TOKENS = 256
DIRECT_TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 5.0
_MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DirectProvider:
    """Everything vendor-specific about one direct provider, as data."""

    provider: str
    endpoint: str
    model: str
    quota_url: str
    key_service: str
    # Environment variable that may hold the same key, checked before the
    # Keychain so a one-off run or a suite can override the stored credential.
    env_key: str = ""
    # ClinePass wraps the OpenAI body: {"success": bool, "data": {...}}.
    envelope: bool = False
    # OpenCode Go refuses a request without a stable session id.
    session_header: bool = False
    # ClinePass reads its caller's name from the documented X-Title header.
    title_header: bool = False
    # Which usage payload the runner knows how to turn into a snapshot.
    quota_kind: str = "opencode"


DIRECT_PROVIDERS: Dict[str, DirectProvider] = {
    "opencode": DirectProvider(
        provider="opencode",
        endpoint="https://opencode.ai/zen/go/v1/chat/completions",
        model="deepseek-v4.1-flash",
        quota_url="https://opencode.ai/zen/go/v1/usage",
        key_service="quota-sentinel.opencode-go-api-key",
        env_key="OPENCODE_API_KEY",
        session_header=True,
        quota_kind="opencode",
    ),
    "clinepass": DirectProvider(
        provider="clinepass",
        endpoint="https://api.cline.bot/api/v1/chat/completions",
        model="cline-pass/deepseek-v4.1-flash",
        quota_url="https://api.cline.bot/api/v1/users/me/plan/usage-limits",
        key_service="quota-sentinel.clinepass-api-key",
        env_key="CLINE_API_KEY",
        envelope=True,
        title_header=True,
        quota_kind="clinepass",
    ),
}

# A constant per provider, not a per-attempt random value: OpenCode Go asks for
# "a stable session ID in x-opencode-session for each conversation so we can
# optimize routing and prompt caching", and this sentinel has exactly one
# conversation.
SESSION_ID = "quota-sentinel-%s-main"

_ISO_Z = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$"
)


def _epoch_utc(value: object) -> Optional[int]:
    """ISO-8601 UTC (milli- or nanosecond fraction) -> epoch seconds.

    ``datetime.fromisoformat`` on the 3.9 interpreter the deployment targets
    rejects the nanosecond stamps the ClinePass gateway emits, so the whole
    seconds are parsed first and the fraction is discarded deliberately.
    """
    if not isinstance(value, str) or not value:
        return None
    match = _ISO_Z.match(value)
    if match is None:
        return None
    try:
        parts = tuple(int(part) for part in match.groups()[:6])
        return calendar.timegm(parts + (0, 0, 0))
    except (ValueError, OverflowError):
        return None


def _remaining(used: object) -> Optional[int]:
    if type(used) not in (int, float):
        return None
    if used != used or used in (float("inf"), float("-inf")):
        return None
    if not 0 <= used <= 100:
        return None
    return max(0, min(100, int(round(100 - used))))


def _window(percent_used: object, resets_at: object) -> Optional[dict]:
    remaining = _remaining(percent_used)
    reset = _epoch_utc(resets_at)
    if remaining is None or reset is None:
        return None
    return {"remainingPercent": remaining, "resetAt": reset}


def _snapshot_opencode(payload: Mapping[str, object], captured_at: int) -> Optional[dict]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    document: Dict[str, object] = {"capturedAt": captured_at}
    for api_name, normalized in (("rolling", "fiveHour"), ("weekly", "weekly"),
                                 ("monthly", "monthly")):
        raw = usage.get(api_name)
        if not isinstance(raw, Mapping):
            continue
        window = _window(raw.get("percent"), raw.get("resetsAt"))
        if window is not None:
            document[normalized] = window
    if "fiveHour" not in document or "weekly" not in document:
        return None
    return document


def _snapshot_clinepass(payload: Mapping[str, object], captured_at: int) -> Optional[dict]:
    if payload.get("success") is not True:
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    limits = data.get("limits")
    if not isinstance(limits, list):
        return None
    names = {"five_hour": "fiveHour", "weekly": "weekly", "monthly": "monthly"}
    document: Dict[str, object] = {"capturedAt": captured_at}
    for limit in limits:
        if not isinstance(limit, Mapping):
            continue
        normalized = names.get(limit.get("type"))
        if normalized is None:
            continue
        window = _window(limit.get("percentUsed"), limit.get("resetsAt"))
        if window is not None:
            document[normalized] = window
    if "fiveHour" not in document or "weekly" not in document:
        return None
    return document


_SNAPSHOTS = {
    "opencode": _snapshot_opencode,
    "clinepass": _snapshot_clinepass,
}


@dataclass(frozen=True)
class _CommandResult:
    stdout: str
    status: str
    returncode: int
    timed_out: bool
    error: str = ""


def _kill_group(process: subprocess.Popen, sig: int) -> None:
    try:
        if process.pid != os.getpgrp():
            os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _open_private(path: Path, *, append: bool = False):
    from quota_sentinel.platform.files import private_open
    return private_open(path, 'ab' if append else 'wb')



class DirectRunner:
    """Attempt a provider's model by calling its own API, not the Pi agent."""

    def __init__(
        self,
        *,
        curl_bin: Path = Path("/usr/bin/curl"),
        timeout: float = DIRECT_TIMEOUT_SECONDS,
        logger: Optional[Callable[[str], None]] = None,
        providers: Mapping[str, DirectProvider] = DIRECT_PROVIDERS,
        capture_providers=None,
        key_reader: Optional[Callable[[str], str]] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.capture_providers = capture_providers
        self.curl_bin = Path(curl_bin)
        self.timeout = timeout
        self.logger = logger or (lambda _message: None)
        self.providers = dict(providers)
        self.environment = environment
        self._key_reader = key_reader or self._default_key
        self._prepared: dict[tuple[str, Path], PreparedPaths] = {}

    def _default_key(self, provider: str) -> str:
        """The vendor env var first (a one-off override), then the Keychain."""
        env = os.environ if self.environment is None else self.environment
        spec = self.providers[provider]
        if spec.env_key and env.get(spec.env_key):
            return env[spec.env_key]
        return keychain.read(spec.key_service, environment=self.environment)

    # -- contract shared with ModelRunner ---------------------------------
    def prepare(self, provider: str, workspace: Path) -> PreparedPaths:
        if provider not in self.providers:
            raise ValueError(f"unknown direct provider: {provider}")
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
        self._prepared[(provider, workspace)] = paths
        return paths

    def run(
        self, provider: str, workspace: Path, phase: str, attempt: int, limit: int
    ) -> AttemptResult:
        if provider not in self.providers:
            raise ValueError(f"unknown direct provider: {provider}")
        workspace = Path(os.path.abspath(workspace))
        paths = self._prepared.get((provider, workspace)) or self.prepare(
            provider, workspace
        )
        spec = self.providers[provider]
        started = time.monotonic()

        key = self._key_reader(provider) or ""
        if not key:
            result = self._finish(
                spec, paths, started, False, 3, False,
                "credential missing (Keychain %s)" % spec.key_service,
            )
            self._log_attempt(provider, phase, attempt, limit, result)
            return result

        completion = self._post_json(
            spec.endpoint, key, self._body(spec), spec
        )
        content, usage, detail = self._read_completion(spec, completion)
        success = completion.status == "200" and completion.returncode == 0 and content == "1"

        if success and (self.capture_providers is None or provider in self.capture_providers):
            self._write_snapshot(spec, key, paths.quota_path)

        with _open_private(paths.stdout_path) as handle:
            handle.write((content + "\n").encode("utf-8", "replace"))
        summary = "" if success else self._failure_summary(spec, completion, content, detail)
        result = self._finish(
            spec, paths, started, success,
            0 if success else (124 if completion.timed_out else 1),
            completion.timed_out, summary,
        )
        if success and usage is not None:
            self.logger(
                "model %s attempt=%s usage %s" % (provider, attempt, usage)
            )
        self._log_attempt(provider, phase, attempt, limit, result)
        return result

    # -- internals --------------------------------------------------------
    def _body(self, spec: DirectProvider) -> dict:
        return {
            "model": spec.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "reasoning_effort": "none",
        }

    def _config(self, spec: DirectProvider, key: str) -> str:
        config = (
            'header = "Authorization: Bearer %s"\n'
            'header = "Content-Type: application/json"\n' % key
        )
        if spec.title_header:
            # Cline's docs: X-Title labels the caller in their usage logs. The
            # native probes send the same value, so one name covers every
            # request this deployment makes.
            config += 'header = "X-Title: %s"\n' % USER_AGENT.split("/")[0]
        if spec.session_header:
            config += 'header = "x-opencode-session: %s"\n' % (SESSION_ID % spec.provider)
        return config

    def _run_bounded(
        self, command: list, stdin_text: str, timeout: float
    ) -> Tuple[str, int, bool, str]:
        from quota_sentinel.platform.paths import resolve_launcher
        from quota_sentinel.platform.process import run_bounded
        env=dict(os.environ)
        if self.environment is not None:env.update(self.environment)
        for name in ('OPENCODE_API_KEY','CLINE_API_KEY','FEISHU_APP_SECRET'):env.pop(name,None)
        try:
            prefix=resolve_launcher('curl',explicit=command[0])
            with tempfile.TemporaryDirectory(prefix='quota-direct.') as cwd:
                result=run_bounded((*prefix,*command[1:]),cwd=cwd,environment=env,
                    input_data=stdin_text.encode(),timeout=timeout,kill_grace=2,max_bytes=_MAX_RESPONSE_BYTES)
        except (OSError,ValueError):return '',127,False,'curl could not start'
        if result.returncode==125:return result.stdout.decode('utf-8','replace'),1,False,'response too large'
        return result.stdout.decode('utf-8','replace'),result.returncode,result.timed_out,result.stderr.decode('utf-8','replace').strip()[:300]

    def _curl(self, url: str, key: str, body: Optional[dict], spec: DirectProvider) -> _CommandResult:
        with tempfile.TemporaryDirectory(prefix="quota-sentinel-direct.") as work:
            os.chmod(work, 0o700)
            command = [
                str(self.curl_bin), "--config", "-", "--silent", "--show-error",
                "--user-agent", USER_AGENT,
                "--connect-timeout", str(CONNECT_TIMEOUT_SECONDS),
                "--max-time", str(self.timeout),
                "--write-out", "\nQSHTTPSTATUS:%{http_code}",
            ]
            if body is not None:
                body_path = Path(work) / "body.json"
                with _open_private(body_path) as handle:
                    handle.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))
                command += ["-X", "POST", "--data-binary", "@" + str(body_path)]
            command.append(url)
            output, rc, timed_out, error = self._run_bounded(
                command, self._config(spec, key), self.timeout + 5
            )
        marker = output.rfind("QSHTTPSTATUS:")
        if marker < 0:
            return _CommandResult(output, "", rc, timed_out, error)
        return _CommandResult(output[:marker], output[marker + len("QSHTTPSTATUS:"):].strip(),
                              rc, timed_out, error)

    def _post_json(self, url: str, key: str, body: dict, spec: DirectProvider) -> _CommandResult:
        return self._curl(url, key, body, spec)

    def _read_completion(
        self, spec: DirectProvider, result: _CommandResult
    ) -> Tuple[str, Optional[str], str]:
        """Return (content, usage-summary, detail) from one completion body."""
        if result.error:
            return "", None, result.error
        try:
            document = json.loads(result.stdout)
        except ValueError:
            return "", None, "response was not JSON"
        if not isinstance(document, Mapping):
            return "", None, "response was not an object"
        if spec.envelope:
            if document.get("success") is not True:
                return "", None, "envelope reported success=false"
            document = document.get("data")
            if not isinstance(document, Mapping):
                return "", None, "envelope had no data object"
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            return "", None, "no choices in response"
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip("\n"), _usage_summary(document.get("usage")), "ok"
        return "", None, "no text content in response"

    def _write_snapshot(self, spec: DirectProvider, key: str, path: Path) -> None:
        """Persist the vendor's own usage document in the Pi-capture shape.

        The snapshot is the same artifact the retired capture extension wrote on
        ``agent_end``, so the normalizers, the cache and the card stay unaware
        that the transport changed. A failure here never fails the attempt: the
        model's answer is what the scheduler owes, the snapshot is a fallback.
        """
        snapshot = _SNAPSHOTS.get(spec.quota_kind)
        if snapshot is None:
            return
        result = self._curl(spec.quota_url, key, None, spec)
        if result.status != "200" or result.returncode != 0:
            self.logger(
                "quota %s: direct snapshot http=%s" % (spec.provider, result.status or "?")
            )
            return
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return
        document = snapshot(payload, int(time.time())) if isinstance(payload, Mapping) else None
        if document is None:
            self.logger("quota %s: direct snapshot unusable" % spec.provider)
            return
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        with _open_private(path) as handle:
            handle.write(encoded + b"\n")

    def _finish(
        self, spec: DirectProvider, paths: PreparedPaths, started: float,
        success: bool, exit_code: int, timed_out: bool, summary: str,
    ) -> AttemptResult:
        elapsed = time.monotonic() - started
        if summary:
            with _open_private(paths.stderr_path, append=True) as handle:
                handle.write((_redact_secrets(summary)[:300] + "\n").encode("utf-8"))
        return AttemptResult(
            success=success,
            exit_code=exit_code,
            timed_out=timed_out,
            elapsed=elapsed,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            quota_path=paths.quota_path,
            error_summary="" if success else (summary or "(no diagnostics)"),
        )

    def _failure_summary(
        self, spec: DirectProvider, result: _CommandResult, content: str, detail: str
    ) -> str:
        if result.timed_out:
            return "direct %s timeout after %ss" % (spec.provider, int(self.timeout))
        if result.status and result.status != "200":
            return "direct %s http=%s %s" % (spec.provider, result.status, detail)
        if content and content != "1":
            return "direct %s reply was not 1" % spec.provider
        return "direct %s failed: %s" % (spec.provider, detail)

    def _log_attempt(
        self, provider: str, phase: str, attempt: int, limit: int, result: AttemptResult
    ) -> None:
        """Same wording as the Pi transport, so one log covers both."""
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


def _usage_summary(usage: object) -> Optional[str]:
    """One sanitized line: token counts and, when the vendor reports it, cost."""
    if not isinstance(usage, Mapping):
        return None
    fields = []
    for key, label in (("prompt_tokens", "prompt"), ("completion_tokens", "completion"),
                       ("total_tokens", "total")):
        value = usage.get(key)
        if type(value) is int:
            fields.append("%s=%d" % (label, value))
    details = usage.get("completion_tokens_details")
    if isinstance(details, Mapping) and type(details.get("reasoning_tokens")) is int:
        fields.append("reasoning=%d" % details["reasoning_tokens"])
    cost = usage.get("cost")
    if type(cost) in (int, float):
        fields.append("cost=%.8f" % float(cost))
    return " ".join(fields) if fields else None
