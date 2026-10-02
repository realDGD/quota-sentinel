"""Acquire quota readings through the provider's ordered fallback tiers.

The existing quota package owns tier policy and payload normalization. This
module owns only external probes, persisted fallback files, and selection of
the first usable reading. A collection has no memory of earlier collections.
"""
from __future__ import annotations

from dataclasses import dataclass
import contextlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from quota_sentinel.runtime import keychain
from quota_sentinel.quota import (
    PROVIDERS,
    FRESH_TIERS,
    Tier,
    ProviderQuota,
    QuotaNormalizationError,
    QuotaWindow,
    demote_to_cached,
    normalize_codexbar_antigravity,
    normalize_codexbar_clinepass,
    normalize_codexbar_codex,
    normalize_codexbar_opencode,
    normalize_pi_antigravity,
    normalize_pi_clinepass,
    normalize_pi_codex,
    normalize_pi_opencode,
    parse_document,
    renormalize_document,
    tier_plan,
    write_document,
)


_NORMALIZE_BAR = {
    "codex": normalize_codexbar_codex,
    "antigravity": normalize_codexbar_antigravity,
    "opencode": normalize_codexbar_opencode,
    "clinepass": normalize_codexbar_clinepass,
}
_NORMALIZE_PI = {
    "codex": normalize_pi_codex,
    "antigravity": normalize_pi_antigravity,
    "opencode": normalize_pi_opencode,
    "clinepass": normalize_pi_clinepass,
}
from quota_sentinel.helpers import resource_path
_ROOT = resource_path("opencode_usage.py").parent
_MAX_PROBE_BYTES = 1024 * 1024
# The shell has no outer bound on the native helper: only the helper's own
# --timeout applies, and uv + interpreter start-up happens BEFORE that clock
# starts. A tight outer bound would SIGTERM probes the shell accepts and
# silently fall back to CodexBar, so this is deliberately generous.
_NATIVE_HELPER_SLACK_SECONDS = 5
_TIERS = (Tier.NATIVE, Tier.CODEXBAR_LIVE, Tier.CODEXBAR_CACHE, Tier.PI_SNAPSHOT, Tier.PI_LIVE)
_OPENCODE_API_KEY_SERVICE = "quota-sentinel.opencode-go-api-key"
_CLINEPASS_API_KEY_SERVICE = "quota-sentinel.clinepass-api-key"


@dataclass(frozen=True)
class QuotaReading:
    quota: Optional[ProviderQuota]
    tier: Optional[Tier]
    fresh: bool
    error: Optional[str] = None

    @property
    def document(self) -> Optional[dict]:
        """The canonical JSON shape for card and scheduler callers."""
        return None if self.quota is None else self.quota.as_document()


@dataclass(frozen=True)
class _CommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool = False


def _kill_group(process: subprocess.Popen, sig: int) -> None:
    try:
        if process.pid != os.getpgrp():
            os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _run_bounded(
    command: Sequence[str], timeout: float, grace: float = 1.0,
    stdin: Optional[bytes] = None,
    *, environment=None, cwd=None,
) -> _CommandResult:
    """Capture an owned tree with finite, portable pipe limits."""
    from quota_sentinel.platform.process import run_bounded
    try:
        result=run_bounded(command,cwd=Path.cwd() if cwd is None else cwd,
            environment=os.environ if environment is None else environment,
            input_data=stdin,timeout=timeout,kill_grace=grace,max_bytes=_MAX_PROBE_BYTES)
    except (OSError,ValueError):return _CommandResult(b"",b"",127)
    return _CommandResult(result.stdout,result.stderr,result.returncode,result.timed_out)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _bytes_json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None


class QuotaCollector:
    """Collect independent readings for the fixed provider roster.

    All path and credential boundaries can be injected. The default paths
    match the shell deployment, while tests can supply fake binaries.
    """

    def __init__(
        self,
        state_dir: Path,
        workspace: Path,
        *,
        codex_bin: Path = Path("/opt/homebrew/bin/codex"),
        agy_bin: Path = Path("/opt/homebrew/bin/agy"),
        uv_bin: Path = Path("/opt/homebrew/bin/uv"),
        codexbar_bin: Path = Path("/opt/homebrew/bin/codexbar"),
        opencode_usage_helper: Path = _ROOT / "opencode_usage.py",
        antigravity_usage_helper: Path = _ROOT / "antigravity_usage.py",
        clinepass_usage_helper: Path = _ROOT / "clinepass_usage.py",
        curl_bin: Path = Path("/usr/bin/curl"),
        security_bin: Path = Path("/usr/bin/security"),
        python_bin: Path = Path(sys.executable),
        opencode_api_key_getter: Optional[Callable[[], str]] = None,
        api_key_getter=None,
        environment=None,
        logger: Optional[Callable[[str], None]] = None,
        codexbar_timeout: float = 20,
        antigravity_codexbar_timeout: float = 35,
        opencode_codexbar_timeout: float = 20,
        clinepass_codexbar_timeout: float = 20,
        antigravity_native_timeout: float = 20,
        opencode_native_timeout: float = 15,
        clinepass_native_timeout: float = 15,
        codexbar_kill_grace: float = 10,
        providers: Sequence[str] = PROVIDERS,
        tier_chains=None,
        pi_live_client=None,
    ) -> None:
        self.environment=dict(os.environ if environment is None else environment)
        if self.environment.get("QUOTA_SENTINEL_CODEX_HOME"):
            self.environment["CODEX_HOME"]=self.environment["QUOTA_SENTINEL_CODEX_HOME"]
        self.providers = tuple(providers)
        self.pi_live_client = pi_live_client
        self.tier_chains = {p: tuple(Tier(t) for t in tier_chains[p]) for p in self.providers} if tier_chains is not None else None
        self.state_dir = Path(state_dir)
        self.workspace = Path(workspace)
        self.codex_bin = Path(codex_bin)
        self.agy_bin = Path(agy_bin)
        self.uv_bin = Path(uv_bin)
        self.codexbar_bin = Path(codexbar_bin)
        self.opencode_usage_helper = Path(opencode_usage_helper)
        self.antigravity_usage_helper = Path(antigravity_usage_helper)
        self.clinepass_usage_helper = Path(clinepass_usage_helper)
        self.curl_bin = Path(curl_bin)
        self.security_bin = Path(security_bin)
        self.python_bin = Path(python_bin)
        self.opencode_api_key_getter = opencode_api_key_getter
        self.api_key_getter = api_key_getter
        self.logger = logger or (lambda _message: None)
        self.codexbar_timeout = codexbar_timeout
        self.antigravity_codexbar_timeout = antigravity_codexbar_timeout
        self.opencode_codexbar_timeout = opencode_codexbar_timeout
        self.clinepass_codexbar_timeout = clinepass_codexbar_timeout
        self.antigravity_native_timeout = antigravity_native_timeout
        self.opencode_native_timeout = opencode_native_timeout
        self.clinepass_native_timeout = clinepass_native_timeout
        self.codexbar_kill_grace = codexbar_kill_grace

    def _cache_path(self, provider: str) -> Path:
        return self.state_dir / ("codexbar-%s-last-success.json" % provider)

    def _snapshot_path(self, provider: str) -> Path:
        return self.state_dir / ("pi-%s-quota.json" % provider)

    def _ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)

    def _write_json(self, path: Path, document: dict) -> None:
        self._ensure_state_dir()
        from quota_sentinel.platform.files import publish_private
        publish_private(path,(json.dumps(document,ensure_ascii=False)+'\n').encode())

    def _save_cache(self, provider: str, live: ProviderQuota) -> None:
        cached = demote_to_cached(live).as_document()
        cached["originalSource"] = live.source
        self._write_json(self._cache_path(provider), cached)

    def _read_cache(self, provider: str) -> Optional[ProviderQuota]:
        raw = _read_json(self._cache_path(provider))
        if not isinstance(raw, dict):
            return None
        try:
            document = renormalize_document(raw)
            # The on-disk originalSource is intentional cache metadata, not
            # part of the effective quota document's strict typed schema.
            document.pop("originalSource", None)
            document["source"] = "CodexBar · cached（可能不是最新）"
            document["fresh"] = False
            document["cached"] = True
            return parse_document(document)
        except (QuotaNormalizationError, ValueError, TypeError, OSError):
            # A persisted file is untrusted input. A zero year in capturedAt
            # leaves the timestamp parser as a PLAIN ValueError, and a
            # truncated or oddly typed file can raise TypeError: all of them
            # mean "this tier has nothing usable", never "abort the roster".
            return None

    def _read_pi(self, provider: str, pi_raw: Optional[Mapping[str, Path]]) -> Optional[ProviderQuota]:
        if pi_raw and provider in pi_raw:
            raw = _read_json(Path(pi_raw[provider]))
            if raw is not None:
                try:
                    return _NORMALIZE_PI[provider](raw)
                except QuotaNormalizationError:
                    pass
        raw = _read_json(self._snapshot_path(provider))
        if raw is None:
            return None
        try:
            document = renormalize_document(raw)
            document["source"] = "Pi 快照（可能不是最新）"
            document["fresh"] = False
            document["cached"] = True
            return parse_document(document)
        except (QuotaNormalizationError, ValueError, TypeError, OSError):
            return None

    def save_pi_snapshots(self, pi_raw: Mapping[str, Path]) -> None:
        """Persist valid captures without replacing older files on failure."""
        for provider in PROVIDERS:
            path = pi_raw.get(provider)
            if path is None:
                continue
            raw = _read_json(Path(path))
            if raw is None:
                continue
            try:
                quota = _NORMALIZE_PI[provider](raw)
                self._ensure_state_dir()
                write_document(self._snapshot_path(provider), quota)
            except (QuotaNormalizationError, OSError):
                continue

    def _api_key(self, service: str, env_key: str) -> str:
        """The provider's own env override, then its Keychain item.

        ``opencode_api_key_getter`` remains the injectable seam the OpenCode
        suites use; every other provider reads its own service.
        """
        if self.api_key_getter is not None:return self.api_key_getter(service,env_key) or ''
        if service == _OPENCODE_API_KEY_SERVICE and self.opencode_api_key_getter is not None:
            return self.opencode_api_key_getter() or ""
        value = os.environ.get(env_key, "")
        if value:
            return value
        return keychain.read(
            service, security_bin=str(self.security_bin), timeout=5,
        )

    def _native_codex(self) -> Optional[ProviderQuota]:
        from quota_sentinel.platform.paths import resolve_launcher
        from quota_sentinel.platform.process import spawn_owned, BoundedPipes
        env=dict(self.environment)
        for key in ('OPENAI_API_KEY','CODEX_API_KEY'):env.pop(key,None)
        try:
            command=(*resolve_launcher('codex',explicit=self.codex_bin),'app-server')
            process=spawn_owned(command,cwd=self.workspace if self.workspace.is_dir() else Path.cwd(),environment=env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            with BoundedPipes(process,max_bytes=_MAX_PROBE_BYTES) as pipes:
                def request(request_id,method,params):
                    deadline=time.monotonic()+5
                    data=json.dumps(dict(jsonrpc='2.0',id=request_id,method=method,params=params)).encode()+b'\n'
                    pipes.write(data,deadline=deadline)
                    while time.monotonic()<deadline:
                        response=_bytes_json(pipes.readline(deadline=deadline))
                        if isinstance(response,dict) and response.get('id')==request_id:return response
                    return None
                if request(1,'initialize',{'clientInfo':{'name':'quota-sentinel','version':'1.0'}}) is None:return None
                response=request(2,'account/rateLimits/read',{})
                if not isinstance(response,dict):return None
                limits=response.get('result',{}).get('rateLimits',{})
                primary,secondary=limits.get('primary'),limits.get('secondary')
                if not isinstance(primary,dict) or not isinstance(secondary,dict) or not primary or not secondary:return None
                five=primary if primary.get('windowDurationMins',300)<=360 else secondary
                weekly=secondary if secondary.get('windowDurationMins',10080)>360 else primary
                def window(value):
                    used=value.get('usedPercent');reset=value.get('resetsAt')
                    if type(used) not in (int,float) or not math.isfinite(used) or not 0<=used<=100 or type(reset) is not int or reset<=0:raise ValueError('invalid native window')
                    return QuotaWindow(math.floor(max(0,min(100,100-used))+.5),reset)
                quota=ProviderQuota('Native · codex app-server',True,False,int(time.time()),window(five),window(weekly))
                return parse_document(quota.as_document())
        except (OSError,ValueError,TypeError,AttributeError,EOFError,subprocess.SubprocessError):return None

    def _native_helper(self, provider: str) -> Optional[ProviderQuota]:
        if provider == "antigravity":
            from quota_sentinel.platform.paths import resolve_launcher
            try:resolve_launcher('agy',explicit=self.agy_bin)
            except ValueError:return None
            timeout = self.antigravity_native_timeout
            command = [str(self.python_bin), "-B", str(self.antigravity_usage_helper),
                       "--agy", str(self.agy_bin), "--timeout", str(timeout)]
            stdin = None
        elif provider == "opencode":
            if not self.opencode_usage_helper.is_file():
                return None
            key = self._api_key(_OPENCODE_API_KEY_SERVICE, "OPENCODE_API_KEY")
            if not key:
                return None
            timeout = self.opencode_native_timeout
            command = [str(self.python_bin), "-B", str(self.opencode_usage_helper),
                       "--curl", str(self.curl_bin), "--timeout", str(timeout)]
            stdin = (key + "\n").encode()
        elif provider == "clinepass":
            if not self.clinepass_usage_helper.is_file():
                return None
            key = self._api_key(_CLINEPASS_API_KEY_SERVICE, "CLINE_API_KEY")
            if not key:
                return None
            timeout = self.clinepass_native_timeout
            command = [str(self.python_bin), "-B", str(self.clinepass_usage_helper),
                       "--curl", str(self.curl_bin), "--timeout", str(timeout)]
            stdin = (key + "\n").encode()
        else:
            # No native helper exists for this provider; its ladder starts at
            # CodexBar. Returning None is the honest "this rung has nothing" the
            # collector already understands, and it keeps one provider's command
            # from being built out of another provider's helper.
            return None
        result = _run_bounded(command, timeout + _NATIVE_HELPER_SLACK_SECONDS, 1, stdin, environment=self.environment)
        if result.returncode != 0 or not result.stdout:
            reason_match = re.search(
                rb"(?:antigravity_usage|opencode_usage|clinepass_usage): ([a-z_0-9]+)",
                result.stderr,
            )
            reason = reason_match.group(1).decode() if reason_match else "runtime_unavailable"
            self.logger("quota %s: native /usage failed (%s)" % (provider, reason))
            return None
        raw = _bytes_json(result.stdout)
        if not isinstance(raw, dict):
            return None
        raw.setdefault("cached", False)
        try:
            quota = parse_document(raw)
            return quota if quota.fresh and not quota.cached else None
        except QuotaNormalizationError:
            return None

    def _native(self, provider: str) -> Optional[ProviderQuota]:
        return self._native_codex() if provider == "codex" else self._native_helper(provider)

    def _codexbar(self, provider: str) -> Optional[ProviderQuota]:
        from quota_sentinel.platform.paths import resolve_launcher
        try:prefix=resolve_launcher('codexbar',explicit=self.codexbar_bin)
        except ValueError:return None
        names = {"opencode": "opencodego"}
        # CodexBar's ClinePass provider documents exactly two source modes
        # (auto and api) and reaches the vendor with an API key, like OpenCode's.
        api_only = provider in ("opencode", "clinepass")
        sources = ("cli", "oauth") if provider == "codex" else (("api",) if api_only else ("cli",))
        timeout = (self.antigravity_codexbar_timeout if provider == "antigravity" else
                   self.opencode_codexbar_timeout if provider == "opencode" else
                   self.clinepass_codexbar_timeout if provider == "clinepass" else
                   self.codexbar_timeout)
        for source in sources:
            command = [*prefix, "usage", "--provider", names.get(provider, provider),
                       "--source", source, "--format", "json", "--json-only", "--no-color"]
            result = _run_bounded(command, timeout, self.codexbar_kill_grace, environment=self.environment)
            if result.timed_out:
                self.logger("quota %s: codexbar-live TIMEOUT after %ss (source %s)" % (provider, timeout, source))
            if result.returncode != 0:
                continue
            raw = _bytes_json(result.stdout)
            if raw is None:
                continue
            try:
                quota = _NORMALIZE_BAR[provider](raw)
            except QuotaNormalizationError:
                continue
            try:
                self._save_cache(provider, quota)
            except OSError:
                pass  # A live reading remains usable when cache persistence fails.
            return quota
        return None

    def collect(self, pi_raw: Optional[Mapping[str, Path]] = None, *, purpose: str = "display") -> Dict[str, QuotaReading]:
        """Resolve each provider by its declared ladder; no stale carry-over.

        Each rung is an ISOLATION boundary. The shell gets that for free —
        every tier is its own subprocess, so a crash is just a non-zero rc
        and the ladder moves on. In-process the same guarantee has to be
        written down: without it one dead codex app-server or one corrupt
        cache file would take the whole roster's readings with it, and the
        caller would see an exception instead of a degraded collection.
        """
        if purpose not in ("display", "schedule"):
            raise ValueError("unknown quota purpose")
        readings: Dict[str, QuotaReading] = {}
        for provider in self.providers:
            quota = None
            selected = None
            for tier in (self.tier_chains[provider] if self.tier_chains is not None else tier_plan(provider)):
                if tier not in _TIERS:
                    # The shell dies on an unknown tier rather than serving
                    # something else, and a silent fallback here would make a
                    # plan regression look like a stale reading.
                    raise ValueError(
                        "unknown quota tier %r for provider %r" % (tier, provider)
                    )
                started = time.monotonic()
                try:
                    quota = self._tier(provider, tier, pi_raw)
                except Exception as error:  # one rung, not the roster
                    self.logger("quota %s: %s raised %s" % (
                        provider, tier.value, type(error).__name__,
                    ))
                    quota = None
                # Instrumentation, deliberately not policy: this line only
                # records what the ladder did. Without it a rung that hangs
                # until its timeout and one that fails in milliseconds are
                # indistinguishable from outside (the run log shows only the
                # whole check's duration), and the tier that actually served
                # the reading is never written down at all. It carries
                # provider, tier, outcome and seconds — nothing from the
                # vendor CLI's output.
                self.logger("quota %s: %s %s in %.1fs" % (
                    provider, tier.value,
                    "ok" if quota is not None else "none",
                    time.monotonic() - started,
                ))
                if quota is not None and (purpose == "display" or tier in FRESH_TIERS and quota.fresh and not quota.cached):
                    selected = tier
                    break
            if selected is None:
                self.logger("quota %s: selected none (all tiers unavailable)"
                            % provider)
                readings[provider] = QuotaReading(None, None, False, "all tiers unavailable")
                continue
            fresh = selected in FRESH_TIERS and quota.fresh and not quota.cached
            self.logger("quota %s: selected %s (fresh=%d)" % (
                provider, selected.value, 1 if fresh else 0,
            ))
            readings[provider] = QuotaReading(quota, selected, fresh)
        return readings

    def _tier(
        self, provider: str, tier: Tier, pi_raw: Optional[Mapping[str, Path]]
    ) -> Optional[ProviderQuota]:
        if tier is Tier.NATIVE:
            return self._native(provider)
        if tier is Tier.PI_LIVE:
            if self.pi_live_client is None:
                return None
            result = self.pi_live_client.query(provider)
            return result.quota if result.fresh and result.quota is not None and result.quota.fresh and not result.quota.cached else None
        if tier is Tier.CODEXBAR_LIVE:
            return self._codexbar(provider)
        if tier is Tier.CODEXBAR_CACHE:
            return self._read_cache(provider)
        if tier is Tier.PI_SNAPSHOT:
            return self._read_pi(provider, pi_raw)
        raise ValueError("unknown quota tier %r" % (tier,))


__all__ = ["QuotaCollector", "QuotaReading"]
