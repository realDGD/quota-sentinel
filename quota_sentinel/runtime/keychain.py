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
import subprocess
from typing import Mapping, Optional

KEYCHAIN_ACCOUNT = "quota-sentinel"
SECURITY_BIN = "/usr/bin/security"
# Explicit opt-out for tests and CI: without it, a headless host would still
# consult the login Keychain and could pick up a real operator's credentials.
DISABLE_ENV = "QUOTA_SENTINEL_KEYCHAIN_DISABLED"


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
    try:
        result = subprocess.run(
            [security_bin, "find-generic-password", "-a", account,
             "-s", service, "-w"],
            check=False, capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace").strip()


def present(service: str, **kwargs) -> bool:
    return bool(read(service, **kwargs))


__all__ = ["DISABLE_ENV", "KEYCHAIN_ACCOUNT", "SECURITY_BIN", "disabled",
           "present", "read"]
