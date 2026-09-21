"""Pure-Python transcription of the shell's jq quota normalisers.

Every function here mirrors one jq program in `quota-sentinel.sh` — and
mirrors it literally, including the cases where the jq produces no output at
all. "No output" (a failed `select(...)`, a `tonumber`/`fromdateiso8601`
error, an `empty` in an object value) is always surfaced as
`QuotaNormalizationError`; a partially-filled quota is never returned and a
missing window is never invented.

Transcription notes, all deliberate:

* `[0, (100 - used), 100] | sort | .[1] | round` is transcribed as
  clamp-then-round. jq's `round` is half-away-from-zero, not Python's
  banker's rounding, and `sort` puts a NaN below every number (so a NaN used
  percent yields 0). Numeric strings are accepted exactly where jq calls
  `tonumber`, with jq's strictness about surrounding whitespace.
* `epoch($value)` in the CodexBar programs accepts a JSON number (`floor`) or
  an ISO-8601 UTC string (`fromdateiso8601`); every other type produces no
  output. Only the opencode variant strips a `.NNN` fraction before `Z`.
* The Pi normalisers pass `capturedAt` through raw (`// null`) and the shell
  converts it at the read boundary in `renormalise_quota_file`. Because
  `ProviderQuota.captured_at` is `Optional[int]` — and `parse_document`
  accepts nothing else — the Pi normalisers apply that same epoch conversion
  here, i.e. they model the composed `normalize | renormalise` path that the
  shell actually feeds into effective quota.
* jq arithmetic on the Pi paths can yield a fractional percent or reset
  (`100 - 52.5`). The typed document has no place for it, so those fields are
  rounded with jq's own `round` (half away from zero); every integral value —
  i.e. everything the real producers emit — is preserved exactly.
* `normalize_pi_codex` computes `100 - used` without the CodexBar clamp:
  that is what the jq does.
* Two jq paths produce no output because `empty` is not an error, and both
  are rejections here: `(window_for($windows; 43200)) as $monthly` kills the
  whole CodexBar opencode program when the monthly window is missing, and
  `epoch_ts`'s `capture(...)` yields empty (not a caught error) for an
  offset-shaped string whose strict pattern does not match.
* The `source` strings are exact, including the full-width parentheses and
  the ASCII middle dot (`·`).
"""
from __future__ import annotations

import calendar
import json
import math
import os
import re
import tempfile
import time
from typing import Any, Dict, List, NoReturn, Optional, Tuple

from .models import (
    ProviderQuota,
    QuotaNormalizationError,
    QuotaWindow,
    parse_document,
)

__all__ = [
    "CODEXBAR_CACHED_SOURCE",
    "CODEXBAR_SOURCE_PREFIX",
    "PI_SNAPSHOT_SOURCE",
    "demote_to_cached",
    "normalize_codexbar_antigravity",
    "normalize_codexbar_codex",
    "normalize_codexbar_opencode",
    "normalize_pi_antigravity",
    "normalize_pi_codex",
    "normalize_pi_opencode",
    "read_document",
    "renormalize_document",
    "write_document",
]

# Source strings, byte-for-byte as the shell writes them. `PI_SNAPSHOT_SOURCE`
# is what the Pi normalisers stamp; `CODEXBAR_CACHED_SOURCE` is what the
# CodexBar cache tier stamps (`save_codexbar_cache` / `use_codexbar_cached_*`).
PI_SNAPSHOT_SOURCE = "Pi 快照（可能不是最新）"
CODEXBAR_SOURCE_PREFIX = "CodexBar · "
CODEXBAR_CACHED_SOURCE = "CodexBar · cached（可能不是最新）"

# jq `tonumber` accepts every JSON number plus plain decimal strings (C
# strtod spellings included) and rejects anything else — notably strings with
# surrounding whitespace or underscores.
_NUMBER_RE = re.compile(
    r"^[+-]?(?:(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
    r"|inf(?:inity)?|nan)\Z",
    re.IGNORECASE,
)

# jq `fromdateiso8601`: strptime's "%Y-%m-%dT%H:%M:%SZ" with the leniency of
# the C library (one-digit month/day/time fields allowed, year exactly four
# digits) followed by timegm, which normalizes an over-range day-of-month.
_ISO8601_RE = re.compile(
    r"^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})T"
    r"([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2})Z\Z"
)

# `renormalise_quota_file`'s epoch_ts: an explicit numeric offset suffix
# selects the offset branch, whose capture is strict (fixed-width fields).
_OFFSET_SUFFIX_RE = re.compile(r"[+-][0-9]{2}:?[0-9]{2}\Z")
_OFFSET_CAPTURE_RE = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.[0-9]+)?([+-])([0-9]{2}):?([0-9]{2})\Z"
)

# The OpenCode Go API emits ISO-8601 with milliseconds, which
# fromdateiso8601 rejects; the opencode epoch variant strips the fraction.
_FRACTION_Z_RE = re.compile(r"\.[0-9]+Z\Z")


def _reject(detail: str) -> NoReturn:
    """Raise the single error type this module ever raises for a rejection."""
    raise QuotaNormalizationError(detail)


def _now_epoch() -> int:
    """jq `now | floor`."""
    return int(time.time())


# ---------------------------------------------------------------------------
# jq primitives
# ---------------------------------------------------------------------------


def _round_half_away(value: float) -> int:
    """jq `round`: halfway cases away from zero, not banker's rounding."""
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def _tonumber(value: Any, label: str) -> Any:
    """jq `tonumber`: identity for numbers, strtod for strings, else error."""
    kind = type(value)
    if kind is bool or value is None or kind not in (int, float, str):
        _reject("%s: %s cannot be parsed as a number" % (label, kind.__name__))
    if kind is str:
        if _NUMBER_RE.match(value) is None:
            _reject("%s: %r cannot be parsed as a number" % (label, value))
        return float(value)
    return value


def _jq_int(value: Any, label: str) -> int:
    """Fit a jq number into the typed document's int field.

    Integral values (everything the real producers emit) pass through
    unchanged; a fractional value is rounded the way jq's `round` would.
    """
    kind = type(value)
    if kind is int:
        return value
    if kind is float:
        if math.isnan(value) or math.isinf(value):
            _reject("%s: %r is not a finite number" % (label, value))
        return _round_half_away(value)
    _reject("%s: %s is not a number" % (label, kind.__name__))


def _remaining_percent(used: Any, label: str) -> int:
    """jq `[0, (100 - ($used | tonumber)), 100] | sort | .[1] | round`."""
    value = _tonumber(used, label)
    raw = 100 - value
    if isinstance(raw, float):
        if math.isnan(raw):
            # jq's sort orders NaN below every number, so .[1] is 0.
            return 0
        if raw == math.inf:
            return 100
        if raw == -math.inf:
            return 0
    return _round_half_away(min(100.0, max(0.0, raw)))


def _fromdateiso8601(text: str) -> Optional[int]:
    """jq `fromdateiso8601`: UTC ISO-8601, or None when it does not match."""
    match = _ISO8601_RE.match(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (
        int(part) for part in match.groups()
    )
    if not (
        1 <= month <= 12
        and 0 <= day <= 31
        and 0 <= hour <= 23
        and 0 <= minute <= 59
        and 0 <= second <= 60
    ):
        return None
    # strptime checks the field ranges above, then timegm normalizes an
    # over-range day-of-month or leap second instead of failing (jq accepts
    # "2026-02-30T00:00:00Z" as 2026-03-02T00:00:00Z).
    base = calendar.timegm((year, month, 1, 0, 0, 0, 0, 0, 0))
    return base + (day - 1) * 86400 + hour * 3600 + minute * 60 + second


def _epoch(value: Any, label: str) -> int:
    """jq CodexBar `epoch`: number -> floor, string -> fromdateiso8601."""
    kind = type(value)
    if kind is int:
        return value
    if kind is float:
        if math.isnan(value) or math.isinf(value):
            _reject("%s: %r has no epoch form" % (label, value))
        return math.floor(value)
    if kind is str:
        result = _fromdateiso8601(value)
        if result is None:
            _reject(
                '%s: date "%s" does not match format '
                '"%%Y-%%m-%%dT%%H:%%M:%%SZ"' % (label, value)
            )
        return result
    _reject("%s: %s has no epoch form" % (label, kind.__name__))


def _epoch_opencode(value: Any, label: str) -> int:
    """jq opencode `epoch`: like `_epoch`, but strips a `.NNN` before `Z`."""
    if type(value) is str:
        return _epoch(_FRACTION_Z_RE.sub("Z", value), label)
    return _epoch(value, label)


def _epoch_ts(value: Any) -> Optional[int]:
    """jq `renormalise_quota_file`'s epoch_ts: epoch int, or None.

    Accepts a number (floor), a `...Z` ISO string with an optional fractional
    part, and an ISO string with an explicit `+HH:MM`/`+HHMM` offset. Every
    other value — including a missing one — becomes null, never "now".

    The one path that is a rejection rather than a null is an offset-shaped
    string whose strict capture does not match (jq `capture` produces empty,
    which `try/catch` cannot turn into null).
    """
    kind = type(value)
    if kind is int:
        return value
    if kind is float:
        if math.isnan(value) or math.isinf(value):
            return None
        return math.floor(value)
    if kind is str:
        if _OFFSET_SUFFIX_RE.search(value):
            match = _OFFSET_CAPTURE_RE.match(value)
            if match is None:
                # jq `capture` yields EMPTY (not an error) when its pattern
                # does not match, and `try ... catch null` cannot catch
                # empty: `epoch_ts` produces nothing, so `{capturedAt: ...}`
                # produces nothing and the whole renormalise is dropped.
                # That is a rejection, not a null capturedAt.
                _reject(
                    "epoch_ts: offset-shaped timestamp %r matched no capture"
                    % value
                )
            base = _fromdateiso8601(match.group(1) + "Z")
            if base is None:
                return None
            sign = -1 if match.group(2) == "-" else 1
            offset = int(match.group(3)) * 3600 + int(match.group(4)) * 60
            return base - sign * offset
        return _fromdateiso8601(_FRACTION_Z_RE.sub("Z", value))
    return None


# ---------------------------------------------------------------------------
# jq field access / small helpers
# ---------------------------------------------------------------------------


def _field(container: Any, key: str, label: str) -> Any:
    """jq `.key`: null on null, the value on an object, error otherwise."""
    if container is None:
        return None
    if isinstance(container, dict):
        return container.get(key)
    _reject(
        "%s: cannot index %s with %r" % (label, type(container).__name__, key)
    )


def _or_default(value: Any, default: str, label: str) -> str:
    """jq `($value // "default")`: null/false fall back, non-strings fail."""
    if value is None or value is False:
        return default
    if isinstance(value, str):
        return value
    _reject(
        "%s: %s cannot be used as a source string"
        % (label, type(value).__name__)
    )


def _ascii_downcase(text: str) -> str:
    """jq `ascii_downcase`: ASCII A-Z only, Unicode is left alone."""
    return "".join(
        chr(ord(character) + 32) if "A" <= character <= "Z" else character
        for character in text
    )


def _string_or_empty(value: Any, label: str) -> str:
    """jq `(.value // "")` followed by a string operation."""
    if value is None or value is False:
        return ""
    if isinstance(value, str):
        return value
    _reject("%s: %s is not a string" % (label, type(value).__name__))


def _codexbar_row(raw: Any, provider: str) -> dict:
    """jq `[.[] | select(.provider == P and (.usage | type) == "object")][0]`.

    The first matching row wins; a non-object element makes `.provider`
    error, exactly as jq does, so it is a rejection rather than a skip.
    """
    if not isinstance(raw, list):
        _reject(
            "codexbar %s: expected a JSON array of usage rows, got %s"
            % (provider, type(raw).__name__)
        )
    for element in raw:
        if not isinstance(element, dict):
            _reject(
                "codexbar %s: cannot index %s with \"provider\""
                % (provider, type(element).__name__)
            )
        if element.get("provider") != provider:
            continue
        if not isinstance(element.get("usage"), dict):
            continue
        return element
    _reject("codexbar %s: no usage row for provider %r" % (provider, provider))


def _first_window(
    windows: List[Any], minutes: int, require_used: bool, label: str
) -> Optional[dict]:
    """jq `[W[] | select(.windowMinutes == N and .usedPercent != null)][0]`.

    The array comprehension scans EVERY element before `[0]` picks a winner,
    so a later element that cannot be indexed by `windowMinutes` is an error
    even when an earlier element already matched. With `require_used` false
    the used-percent test is omitted (the antigravity program selects on the
    Gemini id/title instead).
    """
    matches = []
    for window in windows:
        if not isinstance(window, dict):
            _reject(
                "%s: cannot index %s with \"windowMinutes\""
                % (label, type(window).__name__)
            )
        if window.get("windowMinutes") != minutes:
            continue
        if require_used and window.get("usedPercent") is None:
            continue
        matches.append(window)
    return matches[0] if matches else None


def _codexbar_codex_windows(usage: dict) -> List[Any]:
    """jq `[$usage.primary, $usage.secondary,
    ($usage.extraRateWindows[]?.window)] | map(select(. != null))`."""
    candidates = [usage.get("primary"), usage.get("secondary")]
    extra = usage.get("extraRateWindows")
    if isinstance(extra, (list, dict)):
        if isinstance(extra, dict):
            elements = list(extra.values())
        else:
            elements = list(extra)
        for element in elements:
            if not isinstance(element, dict):
                _reject(
                    "codexbar codex: cannot index %s with \"window\""
                    % type(element).__name__
                )
            candidates.append(element.get("window"))
    # `.[]?` swallows the "cannot iterate" error for a scalar, so those
    # contribute no windows at all.
    return [candidate for candidate in candidates if candidate is not None]


def _antigravity_windows(usage: dict) -> List[Any]:
    """jq `($usage.extraRateWindows // []) as $windows`."""
    extra = usage.get("extraRateWindows")
    if extra is None or extra is False:
        return []
    if isinstance(extra, list):
        return list(extra)
    if isinstance(extra, dict):
        return list(extra.values())
    _reject(
        "codexbar antigravity: cannot iterate over %s" % type(extra).__name__
    )


def _gemini_window(elements: List[Any], minutes: int) -> Optional[dict]:
    """jq's Gemini window select: windowMinutes plus id/title containing
    "gemini" (the title comparison is ASCII-lowercased, the id is not).

    Like `_first_window`, the array comprehension scans every element before
    `[0]` picks a winner, so mismatches later in the list are still checked
    for indexability and for a non-string id/title.
    """
    matches = []
    for element in elements:
        if not isinstance(element, dict):
            _reject(
                "codexbar antigravity: cannot index %s with \"window\""
                % type(element).__name__
            )
        window = element.get("window")
        if window is None:
            continue
        if not isinstance(window, dict):
            _reject(
                "codexbar antigravity: cannot index %s with \"windowMinutes\""
                % type(window).__name__
            )
        if window.get("windowMinutes") != minutes:
            continue
        # jq `and` short-circuits on the windowMinutes test, and `or`
        # short-circuits only on a *truthy* left side: a non-string id is an
        # error even when the title would have matched.
        identifier = _string_or_empty(element.get("id"), "gemini window id")
        if "gemini" in identifier:
            matches.append(window)
            continue
        title = _string_or_empty(element.get("title"), "gemini window title")
        if "gemini" in _ascii_downcase(title):
            matches.append(window)
    return matches[0] if matches else None


def _tonumber_window(container: Any, label: str) -> Tuple[Any, Any]:
    """jq `def window: {remainingPercent: (.remainingPercent | tonumber),
    resetAt: (.resetAt | tonumber)}` — raw numbers, no range test."""
    remaining = _tonumber(
        _field(container, "remainingPercent", label),
        label + ".remainingPercent",
    )
    reset = _tonumber(_field(container, "resetAt", label), label + ".resetAt")
    return remaining, reset


# ---------------------------------------------------------------------------
# Pi snapshot tier
# ---------------------------------------------------------------------------


def normalize_pi_codex(raw: dict) -> ProviderQuota:
    """Transcribe `normalize_pi_codex_quota` (Pi agent Codex headers)."""
    if not isinstance(raw, dict):
        _reject(
            "pi codex: expected a JSON object, got %s" % type(raw).__name__
        )
    headers = _field(raw, "headers", "pi codex")
    primary_used = _tonumber(
        _field(headers, "x-codex-primary-used-percent", "pi codex headers"),
        "pi codex x-codex-primary-used-percent",
    )
    primary_window = _tonumber(
        _field(headers, "x-codex-primary-window-minutes", "pi codex headers"),
        "pi codex x-codex-primary-window-minutes",
    )
    primary_reset = _tonumber(
        _field(headers, "x-codex-primary-reset-at", "pi codex headers"),
        "pi codex x-codex-primary-reset-at",
    )
    secondary_used = _tonumber(
        _field(headers, "x-codex-secondary-used-percent", "pi codex headers"),
        "pi codex x-codex-secondary-used-percent",
    )
    secondary_window = _tonumber(
        _field(
            headers, "x-codex-secondary-window-minutes", "pi codex headers"
        ),
        "pi codex x-codex-secondary-window-minutes",
    )
    secondary_reset = _tonumber(
        _field(headers, "x-codex-secondary-reset-at", "pi codex headers"),
        "pi codex x-codex-secondary-reset-at",
    )
    if not (
        primary_window == 300
        and secondary_window == 10080
        and 0 <= primary_used <= 100
        and 0 <= secondary_used <= 100
    ):
        _reject("pi codex: window/usage select failed")
    return ProviderQuota(
        source=PI_SNAPSHOT_SOURCE,
        fresh=False,
        cached=True,
        captured_at=_epoch_ts(_field(raw, "capturedAt", "pi codex")),
        five_hour=QuotaWindow(
            # The jq deliberately does NOT clamp here.
            remaining_percent=_jq_int(
                100 - primary_used, "pi codex fiveHour.remainingPercent"
            ),
            reset_at=_jq_int(primary_reset, "pi codex fiveHour.resetAt"),
        ),
        weekly=QuotaWindow(
            remaining_percent=_jq_int(
                100 - secondary_used, "pi codex weekly.remainingPercent"
            ),
            reset_at=_jq_int(secondary_reset, "pi codex weekly.resetAt"),
        ),
    )


def normalize_pi_antigravity(raw: dict) -> ProviderQuota:
    """Transcribe `normalize_pi_antigravity_quota` (Pi agent snapshot)."""
    if not isinstance(raw, dict):
        _reject(
            "pi antigravity: expected a JSON object, got %s"
            % type(raw).__name__
        )
    five_hour = _field(raw, "fiveHour", "pi antigravity")
    weekly = _field(raw, "weekly", "pi antigravity")
    five_remaining, five_reset = _tonumber_window(five_hour, "fiveHour")
    weekly_remaining, weekly_reset = _tonumber_window(weekly, "weekly")
    if not (0 <= five_remaining <= 100 and 0 <= weekly_remaining <= 100):
        _reject("pi antigravity: remaining-percent select failed")
    return ProviderQuota(
        source=PI_SNAPSHOT_SOURCE,
        fresh=False,
        cached=True,
        captured_at=_epoch_ts(_field(raw, "capturedAt", "pi antigravity")),
        five_hour=QuotaWindow(
            remaining_percent=_jq_int(
                five_remaining, "pi antigravity fiveHour.remainingPercent"
            ),
            reset_at=_jq_int(five_reset, "pi antigravity fiveHour.resetAt"),
        ),
        weekly=QuotaWindow(
            remaining_percent=_jq_int(
                weekly_remaining, "pi antigravity weekly.remainingPercent"
            ),
            reset_at=_jq_int(weekly_reset, "pi antigravity weekly.resetAt"),
        ),
    )


def normalize_pi_opencode(raw: dict) -> ProviderQuota:
    """Transcribe `normalize_pi_opencode_quota` (snapshot, monthly aware)."""
    if not isinstance(raw, dict):
        _reject(
            "pi opencode: expected a JSON object, got %s" % type(raw).__name__
        )
    five_hour = _field(raw, "fiveHour", "pi opencode")
    weekly = _field(raw, "weekly", "pi opencode")
    # select(($orig.fiveHour | type) == "object"
    #        and ($orig.weekly | type) == "object")
    if not isinstance(five_hour, dict) or not isinstance(weekly, dict):
        _reject("pi opencode: fiveHour/weekly object select failed")
    five_remaining, five_reset = _tonumber_window(five_hour, "fiveHour")
    weekly_remaining, weekly_reset = _tonumber_window(weekly, "weekly")
    if not (0 <= five_remaining <= 100 and 0 <= weekly_remaining <= 100):
        _reject("pi opencode: remaining-percent select failed")

    # `($orig.monthly | try window catch null)`: a malformed monthly window is
    # dropped, never an error, and its percent is not range-checked.
    monthly: Optional[QuotaWindow] = None
    try:
        monthly_remaining, monthly_reset = _tonumber_window(
            _field(raw, "monthly", "pi opencode"), "monthly"
        )
    except QuotaNormalizationError:
        monthly = None
    else:
        monthly = QuotaWindow(
            remaining_percent=_jq_int(
                monthly_remaining, "pi opencode monthly.remainingPercent"
            ),
            reset_at=_jq_int(monthly_reset, "pi opencode monthly.resetAt"),
        )

    return ProviderQuota(
        source=PI_SNAPSHOT_SOURCE,
        fresh=False,
        cached=True,
        captured_at=_epoch_ts(_field(raw, "capturedAt", "pi opencode")),
        five_hour=QuotaWindow(
            remaining_percent=_jq_int(
                five_remaining, "pi opencode fiveHour.remainingPercent"
            ),
            reset_at=_jq_int(five_reset, "pi opencode fiveHour.resetAt"),
        ),
        weekly=QuotaWindow(
            remaining_percent=_jq_int(
                weekly_remaining, "pi opencode weekly.remainingPercent"
            ),
            reset_at=_jq_int(weekly_reset, "pi opencode weekly.resetAt"),
        ),
        monthly=monthly,
    )


# ---------------------------------------------------------------------------
# CodexBar tier (live)
# ---------------------------------------------------------------------------


def normalize_codexbar_codex(raw: list) -> ProviderQuota:
    """Transcribe `normalize_codexbar_codex_quota`."""
    row = _codexbar_row(raw, "codex")
    usage = row["usage"]
    windows = _codexbar_codex_windows(usage)
    five = _first_window(windows, 300, True, "codexbar codex")
    weekly = _first_window(windows, 10080, True, "codexbar codex")
    if five is None or weekly is None:
        _reject("codexbar codex: no 300/10080 window pair")
    return ProviderQuota(
        source=CODEXBAR_SOURCE_PREFIX
        + _or_default(row.get("source"), "cli", "codexbar codex source"),
        fresh=True,
        cached=False,
        captured_at=_now_epoch(),
        five_hour=QuotaWindow(
            remaining_percent=_remaining_percent(
                five.get("usedPercent"), "codexbar codex fiveHour.usedPercent"
            ),
            reset_at=_epoch(
                five.get("resetsAt"), "codexbar codex fiveHour.resetsAt"
            ),
        ),
        weekly=QuotaWindow(
            remaining_percent=_remaining_percent(
                weekly.get("usedPercent"), "codexbar codex weekly.usedPercent"
            ),
            reset_at=_epoch(
                weekly.get("resetsAt"), "codexbar codex weekly.resetsAt"
            ),
        ),
    )


def normalize_codexbar_antigravity(raw: list) -> ProviderQuota:
    """Transcribe `normalize_codexbar_antigravity_quota`."""
    row = _codexbar_row(raw, "antigravity")
    usage = row["usage"]
    windows = _antigravity_windows(usage)
    five = _gemini_window(windows, 300)
    weekly = _gemini_window(windows, 10080)
    if five is None or weekly is None:
        _reject("codexbar antigravity: no Gemini 300/10080 window pair")
    return ProviderQuota(
        source=CODEXBAR_SOURCE_PREFIX
        + _or_default(row.get("source"), "cli", "codexbar antigravity source"),
        fresh=True,
        cached=False,
        captured_at=_now_epoch(),
        five_hour=QuotaWindow(
            remaining_percent=_remaining_percent(
                five.get("usedPercent"),
                "codexbar antigravity fiveHour.usedPercent",
            ),
            reset_at=_epoch(
                five.get("resetsAt"), "codexbar antigravity fiveHour.resetsAt"
            ),
        ),
        weekly=QuotaWindow(
            remaining_percent=_remaining_percent(
                weekly.get("usedPercent"),
                "codexbar antigravity weekly.usedPercent",
            ),
            reset_at=_epoch(
                weekly.get("resetsAt"), "codexbar antigravity weekly.resetsAt"
            ),
        ),
    )


def normalize_codexbar_opencode(raw: list) -> ProviderQuota:
    """Transcribe `normalize_codexbar_opencode_quota` (monthly aware)."""
    row = _codexbar_row(raw, "opencodego")
    usage = row["usage"]
    windows = [
        window
        for window in (
            usage.get("primary"),
            usage.get("secondary"),
            usage.get("tertiary"),
        )
        if window is not None
    ]
    five = _first_window(windows, 300, True, "codexbar opencode")
    weekly = _first_window(windows, 10080, True, "codexbar opencode")
    monthly_raw = _first_window(windows, 43200, True, "codexbar opencode")
    if five is None or weekly is None:
        _reject("codexbar opencode: no 300/10080 window pair")
    # `(window_for($windows; 43200)) as $monthly |` — an `as` binding over
    # `empty` produces nothing for the REST OF THE PROGRAM, so the jq yields
    # no output at all when the 43200-minute window is missing. The
    # `if $monthly != null` guard is therefore only reachable with a monthly
    # window in hand: absent monthly is a rejection, not an omitted key.
    if monthly_raw is None:
        _reject("codexbar opencode: no 43200-minute monthly window")

    monthly = QuotaWindow(
        remaining_percent=_remaining_percent(
            monthly_raw.get("usedPercent"),
            "codexbar opencode monthly.usedPercent",
        ),
        reset_at=_epoch_opencode(
            monthly_raw.get("resetsAt"), "codexbar opencode monthly.resetsAt"
        ),
    )

    return ProviderQuota(
        source=CODEXBAR_SOURCE_PREFIX
        + _or_default(row.get("source"), "api", "codexbar opencode source"),
        fresh=True,
        cached=False,
        captured_at=_now_epoch(),
        five_hour=QuotaWindow(
            remaining_percent=_remaining_percent(
                five.get("usedPercent"),
                "codexbar opencode fiveHour.usedPercent",
            ),
            reset_at=_epoch_opencode(
                five.get("resetsAt"), "codexbar opencode fiveHour.resetsAt"
            ),
        ),
        weekly=QuotaWindow(
            remaining_percent=_remaining_percent(
                weekly.get("usedPercent"),
                "codexbar opencode weekly.usedPercent",
            ),
            reset_at=_epoch_opencode(
                weekly.get("resetsAt"), "codexbar opencode weekly.resetsAt"
            ),
        ),
        monthly=monthly,
    )


def demote_to_cached(quota: ProviderQuota) -> ProviderQuota:
    """The CodexBar cache tier: relabel a reading and mark it stale.

    Mirrors `save_codexbar_cache` / `use_codexbar_cached_*`, which add
    `source: "CodexBar · cached（可能不是最新）"`, `fresh: false`,
    `cached: true` on top of an already renormalised document.
    """
    return ProviderQuota(
        source=CODEXBAR_CACHED_SOURCE,
        fresh=False,
        cached=True,
        captured_at=quota.captured_at,
        five_hour=quota.five_hour,
        weekly=quota.weekly,
        monthly=quota.monthly,
    )


# ---------------------------------------------------------------------------
# Document boundary
# ---------------------------------------------------------------------------


def renormalize_document(raw: dict) -> dict:
    """Transcribe `renormalise_quota_file`.

    Requires `fiveHour.resetAt` and `weekly.resetAt` to be non-null (a jq
    `select` failure is a rejection) and rewrites `capturedAt` to an epoch
    integer or null — invalid and missing values become null, never "now".
    Every other key, known or not, is passed through untouched.
    """
    if not isinstance(raw, dict):
        _reject(
            "renormalise: expected a JSON object, got %s" % type(raw).__name__
        )
    for label in ("fiveHour", "weekly"):
        window = raw.get(label)
        if not isinstance(window, dict):
            _reject("renormalise: %s is not an object; select failed" % label)
        if window.get("resetAt") is None:
            _reject("renormalise: %s.resetAt is null; select failed" % label)
    document = dict(raw)
    document["capturedAt"] = _epoch_ts(raw.get("capturedAt"))
    return document


def read_document(path) -> ProviderQuota:
    """Read a normalized quota document from `path` and validate it."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as error:
        raise QuotaNormalizationError(
            "cannot read quota document %s: %s" % (path, error)
        ) from error
    except ValueError as error:
        raise QuotaNormalizationError(
            "quota document %s is not valid JSON: %s" % (path, error)
        ) from error
    return parse_document(raw)


def write_document(path, quota: ProviderQuota) -> None:
    """Atomically write `quota` to `path`.

    A temp file in the same directory is written, flushed and fsynced, then
    `os.replace`d over the target, so a reader never observes a partial
    document. The temp file is created 0600 (as the shell's quota files are)
    and the mode survives the rename.
    """
    document = quota.as_document()
    text = json.dumps(document, ensure_ascii=False) + "\n"
    target = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(target)) or "."
    descriptor, temporary = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(target) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        directory_descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_descriptor)
    except OSError:
        pass
    finally:
        os.close(directory_descriptor)
