"""The per-tier quota-probe budgets and the worst case of ONE probe phase.

Two facts used to live apart, and the gap between them was a live defect:

* ``runtime/factory.py``'s ``quota_probe_options`` owned the eight
  ``QUOTA_SENTINEL_*`` names and defaults the collector is built with, so an
  operator could legally raise any probe budget;
* ``task_orchestrator``'s fixed ``QUOTA_PROBE_PHASE_ALLOWANCE_SECONDS = 300``
  bounded one phase with a comment claiming that an operator's per-tier
  override "is not a bound this module can know".

They disagreed the moment the escape hatch was used: with
``QUOTA_SENTINEL_CODEXBAR_TIMEOUT=4000`` the collector's real budget for one
CodexBar call became 4000s (plus its kill grace) while the derived `check`
bound stayed at 5490s, so the outer watchdog could kill a probe that was still
inside its own rules. This module is the single home for both halves: the
names, the defaults and the ``${VAR:-default}`` tolerance (which `factory` now
reads instead of re-typing), and a per-phase bound derived from the collector's
real structure.

The bound walks ``QuotaCollector.collect()`` as it is written, not as it is
hoped to be:

* ``collect()`` loops the provider roster SEQUENTIALLY, so one phase is a SUM
  over providers — unlike a model burst, whose providers run in parallel;
* each provider walks its own ladder (``quota.adapters.tier_plan``), so the
  phase pays every rung that can burn a deadline before a reading resolves:
  the native rung and the CodexBar-live rung. The cache and Pi-snapshot rungs
  are local file reads and are deliberately free here;
* every deadline the collector sets is then extended by the two costs it does
  not name itself: the process start-up that happens BEFORE a child's own clock
  (the collector documents exactly that for the native helpers) and the one
  bounded reap ``_run_bounded`` may still owe after a kill.

Import graph: the standard library, plus two stdlib-only package modules — the
roster/ladder (``quota_sentinel.quota.adapters``) and the collector
(``quota_sentinel.runtime.quota_probe``), which owns the slack constant read
below. Both are already part of the system-interpreter graph pinned by
tests/python-entrypoint-regression.py E11. ``runtime/factory.py``, the
composition root, is deliberately NOT imported here or by this module's caller
``task_orchestrator``.
"""
from __future__ import annotations

import math
import os
from typing import Dict, Mapping, NamedTuple, Optional, Sequence, Tuple

from quota_sentinel.quota.adapters import PROVIDERS, Tier, tier_plan
from quota_sentinel.runtime import quota_probe


class ProbeTimeout(NamedTuple):
    """One overridable probe budget, named once for the whole package.

    ``option`` is the key ``QuotaCollector`` and ``quota_probe_options`` use,
    ``env`` is the operator's variable, ``default`` is what the deployment runs
    with when the variable is unset and ``allow_zero`` marks the one budget
    where zero is a real value rather than a typo (a kill grace of zero means
    "do not wait after SIGTERM").
    """

    option: str
    env: str
    default: float
    allow_zero: bool = False


# The eight budgets the composition root hands to `QuotaCollector`, in the
# order `quota_probe_options` used to type them. The values and the tolerance
# are copied from that function verbatim; the consistency pin in
# tests/python-quota-probe-regression.py fails the day either side drifts.
PROBE_TIMEOUTS: Tuple[ProbeTimeout, ...] = (
    ProbeTimeout("codexbar_timeout", "QUOTA_SENTINEL_CODEXBAR_TIMEOUT", 20),
    ProbeTimeout(
        "antigravity_codexbar_timeout",
        "QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT",
        35,
    ),
    ProbeTimeout(
        "opencode_codexbar_timeout", "QUOTA_SENTINEL_OPENCODE_CODEXBAR_TIMEOUT", 20
    ),
    ProbeTimeout(
        "clinepass_codexbar_timeout", "QUOTA_SENTINEL_CLINEPASS_CODEXBAR_TIMEOUT", 20
    ),
    ProbeTimeout(
        "antigravity_native_timeout", "QUOTA_SENTINEL_ANTIGRAVITY_NATIVE_TIMEOUT", 20
    ),
    ProbeTimeout(
        "opencode_native_timeout", "QUOTA_SENTINEL_OPENCODE_NATIVE_TIMEOUT", 15
    ),
    ProbeTimeout(
        "clinepass_native_timeout", "QUOTA_SENTINEL_CLINEPASS_NATIVE_TIMEOUT", 15
    ),
    ProbeTimeout(
        "codexbar_kill_grace", "QUOTA_SENTINEL_CODEXBAR_KILL_GRACE", 10, allow_zero=True
    ),
)

PROBE_TIMEOUTS_BY_OPTION: Dict[str, ProbeTimeout] = {
    spec.option: spec for spec in PROBE_TIMEOUTS
}


def parse_seconds(raw: str, default: float, *, allow_zero: bool = False) -> float:
    """``${VAR:-default}`` with the composition root's exact tolerance.

    Unset, empty and non-numeric mean the default; so do a non-finite or
    negative value, which would otherwise turn a deadline into "no deadline"
    or a negative one. Zero is a real value only where ``allow_zero`` says so:
    for the CodexBar kill grace, where it means "do not wait after SIGTERM".
    A value that IS accepted is returned as the float it parses to.
    """
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        return default
    return value


def seconds_override(
    environment: Optional[Mapping[str, str]],
    name: str,
    default: float,
    *,
    allow_zero: bool = False,
) -> float:
    """`parse_seconds` against one environment variable.

    ``None`` means the process environment, exactly like every ``from_env``
    in this package. This is the function `factory`'s own override reader
    delegates to, so a probe budget and the direct runner's timeout cannot
    grow two different tolerances.
    """
    env = os.environ if environment is None else environment
    return parse_seconds(env.get(name, ""), default, allow_zero=allow_zero)


def quota_probe_timeouts(
    environment: Optional[Mapping[str, str]] = None,
) -> Dict[str, float]:
    """The option-key -> seconds mapping `quota_probe_options` builds from.

    This is the ONE place the eight names, defaults and the tolerance meet;
    `factory.quota_probe_options` merges this mapping into its path options and
    the bound below reads the very same values, so a probe can never be
    configured with a budget the outer `check` bound does not know.
    """
    env = os.environ if environment is None else environment
    return {
        spec.option: parse_seconds(
            env.get(spec.env, ""), spec.default, allow_zero=spec.allow_zero
        )
        for spec in PROBE_TIMEOUTS
    }


# ---------------------------------------------------------------------------
# The per-phase bound.
#
# Every constant below is read from the collector where the collector names it,
# and stated here with its source where it does not.
# ---------------------------------------------------------------------------

# The collector's own outer allowance for a native helper. Its comment is the
# source of this term: "uv + interpreter start-up happens BEFORE that clock
# starts", which is why the outer bound is not the helper's `--timeout` alone.
# Read, never re-typed: a change in the collector moves this bound in the same
# commit.
NATIVE_HELPER_SLACK_SECONDS: float = quota_probe._NATIVE_HELPER_SLACK_SECONDS

# The fixed grace in `_run_bounded(command, timeout + SLACK, 1, stdin)` — the
# native helper's own call site (`QuotaCollector._native_helper`). CodexBar
# calls pass the operator's `codexbar_kill_grace` instead.
NATIVE_HELPER_KILL_GRACE_SECONDS = 1

# `_run_bounded`'s finally block can still owe one bounded reap after a kill
# (`process.wait(timeout=1)`), and `_native_codex`'s teardown waits the same
# second: one allowance per command that can be killed.
from quota_sentinel.platform.process import CLEANUP_ALLOWANCE_SECONDS
# Five seconds startup plus the remaining stop/reap/pipe-join allowance.
REAP_ALLOWANCE_SECONDS = CLEANUP_ALLOWANCE_SECONDS - 5

# One allowance per spawned process, on top of every deadline the collector
# itself sets. For the native helpers this is exactly what the collector's
# slack comment documents; the codex app-server is a Node process and every
# CodexBar invocation is a compiled binary, so the same allowance applies to
# them. The collector does not name this number, so it is stated here as a
# documented floor rather than read.
PROCESS_STARTUP_ALLOWANCE_SECONDS = 5

# `_native_codex` sends two JSON-RPC requests (initialize, then
# account/rateLimits/read), each under its own 5s `deadline`.
CODEX_APP_SERVER_REQUESTS = 2
CODEX_APP_SERVER_REQUEST_DEADLINE_SECONDS = 5

# `QuotaCollector._api_key` reads the Keychain through `keychain.read(..., timeout=5)`
# whenever the provider's own environment variable is empty (it consults
# `os.environ`, not the injected mapping, so this bound always pays it). One
# read per keyed native helper: OpenCode Go and ClinePass. The per-phase count
# is therefore derived from the keyed providers, never typed twice.
KEYCHAIN_READ_TIMEOUT_SECONDS = 5
_KEYCHAIN_READS: Dict[str, int] = {"opencode": 1, "clinepass": 1}
KEYCHAIN_READS_PER_PHASE = sum(_KEYCHAIN_READS.values())

# The published floor. 300s is the allowance this bound replaced, and the
# derived sum must never fall below it: this change can add to the outer
# `check` bound, never weaken it. Today the structural sum is above the floor
# (see the arithmetic below), so the floor is a net and not the answer.
PROBE_PHASE_FLOOR_SECONDS = 300

# Which native rung each provider pays for. Codex's native tier is the
# app-server handshake, not a helper with a `--timeout`; the three others are
# `_native_helper` commands whose own timeout is the operator's budget. A
# provider outside this map has no native rung at all (`_native_helper`
# returns None after its cheap availability guards), so it costs nothing here.
_NATIVE_TIMEOUT_OPTIONS: Dict[str, str] = {
    "antigravity": "antigravity_native_timeout",
    "opencode": "opencode_native_timeout",
    "clinepass": "clinepass_native_timeout",
}

# Which CodexBar budget a provider's live rung spends, mirroring the chain in
# `QuotaCollector._codexbar`: only the three specialised budgets are named
# there, and everything else (codex) falls through to `codexbar_timeout`.
_CODEXBAR_TIMEOUT_OPTIONS: Dict[str, str] = {
    "antigravity": "antigravity_codexbar_timeout",
    "opencode": "opencode_codexbar_timeout",
    "clinepass": "clinepass_codexbar_timeout",
}
_CODEXBAR_DEFAULT_TIMEOUT_OPTION = "codexbar_timeout"


def codexbar_calls(provider: str) -> int:
    """How many CodexBar invocations ONE provider's live rung may make.

    `_codexbar` walks `("cli", "oauth")` for codex — two sources, two chances
    to hang until the timeout — and a single source for every other provider
    (`api` for the API-key providers, `cli` for antigravity). A source that
    answers ends the walk; the bound has to assume none does.
    """
    return 2 if provider == "codex" else 1


def codexbar_calls_per_phase(providers: Sequence[str] = PROVIDERS) -> int:
    """Every CodexBar call one phase can make, across the sequential roster."""
    return sum(codexbar_calls(provider) for provider in providers)


def _native_bound_seconds(provider: str, timeouts: Mapping[str, float]) -> float:
    """The worst case of ONE provider's native rung, in seconds."""
    if provider == "codex":
        # The app-server handshake: two request deadlines, the teardown wait
        # and the cost of starting the process at all.
        return (
            CODEX_APP_SERVER_REQUESTS * CODEX_APP_SERVER_REQUEST_DEADLINE_SECONDS
            + REAP_ALLOWANCE_SECONDS
            + PROCESS_STARTUP_ALLOWANCE_SECONDS
        )
    option = _NATIVE_TIMEOUT_OPTIONS.get(provider)
    if option is None:
        # No native helper exists for this provider; the rung resolves without
        # spawning anything.
        return 0.0
    keychain = _KEYCHAIN_READS.get(provider, 0) * (KEYCHAIN_READ_TIMEOUT_SECONDS+CLEANUP_ALLOWANCE_SECONDS)
    return (
        timeouts[option]
        + NATIVE_HELPER_SLACK_SECONDS
        + NATIVE_HELPER_KILL_GRACE_SECONDS
        + REAP_ALLOWANCE_SECONDS
        + PROCESS_STARTUP_ALLOWANCE_SECONDS
        + keychain
    )


def _codexbar_bound_seconds(provider: str, timeouts: Mapping[str, float]) -> float:
    """The worst case of ONE provider's CodexBar-live rung, in seconds.

    Each source is one `_run_bounded` call under the provider's own timeout and
    the operator's codexbar kill grace, plus the same start-up and reap
    allowances every other spawned probe pays.
    """
    option = _CODEXBAR_TIMEOUT_OPTIONS.get(provider, _CODEXBAR_DEFAULT_TIMEOUT_OPTION)
    per_call = (
        timeouts[option]
        + timeouts["codexbar_kill_grace"]
        + REAP_ALLOWANCE_SECONDS
        + PROCESS_STARTUP_ALLOWANCE_SECONDS
    )
    return codexbar_calls(provider) * per_call


def worst_case_probe_phase_seconds(
    environment: Optional[Mapping[str, str]] = None,
    providers: Sequence[str] = PROVIDERS,
) -> float:
    """A CONSERVATIVE bound for ONE quota-probe phase, in seconds.

    One phase is `QuotaCollector.collect()` once: every provider in the roster,
    sequentially, walking its whole ladder until a reading resolves. The
    provider roster and the ladders come from ``quota.adapters`` — the same
    facts the collector reads — so a provider added to the package is paid for
    by this bound without a second edit here.

    With the shipped defaults the terms are:

      codex native        2 x 5s app-server deadlines + 1s reap + 5s start  =  16s
      antigravity native  20s + 5s slack + 1s grace + 1s reap + 5s start    =  32s
      opencode native     15s + 5s + 1s + 1s + 5s start + 5s Keychain       =  32s
      clinepass native    15s + 5s + 1s + 1s + 5s start + 5s Keychain       =  32s
      codex codexbar      2 x (20s + 10s grace + 1s reap + 5s start)        =  72s
      antigravity bar     1 x (35s + 10s + 1s + 5s)                         =  51s
      opencode bar        1 x (20s + 10s + 1s + 5s)                         =  36s
      clinepass bar       1 x (20s + 10s + 1s + 5s)                         =  36s
      ---------------------------------------------------------------------------
      structural total (one phase)                                          = 307s

    and the result is that total, or `PROBE_PHASE_FLOOR_SECONDS` if an operator
    has LOWERED the budgets below the published floor. Every accepted override
    moves the total by at least the budget it adds: one native timeout is paid
    once per phase, a CodexBar timeout once per source that provider walks, and
    the kill grace once per CodexBar call in the phase.
    """
    timeouts = quota_probe_timeouts(environment)
    total = 0.0
    for provider in providers:
        for tier in tier_plan(provider):
            if tier is Tier.NATIVE:
                total += _native_bound_seconds(provider, timeouts)
            elif tier is Tier.CODEXBAR_LIVE:
                total += _codexbar_bound_seconds(provider, timeouts)
            # CODEXBAR_CACHE and PI_SNAPSHOT are local file reads with no
            # deadline to spend; they are deliberately free in this bound.
    return max(float(PROBE_PHASE_FLOOR_SECONDS), total)


__all__ = [
    "CODEX_APP_SERVER_REQUESTS",
    "CODEX_APP_SERVER_REQUEST_DEADLINE_SECONDS",
    "KEYCHAIN_READS_PER_PHASE",
    "KEYCHAIN_READ_TIMEOUT_SECONDS",
    "NATIVE_HELPER_KILL_GRACE_SECONDS",
    "NATIVE_HELPER_SLACK_SECONDS",
    "PROBE_PHASE_FLOOR_SECONDS",
    "PROBE_TIMEOUTS",
    "PROBE_TIMEOUTS_BY_OPTION",
    "PROCESS_STARTUP_ALLOWANCE_SECONDS",
    "ProbeTimeout",
    "REAP_ALLOWANCE_SECONDS",
    "codexbar_calls",
    "codexbar_calls_per_phase",
    "parse_seconds",
    "quota_probe_timeouts",
    "seconds_override",
    "worst_case_probe_phase_seconds",
]
