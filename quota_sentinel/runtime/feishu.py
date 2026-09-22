"""Feishu credentials, message transport and user discovery.

The network and Keychain boundaries are injectable. Push secrets travel in
JSON request bodies or in-process headers, never in a process argument/URL.
"""
from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
from typing import Any, Mapping, Optional
from urllib import error, request

from quota_sentinel.quota.adapters import PROVIDERS
from quota_sentinel.runtime import keychain
from quota_sentinel.runtime.cards import (
    render_busy_card, render_task_card, render_task_text_card,
    render_usage_card, render_usage_text_card,
)


API_BASE = "https://open.feishu.cn/open-apis"
KEYCHAIN_ACCOUNT = keychain.KEYCHAIN_ACCOUNT
SERVICES = {
    "app_id": "quota-sentinel.feishu-app-id",
    "app_secret": "quota-sentinel.feishu-app-secret",
    "user_id": "quota-sentinel.feishu-user-id",
}
ENV_NAMES = {
    "app_id": "FEISHU_APP_ID",
    "app_secret": "FEISHU_APP_SECRET",
    "user_id": "FEISHU_USER_ID",
}
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}
# 3.9 still has socket.timeout as its own OSError subclass; 3.10+ aliases it
# to TimeoutError, so both names are listed.
TIMEOUT_ERRORS = (TimeoutError, socket.timeout)
_MAX_RESPONSE_BYTES = 1024 * 1024
_ERROR_BODY_BYTES = 4096


def _bound_socket(response: Any, remaining: float) -> None:
    """Best-effort: make the next read block for at most `remaining`."""
    sock = None
    try:
        sock = response.fp.raw._sock
    except AttributeError:
        sock = getattr(getattr(response, "fp", None), "raw", None)
    setter = getattr(sock, "settimeout", None)
    if setter is not None:
        try:
            setter(max(0.001, remaining))
        except OSError:
            pass


def _read_within(response: Any, deadline: float) -> bytes:
    """Read a response under a wall-clock deadline, chunk by chunk."""
    chunks = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FeishuTimeout("Feishu response exceeded its total timeout")
        _bound_socket(response, remaining)
        reader = getattr(response, "read1", None) or response.read
        chunk = reader(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise FeishuError("Feishu returned an oversized response")


class FeishuError(RuntimeError):
    """Authentication, delivery or lookup failed without exposing credentials."""


class FeishuTimeout(FeishuError):
    """A request exceeded its wall-clock budget rather than a socket read."""


class KeychainCredentials:
    """Read environment first, then the macOS generic-password Keychain."""

    def __init__(self, environment: Optional[Mapping[str, str]] = None, security_bin: str = keychain.SECURITY_BIN):
        self.environment = os.environ if environment is None else environment
        self.security_bin = security_bin

    def get(self, name: str) -> str:
        if name not in SERVICES:
            raise KeyError(name)
        env_value = self.environment.get(ENV_NAMES[name], "")
        if env_value:
            return env_value
        return keychain.read(
            SERVICES[name], security_bin=self.security_bin,
            environment=self.environment, timeout=15,
        )

    def save_user_id(self, value: str) -> None:
        try:
            done = subprocess.run(
                [self.security_bin, "add-generic-password", "-U", "-a", KEYCHAIN_ACCOUNT,
                 "-s", SERVICES["user_id"], "-T", self.security_bin, "-w", value],
                check=False, capture_output=True, text=True,
            )
        except OSError as exc:
            raise FeishuError("could not save Feishu user ID in Keychain") from exc
        if done.returncode != 0:
            raise FeishuError("could not save Feishu user ID in Keychain")


class UrllibHttp:
    """Small HTTP boundary; a fake with the same post method suffices in tests."""

    def post(self, url: str, body: bytes, headers: Mapping[str, str], connect_timeout: int, total_timeout: int) -> dict:
        budget = max(0.0, float(total_timeout))
        if budget == 0:
            raise FeishuTimeout("Feishu request exceeded its total timeout")
        req = request.Request(url, data=body, headers=dict(headers), method="POST")
        deadline = time.monotonic() + budget
        outcome: queue.Queue = queue.Queue(maxsize=1)

        def exchange() -> None:
            try:
                # urllib bounds individual socket operations, not the whole
                # exchange. A daemon worker keeps the calling CLI's run.lock
                # bounded even if DNS or response headers stall indefinitely.
                with request.urlopen(
                    req, timeout=max(0.001, min(float(connect_timeout), budget))
                ) as response:
                    raw = _read_within(response, deadline)
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise FeishuError("Feishu returned invalid JSON") from exc
                if not isinstance(value, dict):
                    raise FeishuError("Feishu returned a non-object response")
                outcome.put((True, value))
            except Exception as exc:
                # HTTPError retains its bounded response stream so the caller
                # can classify retryable status codes and read code/msg.
                outcome.put((False, exc))

        threading.Thread(target=exchange, name="feishu-http", daemon=True).start()
        try:
            succeeded, value = outcome.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise FeishuTimeout("Feishu request exceeded its total timeout") from exc
        if not succeeded:
            raise value
        return value


def lookup_payload(identifier: str) -> dict:
    """Match the Shell discovery rule, including +86 normalization."""
    if "@" in identifier:
        return {"emails": [identifier]}
    if re.fullmatch(r"\+?[0-9]+", identifier):
        return {"mobiles": [identifier[3:] if identifier.startswith("+86") else identifier]}
    raise ValueError(f"expected an email address or mobile number: {identifier}")


class FeishuClient:
    def __init__(self, credentials: Any = None, *, http: Any = None, sleep: Any = None, dry_run: Optional[bool] = None, api_base: str = API_BASE, environment: Optional[Mapping[str, str]] = None):
        self.credentials = credentials if credentials is not None else KeychainCredentials(environment)
        self.http = http if http is not None else UrllibHttp()
        self.sleep = sleep if sleep is not None else time.sleep
        # FEISHU_DRY_RUN is the operator's existing safety switch: it prints
        # the payload instead of delivering it. An explicit argument wins,
        # exactly like FEISHU_DISABLE_CHART below.
        env = os.environ if environment is None else environment
        self.dry_run = (env.get("FEISHU_DRY_RUN") == "1") if dry_run is None else dry_run
        self.api_base = api_base.rstrip("/")

    def _credential(self, name: str) -> str:
        value = self.credentials.get(name)
        if not value:
            raise FeishuError(f"missing Feishu {name.replace('_', ' ')}")
        return value

    def require_credentials(self) -> None:
        """Preflight every push credential in order, without any network call.

        This is the shell's ``feishu_ready`` gate. It only proves presence,
        so the failure names which credential is absent and never echoes a
        value — a preflight that leaked the secret it was checking would be
        worse than no preflight at all.
        """
        for name in ("app_id", "app_secret", "user_id"):
            self._credential(name)

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        """Exactly what curl's --retry retries: timeouts and transient statuses.

        Deliberately NOT a bare OSError. DNS failures, TLS failures and
        ECONNREFUSED are not transient for curl either (it needs
        --retry-connrefused for the last one), and retrying them only delays
        the operator's diagnosis by two backoffs.
        """
        if isinstance(exc, error.HTTPError):
            return exc.code in TRANSIENT_HTTP_CODES
        if isinstance(exc, TIMEOUT_ERRORS):
            return True
        if isinstance(exc, error.URLError):
            return isinstance(exc.reason, TIMEOUT_ERRORS)
        return False

    @staticmethod
    def _http_error_detail(exc: error.HTTPError) -> str:
        """The Feishu code/msg a non-2xx body carries, bounded and sanitized.

        The shell reaches these through `curl --fail-with-body`; without them
        an operator sees "app not in chat" as an opaque failure.
        """
        try:
            raw = exc.read(_ERROR_BODY_BYTES)
        except Exception:
            return " (HTTP %s)" % exc.code
        try:
            document = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return " (HTTP %s)" % exc.code
        if not isinstance(document, dict):
            return " (HTTP %s)" % exc.code
        code = document.get("code", exc.code)
        message = document.get("msg", "")
        if not isinstance(message, str):
            message = ""
        detail = " (code %s" % code
        if message:
            detail += ": " + message[:200]
        return detail + ")"

    def _post(self, path: str, body: Mapping[str, Any], *, token: Optional[str] = None, retries: int = 0) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(retries + 1):
            try:
                result = self.http.post(
                    f"{self.api_base}/{path}", self._encode(body), headers, 15, 45,
                )
                if isinstance(result, bytes):
                    result = json.loads(result)
                if not isinstance(result, dict):
                    raise FeishuError("Feishu returned a non-object response")
                return result
            except FeishuTimeout:
                if attempt >= retries:
                    raise
                self.sleep(min(2 ** attempt, 4))
            except FeishuError:
                raise
            except error.HTTPError as exc:
                retryable = self._retryable(exc)
                if attempt >= retries or not retryable:
                    try:
                        detail = self._http_error_detail(exc)
                    finally:
                        exc.close()
                    raise FeishuError("Feishu request failed%s" % detail) from exc
                exc.close()
                self.sleep(min(2 ** attempt, 4))
            except Exception as exc:
                if attempt >= retries or not self._retryable(exc):
                    raise FeishuError("Feishu request failed") from exc
                self.sleep(min(2 ** attempt, 4))
        raise AssertionError("unreachable")

    @staticmethod
    def _check(result: Mapping[str, Any], action: str) -> None:
        if result.get("code") != 0:
            code = result.get("code", "unknown")
            message = result.get("msg", "rejected")
            if not isinstance(message, str):
                message = "rejected"
            raise FeishuError(f"Feishu {action} rejected (code {code}): {message[:200]}")

    def tenant_token(self) -> str:
        result = self._post(
            "auth/v3/tenant_access_token/internal",
            {"app_id": self._credential("app_id"), "app_secret": self._credential("app_secret")},
        )
        token = result.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            self._check(result, "token request")
            raise FeishuError("Feishu token response had no tenant access token")
        return token

    def send(self, payload: Mapping[str, Any]) -> Optional[dict]:
        """Send an interactive envelope; dry-run prints it and skips HTTP."""
        # Shell dispatch checks all push credentials even for dry runs.
        self.require_credentials()
        if not payload.get("receive_id") or payload.get("msg_type") != "interactive":
            raise FeishuError("invalid Feishu interactive message envelope")
        if not isinstance(payload.get("content"), str):
            raise FeishuError("Feishu content must be a JSON string")
        if self.dry_run:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return None
        result = self._post(
            "im/v1/messages?receive_id_type=user_id", payload,
            token=self.tenant_token(), retries=2,
        )
        self._check(result, "message")
        return result

    def discover_user(self, identifier: str) -> str:
        body = lookup_payload(identifier)
        result = self._post(
            "contact/v3/users/batch_get_id?user_id_type=user_id", body,
            token=self.tenant_token(), retries=2,
        )
        self._check(result, "user lookup")
        data = result.get("data")
        users = data.get("user_list") if isinstance(data, dict) else None
        user = users[0] if isinstance(users, list) and users else None
        user_id = user.get("user_id") if isinstance(user, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise FeishuError(f"no Feishu user matched: {identifier}")
        self.credentials.save_user_id(user_id)
        return user_id


class FeishuNotifier:
    """Application-facing adapter: one rendered message per event."""

    def __init__(self, client: FeishuClient, *, roster=PROVIDERS, clock=time.time, disable_chart: Optional[bool] = None):
        self.client = client
        self.roster = tuple(roster)
        self.clock = clock
        self.disable_chart = (
            os.environ.get("FEISHU_DISABLE_CHART") == "1"
            if disable_chart is None else disable_chart
        )

    def _user(self) -> str:
        user_id = self.client.credentials.get("user_id")
        if not user_id:
            raise FeishuError("missing Feishu user id")
        return user_id

    def validate_ready(self) -> None:
        """Fail fast when this deployment cannot push at all.

        ``run`` and ``check`` call this BEFORE any model attempt: a missing
        push credential discovered after a 40-minute provider run is a
        failure the operator paid for and cannot use.
        """
        self.client.require_credentials()

    def task(self, providers, results, readings, now):
        if not providers:
            return None
        user_id = self._user()
        uuid = f"quota-sentinel-{int(now)}"
        if self.disable_chart:
            payload = render_task_text_card(providers, results, readings, user_id, uuid, now=int(now))
        else:
            try:
                payload = render_task_card(providers, results, readings, user_id, uuid, now=int(now))
            except (TypeError, ValueError, KeyError, OverflowError):
                payload = render_task_text_card(providers, results, readings, user_id, uuid, now=int(now))
        return self.client.send(payload)

    def usage(self, readings, now):
        user_id = self._user()
        uuid = f"quota-sentinel-{int(now)}"
        if self.disable_chart:
            payload = render_usage_text_card(self.roster, readings, user_id, uuid, now=int(now))
        else:
            try:
                payload = render_usage_card(self.roster, readings, user_id, uuid, now=int(now))
            except (TypeError, ValueError, KeyError, OverflowError):
                payload = render_usage_text_card(self.roster, readings, user_id, uuid, now=int(now))
        return self.client.send(payload)

    def busy(self, now):
        return self.client.send(render_busy_card(self._user(), f"quota-sentinel-busy-{int(now)}"))


__all__ = ["FeishuClient", "FeishuError", "FeishuNotifier", "KeychainCredentials", "UrllibHttp", "lookup_payload"]
