"""Provider adapters: which tiers exist per provider and which of them may be
trusted as fresh.

The shell resolves a provider's quota through a fixed fallback ladder
(native probe, CodexBar live, CodexBar cache, Pi/agent snapshot). This module
is the Python home of that ladder, the human-facing card metadata and the
delivery transport, so the scheduler side never hard-codes a provider list of
its own.

``transport`` is a capability, not a preference: Codex and Antigravity are only
reachable through the Pi agent (OAuth plus a local service), while OpenCode Go
and ClinePass are plain OpenAI-compatible endpoints whose whole credential is
an API key, so the port calls them directly instead of starting Pi.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Tuple

__all__ = [
    "ADAPTERS",
    "FRESH_TIERS",
    "PROVIDERS",
    "TIER_LADDER",
    "QuotaAdapter",
    "Tier",
    "adapter_for",
    "tier_plan",
]


class Tier(str, Enum):
    """One rung of the quota fallback ladder."""

    NATIVE = "native"
    CODEXBAR_LIVE = "codexbar-live"
    CODEXBAR_CACHE = "codexbar-cache"
    PI_SNAPSHOT = "pi-snapshot"


# The single ladder used by every provider, in resolution order.
TIER_LADDER: Tuple[Tier, ...] = (
    Tier.NATIVE,
    Tier.CODEXBAR_LIVE,
    Tier.CODEXBAR_CACHE,
    Tier.PI_SNAPSHOT,
)

# Tiers that may be treated as a fresh reading: the live probes. A cached
# document and a Pi snapshot are both "not necessarily current" (`fresh`
# false in the document), so they only feed the fallback path.
FRESH_TIERS = frozenset({Tier.NATIVE, Tier.CODEXBAR_LIVE})


@dataclass(frozen=True)
class QuotaAdapter:
    """Card metadata and the fallback ladder for one provider."""

    provider: str
    title: str
    monthly_display_only: bool
    tiers: Tuple[Tier, ...]
    # "pi" starts the Pi agent; "direct" calls the vendor's own API.
    transport: str = "pi"

    def tier_is_fresh(self, tier: Tier) -> bool:
        return tier in FRESH_TIERS


ADAPTERS: Dict[str, QuotaAdapter] = {
    "codex": QuotaAdapter(
        provider="codex",
        title="GPT-5.6 Luna",
        monthly_display_only=False,
        tiers=TIER_LADDER,
    ),
    "antigravity": QuotaAdapter(
        provider="antigravity",
        title="Gemini 3.7 Flash · Low",
        monthly_display_only=False,
        tiers=TIER_LADDER,
    ),
    "opencode": QuotaAdapter(
        provider="opencode",
        title="DeepSeek V4.1 Flash · OpenCode Go",
        monthly_display_only=True,
        tiers=TIER_LADDER,
        transport="direct",
    ),
    "clinepass": QuotaAdapter(
        provider="clinepass",
        title="DeepSeek V4.1 Flash · ClinePass",
        monthly_display_only=True,
        # No native helper yet: CodexBar's bundled clinepass provider is the
        # fresh source, and the direct transport writes the snapshot tier.
        tiers=(Tier.CODEXBAR_LIVE, Tier.CODEXBAR_CACHE, Tier.PI_SNAPSHOT),
        transport="direct",
    ),
}

PROVIDERS: Tuple[str, ...] = ("codex", "antigravity", "opencode", "clinepass")


def adapter_for(provider: str) -> QuotaAdapter:
    """Return the adapter for `provider`; KeyError when it is unknown."""
    return ADAPTERS[provider]


def tier_plan(provider: str) -> Tuple[Tier, ...]:
    """Ordered fallback ladder for `provider`; KeyError when unknown."""
    return adapter_for(provider).tiers
