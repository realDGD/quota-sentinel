"""Normalized quota document: the typed shape every provider's normaliser
produces and every reader consumes.

The on-disk document is the JSON object the shell writes after
`normalize_*` (+ `renormalise_quota_file`); its key set is exact and
order-independent:

    {"source": str, "fresh": bool, "cached": bool, "capturedAt": int|null,
     "fiveHour": {"remainingPercent": int, "resetAt": int},
     "weekly":   {"remainingPercent": int, "resetAt": int}}
    plus "monthly" (same window shape, never null) only for providers that
    carry a monthly cap.

`parse_document` is deliberately strict: it accepts exactly this shape and
rejects everything else, so a document that reached disk through a partial
or hand-edited writer can never be mistaken for a normalized quota. As in
the shell's jq programs, `bool` is not an integer: `True` never satisfies
an int field.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = [
    "ProviderQuota",
    "QuotaNormalizationError",
    "QuotaWindow",
    "parse_document",
]

# Top-level keys that are always present; "monthly" is the only optional one
# and is omitted (not null) when a provider has no monthly window.
_BASE_KEYS = frozenset(
    {"source", "fresh", "cached", "capturedAt", "fiveHour", "weekly"}
)
_OPTIONAL_KEYS = frozenset({"monthly"})
_WINDOW_KEYS = frozenset({"remainingPercent", "resetAt"})


class QuotaNormalizationError(ValueError):
    """A quota payload could not be normalized to the document shape.

    Raised for every input the shell's jq programs would have rejected
    (produced no output) as well as for a malformed normalized document.
    """


@dataclass(frozen=True)
class QuotaWindow:
    """One rate-limit window: percent still available and its reset epoch."""

    remaining_percent: int
    reset_at: int

    def as_document(self) -> Dict[str, Any]:
        return {
            "remainingPercent": self.remaining_percent,
            "resetAt": self.reset_at,
        }


@dataclass(frozen=True)
class ProviderQuota:
    """A fully normalized quota reading for one provider."""

    source: str
    fresh: bool
    cached: bool
    captured_at: Optional[int]
    five_hour: QuotaWindow
    weekly: QuotaWindow
    monthly: Optional[QuotaWindow] = None

    def as_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "source": self.source,
            "fresh": self.fresh,
            "cached": self.cached,
            "capturedAt": self.captured_at,
            "fiveHour": self.five_hour.as_document(),
            "weekly": self.weekly.as_document(),
        }
        if self.monthly is not None:
            document["monthly"] = self.monthly.as_document()
        return document


def _parse_window(value: Any, label: str) -> QuotaWindow:
    if not isinstance(value, dict):
        raise QuotaNormalizationError(
            "%s must be an object, got %s" % (label, type(value).__name__)
        )
    keys = set(value)
    missing = sorted(_WINDOW_KEYS - keys)
    extra = sorted(keys - _WINDOW_KEYS)
    if missing:
        raise QuotaNormalizationError(
            "%s is missing key(s): %s" % (label, ", ".join(missing))
        )
    if extra:
        raise QuotaNormalizationError(
            "%s has unexpected key(s): %s" % (label, ", ".join(extra))
        )
    remaining = value["remainingPercent"]
    if type(remaining) is not int:
        raise QuotaNormalizationError(
            "%s.remainingPercent must be an int, got %s"
            % (label, type(remaining).__name__)
        )
    if not 0 <= remaining <= 100:
        raise QuotaNormalizationError(
            "%s.remainingPercent out of range 0..100: %d" % (label, remaining)
        )
    reset_at = value["resetAt"]
    if type(reset_at) is not int:
        raise QuotaNormalizationError(
            "%s.resetAt must be an int, got %s"
            % (label, type(reset_at).__name__)
        )
    return QuotaWindow(remaining_percent=remaining, reset_at=reset_at)


def parse_document(raw: dict) -> ProviderQuota:
    """Validate a normalized quota document and return it typed.

    Rejects a missing or extra key, a non-bool `fresh`/`cached`, a
    `capturedAt` that is neither None nor a plain int, a `monthly` key whose
    value is null, and any window that is not exactly
    `{"remainingPercent": int 0..100, "resetAt": int}`.
    """
    if not isinstance(raw, dict):
        raise QuotaNormalizationError(
            "quota document must be an object, got %s" % type(raw).__name__
        )
    keys = set(raw)
    missing = sorted(_BASE_KEYS - keys)
    extra = sorted(keys - _BASE_KEYS - _OPTIONAL_KEYS)
    if missing:
        raise QuotaNormalizationError(
            "quota document is missing key(s): %s" % ", ".join(missing)
        )
    if extra:
        raise QuotaNormalizationError(
            "quota document has unexpected key(s): %s" % ", ".join(extra)
        )

    source = raw["source"]
    if not isinstance(source, str):
        raise QuotaNormalizationError(
            "source must be a string, got %s" % type(source).__name__
        )
    fresh = raw["fresh"]
    if type(fresh) is not bool:
        raise QuotaNormalizationError(
            "fresh must be a bool, got %s" % type(fresh).__name__
        )
    cached = raw["cached"]
    if type(cached) is not bool:
        raise QuotaNormalizationError(
            "cached must be a bool, got %s" % type(cached).__name__
        )
    captured_at = raw["capturedAt"]
    if captured_at is not None and type(captured_at) is not int:
        raise QuotaNormalizationError(
            "capturedAt must be null or an int, got %s"
            % type(captured_at).__name__
        )

    five_hour = _parse_window(raw["fiveHour"], "fiveHour")
    weekly = _parse_window(raw["weekly"], "weekly")

    monthly: Optional[QuotaWindow] = None
    if "monthly" in raw:
        if raw["monthly"] is None:
            raise QuotaNormalizationError(
                "monthly must be omitted when absent, not null"
            )
        monthly = _parse_window(raw["monthly"], "monthly")

    return ProviderQuota(
        source=source,
        fresh=fresh,
        cached=cached,
        captured_at=captured_at,
        five_hour=five_hour,
        weekly=weekly,
        monthly=monthly,
    )
