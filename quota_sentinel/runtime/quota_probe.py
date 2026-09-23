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
_ROOT = Path(__file__).resolve().parents[2]
_MAX_PROBE_BYTES = 1024 * 1024
# The shell has no outer bound on the native helper: only the helper's own
# --timeout applies, and uv + interpreter start-up happens BEFORE that clock
# starts. A tight outer bound would SIGTERM probes the shell accepts and
# silently fall back to CodexBar, so this is deliberately generous.
_NATIVE_HELPER_SLACK_SECONDS = 5
_TIERS = (Tier.NATIVE, Tier.CODEXBAR_LIVE, Tier.CODEXBAR_CACHE, Tier.PI_SNAPSHOT)
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
) -> _CommandResult:
    """Capture a child under a process-group deadline; never expose argv."""
    try:
        process = subprocess.Popen(
            list(command), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError:
        return _CommandResult(b"", b"", 127)
    try:
        try:
            out, err = process.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process, signal.SIGTERM)
            try:
                out, err = process.communicate(timeout=grace)
            except subprocess.TimeoutExpired as expired:
                # A detached descendant can keep our pipes open after the
                # direct child is dead. communicate() without a deadline would
                # wait for that descendant forever while quota.lock is held.
                _kill_group(process, signal.SIGKILL)
                out, err = expired.output or b"", expired.stderr or b""
            return _CommandResult(out[:_MAX_PROBE_BYTES], err[:_MAX_PROBE_BYTES], 124, True)
        if len(out) > _MAX_PROBE_BYTES or len(err) > _MAX_PROBE_BYTES:
            return _CommandResult(b"", b"", 1)
        return _CommandResult(out, err, process.returncode)
    finally:
        if process.poll() is None:
            _kill_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


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
        logger: Optional[Callable[[str], None]] = None,
        codexbar_timeout: float = 20,
        antigravity_codexbar_timeout: float = 35,
        opencode_codexbar_timeout: float = 20,
        clinepass_codexbar_timeout: float = 20,
        antigravity_native_timeout: float = 20,
        opencode_native_timeout: float = 15,
        clinepass_native_timeout: float = 15,
        codexbar_kill_grace: float = 10,
    ) -> None:
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
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(document, output, ensure_ascii=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(name, 0o600)
            os.replace(name, path)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise

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
        if service == _OPENCODE_API_KEY_SERVICE and self.opencode_api_key_getter is not None:
            return self.opencode_api_key_getter() or ""
        value = os.environ.get(env_key, "")
        if value:
            return value
        return keychain.read(
            service, security_bin=str(self.security_bin), timeout=5,
        )

    def _native_codex(self) -> Optional[ProviderQuota]:
        if not os.access(self.codex_bin, os.X_OK):
            return None
        try:
            process = subprocess.Popen(
                [str(self.codex_bin), "app-server"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return None
        pending = b""
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)

                def request(request_id: int, method: str, params: dict):
                    nonlocal pending
                    data = json.dumps({"jsonrpc": "2.0", "id": request_id,
                                       "method": method, "params": params}).encode() + b"\n"
                    process.stdin.write(data)
                    process.stdin.flush()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            response = _bytes_json(line)
                            if isinstance(response, dict) and response.get("id") == request_id:
                                return response
                        events = selector.select(max(0, deadline - time.monotonic()))
                        if not events:
                            break
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk or len(pending) + len(chunk) > _MAX_PROBE_BYTES:
                            break
                        pending += chunk
                    return None

                if request(1, "initialize", {"clientInfo": {
                    "name": "quota-sentinel", "version": "1.0"}}) is None:
                    return None
                response = request(2, "account/rateLimits/read", {})
                if not isinstance(response, dict):
                    return None
                limits = response.get("result", {}).get("rateLimits", {})
                primary, secondary = limits.get("primary"), limits.get("secondary")
                if not isinstance(primary, dict) or not isinstance(secondary, dict) or not primary or not secondary:
                    return None
                five = primary if primary.get("windowDurationMins", 300) <= 360 else secondary
                weekly = secondary if secondary.get("windowDurationMins", 10080) > 360 else primary

                def window(value: dict) -> QuotaWindow:
                    used = value.get("usedPercent", 0)
                    reset = value.get("resetsAt")
                    if type(used) not in (int, float) or not math.isfinite(used) or type(reset) is not int or not reset:
                        raise ValueError("invalid native window")
                    remaining = math.floor(max(0, min(100, 100 - used)) + 0.5)
                    return QuotaWindow(remaining, reset)

                quota = ProviderQuota("Native · codex app-server", True, False,
                                      int(time.time()), window(five), window(weekly))
                return parse_document(quota.as_document())
        except (BrokenPipeError, OSError, ValueError, TypeError, AttributeError):
            return None
        finally:
            _kill_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _kill_group(process, signal.SIGKILL)
                process.wait()
            # An app-server that died mid-handshake leaves a broken pipe; a
            # close() that re-raises from here would escape collect() and
            # cost every other provider its reading.
            with contextlib.suppress(OSError):
                process.stdin.close()
            with contextlib.suppress(OSError):
                process.stdout.close()

    def _native_helper(self, provider: str) -> Optional[ProviderQuota]:
        if provider == "antigravity":
            if not os.access(self.agy_bin, os.X_OK) or not os.access(self.uv_bin, os.X_OK):
                return None
            timeout = self.antigravity_native_timeout
            command = [str(self.uv_bin), "run", "--offline", "--no-project", "--no-config",
                       "python", "-B", str(self.antigravity_usage_helper),
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
        result = _run_bounded(command, timeout + _NATIVE_HELPER_SLACK_SECONDS, 1, stdin)
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
        if not os.access(self.codexbar_bin, os.X_OK):
            return None
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
            command = [str(self.codexbar_bin), "usage", "--provider", names.get(provider, provider),
                       "--source", source, "--format", "json", "--json-only", "--no-color"]
            result = _run_bounded(command, timeout, self.codexbar_kill_grace)
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

    def collect(self, pi_raw: Optional[Mapping[str, Path]] = None) -> Dict[str, QuotaReading]:
        """Resolve each provider by its declared ladder; no stale carry-over.

        Each rung is an ISOLATION boundary. The shell gets that for free —
        every tier is its own subprocess, so a crash is just a non-zero rc
        and the ladder moves on. In-process the same guarantee has to be
        written down: without it one dead codex app-server or one corrupt
        cache file would take the whole roster's readings with it, and the
        caller would see an exception instead of a degraded collection.
        """
        readings: Dict[str, QuotaReading] = {}
        for provider in PROVIDERS:
            quota = None
            selected = None
            for tier in tier_plan(provider):
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
                if quota is not None:
                    selected = tier
                    break
            if selected is None:
                self.logger("quota %s: selected none (all tiers unavailable)"
                            % provider)
                readings[provider] = QuotaReading(None, None, False, "all tiers unavailable")
                continue
            fresh = selected in (Tier.NATIVE, Tier.CODEXBAR_LIVE)
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
        if tier is Tier.CODEXBAR_LIVE:
            return self._codexbar(provider)
        if tier is Tier.CODEXBAR_CACHE:
            return self._read_cache(provider)
        if tier is Tier.PI_SNAPSHOT:
            return self._read_pi(provider, pi_raw)
        raise ValueError("unknown quota tier %r" % (tier,))


__all__ = ["QuotaCollector", "QuotaReading"]
