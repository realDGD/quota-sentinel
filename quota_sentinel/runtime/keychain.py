"""The one macOS Keychain boundary used by every credential in the port.

Two callers need the same thing: the push credentials (app id, app secret,
user id) and the OpenCode Go quota API key. Keeping one implementation means
one place where the service names live, one place that decides how a missing
Keychain is reported, and one place a test can turn the Keychain off.

Nothing here ever returns a value to a log or an argument list; callers own
that, and they pass secrets to child processes over stdin only.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

KEYCHAIN_ACCOUNT = "quota-sentinel"
SECURITY_BIN = "/usr/bin/security"
# Explicit opt-out for tests and CI: without it, a headless host would still
# consult the login Keychain and could pick up a real operator's credentials.
DISABLE_ENV = "QUOTA_SENTINEL_KEYCHAIN_DISABLED"
# Every read is bounded even when the caller forgets the argument. A wedged
# `security` (an unanswered Keychain prompt, stuck IPC) would otherwise block
# the readiness gate, the direct transport's key lookup and the /usage answer
# forever — and those are exactly the paths where a missing credential is
# supposed to fail in milliseconds. `None` therefore means "the default", not
# "unbounded"; a caller with its own budget still passes it explicitly.
DEFAULT_READ_TIMEOUT_SECONDS = 15


def disabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environment is None else environment
    return env.get(DISABLE_ENV) == "1"


def read(
    service: str,
    *,
    account: str = KEYCHAIN_ACCOUNT,
    security_bin: str = SECURITY_BIN,
    environment: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> str:
    """Read a generic password; an absent Keychain or item is empty, not fatal."""
    if disabled(environment):
        return ""
    from quota_sentinel.config import CredentialReference
    from quota_sentinel.platform.credentials import CredentialStore, CredentialUnavailable
    budget = DEFAULT_READ_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        return CredentialStore(environment=environment, security_bin=security_bin).read(
            CredentialReference('system', service, account), timeout=budget)
    except CredentialUnavailable:
        return ""


def present(service: str, **kwargs) -> bool:
    return bool(read(service, **kwargs))


__all__ = ["DEFAULT_READ_TIMEOUT_SECONDS", "DISABLE_ENV", "KEYCHAIN_ACCOUNT",
           "SECURITY_BIN", "disabled", "present", "read"]
