"""Quota normalization: the Python transcription of the shell's jq programs.

`adapters` is the provider roster and fallback ladder, `models` is the typed
normalized document (and its strict parser), and `normalize` holds the six
`normalize_*` transcriptions plus the `renormalise_quota_file` epoch pass and
the atomic document read/write boundary.
"""
from __future__ import annotations

from .adapters import (
    ADAPTERS,
    FRESH_TIERS,
    PROVIDERS,
    TIER_LADDER,
    QuotaAdapter,
    Tier,
    adapter_for,
    tier_plan,
)
from .models import (
    ProviderQuota,
    QuotaNormalizationError,
    QuotaWindow,
    parse_document,
)
from .normalize import (
    CODEXBAR_CACHED_SOURCE,
    CODEXBAR_SOURCE_PREFIX,
    PI_SNAPSHOT_SOURCE,
    demote_to_cached,
    normalize_codexbar_antigravity,
    normalize_codexbar_codex,
    normalize_codexbar_opencode,
    normalize_pi_antigravity,
    normalize_pi_codex,
    normalize_pi_opencode,
    read_document,
    renormalize_document,
    write_document,
)

__all__ = [
    "ADAPTERS",
    "CODEXBAR_CACHED_SOURCE",
    "CODEXBAR_SOURCE_PREFIX",
    "FRESH_TIERS",
    "PI_SNAPSHOT_SOURCE",
    "PROVIDERS",
    "ProviderQuota",
    "QuotaAdapter",
    "QuotaNormalizationError",
    "QuotaWindow",
    "TIER_LADDER",
    "Tier",
    "adapter_for",
    "demote_to_cached",
    "normalize_codexbar_antigravity",
    "normalize_codexbar_codex",
    "normalize_codexbar_opencode",
    "normalize_pi_antigravity",
    "normalize_pi_codex",
    "normalize_pi_opencode",
    "parse_document",
    "read_document",
    "renormalize_document",
    "tier_plan",
    "write_document",
]
