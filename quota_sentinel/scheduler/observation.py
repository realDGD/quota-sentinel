"""Reading one provider's normalised quota probe (Phase 3C seam).

The shell's quota layer (Phase 4 will move it into Python adapters) already
normalises every tier into one small JSON shape:

    {"source": "...", "fresh": true, "capturedAt": 123,
     "fiveHour": {"remainingPercent": 90, "resetAt": 456},
     "weekly": {...}, "monthly": {...}}   # monthly: OpenCode only

The scheduler needs exactly two things from it: the trust gate and the
five-hour reset. This module extracts those and nothing else.

Two rules, both deliberate:

* A missing, unreadable or malformed file is NO observation — never a
  zero, never "now". Absence of evidence must not be able to move a
  deadline, so every failure path returns ``fresh=False``.
* Only the five-hour window is read. OpenCode's ``monthly`` block is
  display-only and must never reach deadline policy; this reader cannot
  see it, which is a stronger guarantee than a comment telling callers
  not to look.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .models import NO_OBSERVATION, QuotaObservation


def _plain_int(value: object) -> Optional[int]:
    # type() (not isinstance) so bool cannot masquerade as an epoch.
    return value if type(value) is int and value >= 0 else None


def read_normalized_quota(path: Optional[Path]) -> QuotaObservation:
    """Best-effort observation from a normalised quota file."""
    if path is None:
        return NO_OBSERVATION
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return NO_OBSERVATION
    try:
        document = json.loads(raw)
    except ValueError:
        return NO_OBSERVATION
    if not isinstance(document, dict):
        return NO_OBSERVATION

    five_hour = document.get("fiveHour")
    reset_at: Optional[int] = None
    if isinstance(five_hour, dict):
        reset_at = _plain_int(five_hour.get("resetAt"))

    source = document.get("source")
    return QuotaObservation(
        fresh=document.get("fresh") is True,
        reset_at=reset_at,
        source=source if isinstance(source, str) else "unknown",
    )


def observation_for(
    quota_file: Optional[Path], fresh_override: Optional[bool]
) -> QuotaObservation:
    """Observation with the caller's freshness verdict applied on top.

    The shell's freshness test is ``file says fresh`` OR ``this probe
    reported fresh`` (the tiers set a flag when they produced live data).
    Passing that verdict explicitly keeps the two sources of truth in the
    caller where they already live, instead of this reader inventing a
    third rule. A caller that passes ``False`` can only lower freshness,
    never raise it above what the file itself claims.
    """
    observation = read_normalized_quota(quota_file)
    if fresh_override is None:
        return observation
    return QuotaObservation(
        fresh=observation.fresh and fresh_override,
        reset_at=observation.reset_at,
        source=observation.source,
    )


__all__ = ["read_normalized_quota", "observation_for"]
