#!/usr/bin/env python3
"""Behavioural regression suite for quota_sentinel.quota.

The Python quota layer is a literal transcription of the jq programs the
retired zsh implementation used to carry, so this suite locks down both
directions:

* the accepted cases (the three real fixtures plus synthetic CodexBar
  payloads) produce exactly the documented shape, with `monthly` present only
  where the vendor has a monthly cap;
* every rejection the jq performs — empty input, wrong type, a missing
  window, an out-of-range percentage, a non-numeric timestamp, the wrong
  `windowMinutes`, a non-Gemini antigravity window, a malformed monthly —
  raises QuotaNormalizationError instead of yielding a partial quota;
* `parse_document` accepts exactly `as_document()` and nothing else;
* `renormalize_document` performs the epoch conversion at the read boundary
  (numeric, `...Z`, fractional `...123Z`, explicit offsets) and refuses a
  document whose fiveHour/weekly reset is null.
* the two live native-tier helpers (`antigravity_usage.py`,
  `opencode_usage.py`) normalise and reject their vendor payloads exactly as
  pinned, and their bounded-subprocess / credential-path contracts hold.

`JqParityTests` diffs real jq output against `as_document()` for the golden
payloads, using the programs embedded below as the frozen reference. It is
skipped when jq is unavailable, but when it runs it is the arbiter of
"exactly what the jq produced".
"""
from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import antigravity_usage as antigravity_quota
import clinepass_usage as clinepass_quota
import opencode_usage as opencode_quota

from quota_sentinel.quota import (
    ADAPTERS,
    CODEXBAR_CACHED_SOURCE,
    PI_SNAPSHOT_SOURCE,
    PROVIDERS,
    QuotaAdapter,
    QuotaNormalizationError,
    QuotaWindow,
    TIER_LADDER,
    Tier,
    adapter_for,
    demote_to_cached,
    normalize_codexbar_antigravity,
    normalize_codexbar_codex,
    normalize_codexbar_opencode,
    normalize_pi_antigravity,
    normalize_pi_codex,
    normalize_pi_opencode,
    parse_document,
    read_document,
    renormalize_document,
    tier_plan,
    write_document,
)
from quota_sentinel.quota import normalize as normalize_module

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# Frozen `now` for the CodexBar live programs, which stamp `capturedAt`.
NOW = 1788000000

PI_CODEX_FIXTURE = json.loads((TESTS_DIR / "quota-fixture.json").read_text())
PI_ANTIGRAVITY_FIXTURE = json.loads(
    (TESTS_DIR / "antigravity-quota-fixture.json").read_text()
)
PI_OPENCODE_FIXTURE = json.loads(
    (TESTS_DIR / "opencode-quota-fixture.json").read_text()
)

# Byte-for-byte golden documents (json.dumps(sort_keys=True) of a full case).
PI_ANTIGRAVITY_DOCUMENT = (
    '{"cached": true, "capturedAt": 1788002498, "fiveHour": '
    '{"remainingPercent": 100, "resetAt": 1788020474}, "fresh": false, '
    '"source": "Pi 快照（可能不是最新）", "weekly": '
    '{"remainingPercent": 91, "resetAt": 1788424918}}'
)
PI_ANTIGRAVITY_DOCUMENT_ESCAPED = (
    '{"cached": true, "capturedAt": 1788002498, "fiveHour": '
    '{"remainingPercent": 100, "resetAt": 1788020474}, "fresh": false, '
    '"source": "Pi \\u5feb\\u7167\\uff08\\u53ef\\u80fd\\u4e0d\\u662f\\u6700'
    '\\u65b0\\uff09", "weekly": {"remainingPercent": 91, "resetAt": 1788424918}}'
)
PI_OPENCODE_DOCUMENT = (
    '{"cached": true, "capturedAt": 1788002498, "fiveHour": '
    '{"remainingPercent": 88, "resetAt": 1788020474}, "fresh": false, '
    '"monthly": {"remainingPercent": 98, "resetAt": 1791026917}, '
    '"source": "Pi 快照（可能不是最新）", "weekly": '
    '{"remainingPercent": 95, "resetAt": 1788424918}}'
)


def frozen_now(function, payload):
    """Call a CodexBar normaliser with `now | floor` pinned to NOW."""
    with mock.patch.object(normalize_module, "_now_epoch", return_value=NOW):
        return function(payload)


def codex_row(primary, secondary, extra=None, source="cli"):
    usage = {
        "primary": {
            "windowMinutes": 300,
            "usedPercent": primary,
            "resetsAt": 1788012274,
        },
        "secondary": {
            "windowMinutes": 10080,
            "usedPercent": secondary,
            "resetsAt": 1788480117,
        },
    }
    if extra is not None:
        usage["extraRateWindows"] = extra
    row = {"provider": "codex", "usage": usage}
    if source is not None:
        row["source"] = source
    return [row]


CODEXBAR_CODEX = codex_row(52.5, 35, source="cli")
CODEXBAR_ANTIGRAVITY = [
    {
        "provider": "antigravity",
        "source": "agy",
        "usage": {
            "extraRateWindows": [
                {
                    "id": "gemini-5h",
                    "window": {
                        "windowMinutes": 300,
                        "usedPercent": 0,
                        "resetsAt": 1788020474,
                    },
                },
                {
                    "title": "Gemini Weekly",
                    "window": {
                        "windowMinutes": 10080,
                        "usedPercent": 9,
                        "resetsAt": 1788424918,
                    },
                },
                {
                    "id": "Gemini Monthly",
                    "window": {
                        "windowMinutes": 43200,
                        "usedPercent": 1,
                        "resetsAt": 1791026917,
                    },
                },
            ]
        },
    }
]
CODEXBAR_OPENCODE = [
    {
        "provider": "opencodego",
        "source": "api",
        "usage": {
            "primary": {
                "windowMinutes": 300,
                "usedPercent": 12.5,
                "resetsAt": "2026-08-29T11:21:38.870Z",
            },
            "secondary": {
                "windowMinutes": 10080,
                "usedPercent": 5,
                "resetsAt": 1788424918,
            },
            "tertiary": {
                "windowMinutes": 43200,
                "usedPercent": 2,
                "resetsAt": 1791026917,
            },
        },
    }
]


class AdapterTests(unittest.TestCase):
    def test_roster_order_is_codex_antigravity_opencode_clinepass(self):
        self.assertEqual(
            PROVIDERS, ("codex", "antigravity", "opencode", "clinepass")
        )
        self.assertEqual(tuple(ADAPTERS), PROVIDERS)
        self.assertEqual(
            tuple(adapter.provider for adapter in ADAPTERS.values()), PROVIDERS
        )

    def test_every_provider_shares_the_four_tier_ladder_in_order(self):
        self.assertEqual(
            TIER_LADDER,
            (
                Tier.NATIVE,
                Tier.CODEXBAR_LIVE,
                Tier.CODEXBAR_CACHE,
                Tier.PI_SNAPSHOT,
            ),
        )
        self.assertEqual(
            [tier.value for tier in TIER_LADDER],
            ["native", "codexbar-live", "codexbar-cache", "pi-snapshot"],
        )
        for provider in PROVIDERS:
            self.assertEqual(tier_plan(provider), TIER_LADDER)
            self.assertEqual(adapter_for(provider).tiers, TIER_LADDER)

    def test_transport_names_who_delivers_the_task(self):
        # Pi is the shipped path for codex because it costs ~54 tokens per
        # ignition against the official CLI's ~1,687; the official client is
        # wired as its fallback (and as an opt-in primary via
        # QUOTA_SENTINEL_TRANSPORT=codex=codex).
        self.assertEqual(adapter_for("codex").transport, "pi")
        # Antigravity's priority runs the other way: the official CLI with its
        # minimal agent costs ~564 input tokens per ignition against the stock
        # agent's ~22,311, so agy is primary and Pi is its fallback (revert with
        # QUOTA_SENTINEL_TRANSPORT=antigravity=pi).
        self.assertEqual(adapter_for("antigravity").transport, "agy")
        self.assertEqual(adapter_for("opencode").transport, "direct")
        self.assertEqual(adapter_for("clinepass").transport, "direct")

    def test_tier_is_fresh_only_for_the_two_live_tiers(self):
        adapter = adapter_for("codex")
        self.assertTrue(adapter.tier_is_fresh(Tier.NATIVE))
        self.assertTrue(adapter.tier_is_fresh(Tier.CODEXBAR_LIVE))
        self.assertFalse(adapter.tier_is_fresh(Tier.CODEXBAR_CACHE))
        self.assertFalse(adapter.tier_is_fresh(Tier.PI_SNAPSHOT))
        for provider in PROVIDERS:
            self.assertTrue(adapter_for(provider).tier_is_fresh(Tier.CODEXBAR_LIVE))
            self.assertFalse(adapter_for(provider).tier_is_fresh(Tier.PI_SNAPSHOT))

    def test_monthly_display_only_marks_the_three_window_plans(self):
        self.assertTrue(ADAPTERS["opencode"].monthly_display_only)
        self.assertTrue(ADAPTERS["clinepass"].monthly_display_only)
        self.assertFalse(ADAPTERS["codex"].monthly_display_only)
        self.assertFalse(ADAPTERS["antigravity"].monthly_display_only)

    def test_card_titles_name_the_plan_and_model(self):
        self.assertEqual(ADAPTERS["codex"].title, "GPT-6 Luna")
        self.assertEqual(ADAPTERS["antigravity"].title, "Gemini 3.7 Flash · Low")
        self.assertEqual(ADAPTERS["opencode"].title, "DeepSeek V4.1 Flash · OpenCode Go")
        self.assertEqual(ADAPTERS["clinepass"].title, "DeepSeek V4.1 Flash · ClinePass")

    def test_unknown_provider_raises_key_error(self):
        for unknown in ("backend", "", "Codex"):
            with self.assertRaises(KeyError):
                adapter_for(unknown)
            with self.assertRaises(KeyError):
                tier_plan(unknown)

    def test_adapter_is_a_frozen_value(self):
        adapter = QuotaAdapter("codex", "x", False, TIER_LADDER)
        with self.assertRaises(Exception):
            adapter.provider = "antigravity"


class PiGoldenTests(unittest.TestCase):
    """The Pi snapshot tier against the three checked-in fixtures."""

    def test_codex_headers_fixture(self):
        quota = normalize_pi_codex(PI_CODEX_FIXTURE)
        self.assertEqual(quota.source, PI_SNAPSHOT_SOURCE)
        self.assertFalse(quota.fresh)
        self.assertTrue(quota.cached)
        self.assertIsNone(quota.captured_at)
        # 100 - 52 and 100 - 35; reset epochs are the header strings as numbers.
        self.assertEqual(quota.five_hour, QuotaWindow(48, 1788012274))
        self.assertEqual(quota.weekly, QuotaWindow(65, 1788480117))
        self.assertIsNone(quota.monthly)
        self.assertEqual(set(quota.as_document()), {
            "source", "fresh", "cached", "capturedAt", "fiveHour", "weekly",
        })

    def test_codex_document_is_byte_for_byte_the_golden_json(self):
        document = normalize_pi_codex(PI_CODEX_FIXTURE).as_document()
        self.assertEqual(
            json.dumps(document, sort_keys=True, ensure_ascii=False),
            '{"cached": true, "capturedAt": null, "fiveHour": '
            '{"remainingPercent": 48, "resetAt": 1788012274}, "fresh": false, '
            '"source": "Pi 快照（可能不是最新）", "weekly": '
            '{"remainingPercent": 65, "resetAt": 1788480117}}',
        )

    def test_antigravity_fixture_is_byte_for_byte_the_golden_json(self):
        quota = normalize_pi_antigravity(PI_ANTIGRAVITY_FIXTURE)
        self.assertEqual(quota.five_hour, QuotaWindow(100, 1788020474))
        self.assertEqual(quota.weekly, QuotaWindow(91, 1788424918))
        # "2026-08-29T11:21:38.870Z" -> the read-boundary epoch conversion.
        self.assertEqual(quota.captured_at, 1788002498)
        self.assertIsNone(quota.monthly)
        document = quota.as_document()
        self.assertEqual(
            json.dumps(document, sort_keys=True, ensure_ascii=False),
            PI_ANTIGRAVITY_DOCUMENT,
        )
        self.assertEqual(
            json.dumps(document, sort_keys=True),
            PI_ANTIGRAVITY_DOCUMENT_ESCAPED,
        )
        self.assertNotIn("monthly", document)

    def test_opencode_fixture_carries_the_monthly_window(self):
        quota = normalize_pi_opencode(PI_OPENCODE_FIXTURE)
        self.assertEqual(quota.five_hour, QuotaWindow(88, 1788020474))
        self.assertEqual(quota.weekly, QuotaWindow(95, 1788424918))
        self.assertEqual(quota.monthly, QuotaWindow(98, 1791026917))
        self.assertEqual(quota.captured_at, 1788002498)
        document = quota.as_document()
        self.assertEqual(
            json.dumps(document, sort_keys=True, ensure_ascii=False),
            PI_OPENCODE_DOCUMENT,
        )
        self.assertEqual(
            set(document),
            {"source", "fresh", "cached", "capturedAt", "fiveHour", "weekly",
             "monthly"},
        )

    def test_opencode_without_monthly_omits_the_key_entirely(self):
        raw = dict(PI_OPENCODE_FIXTURE)
        raw.pop("monthly")
        quota = normalize_pi_opencode(raw)
        self.assertIsNone(quota.monthly)
        self.assertNotIn("monthly", quota.as_document())

    def test_opencode_malformed_monthly_is_dropped_not_fatal(self):
        # jq: ($orig.monthly | try window catch null)
        for monthly in (
            {"remainingPercent": "x", "resetAt": 1},
            {"remainingPercent": 50},
            {"resetAt": 1},
            5,
            None,
            [],
        ):
            with self.subTest(monthly=monthly):
                quota = normalize_pi_opencode(
                    dict(PI_OPENCODE_FIXTURE, monthly=monthly)
                )
                self.assertIsNone(quota.monthly)

    def test_opencode_monthly_is_not_range_checked_by_jq(self):
        # The select() only guards fiveHour/weekly; a monthly 150 is attached
        # verbatim (and would then be rejected by parse_document).
        quota = normalize_pi_opencode(
            dict(PI_OPENCODE_FIXTURE, monthly={"remainingPercent": 150, "resetAt": 1})
        )
        self.assertEqual(quota.monthly, QuotaWindow(150, 1))
        with self.assertRaises(QuotaNormalizationError):
            parse_document(quota.as_document())

    def test_captured_at_string_forms_are_epoch_converted(self):
        cases = (
            (1788000000, 1788000000),
            ("2026-08-29T11:21:38Z", 1788002498),
            ("2026-08-29T11:21:38.870Z", 1788002498),
            ("2026-08-29T19:21:38+08:00", 1788002498),
            ("garbage", None),
            (None, None),
            (False, None),
        )
        for captured, expected in cases:
            with self.subTest(captured=captured):
                quota = normalize_pi_antigravity(
                    dict(PI_ANTIGRAVITY_FIXTURE, capturedAt=captured)
                )
                self.assertEqual(quota.captured_at, expected)


class CodexBarGoldenTests(unittest.TestCase):
    """The CodexBar live tier against synthetic payloads."""

    def test_codex_payload(self):
        quota = frozen_now(normalize_codexbar_codex, CODEXBAR_CODEX)
        self.assertEqual(quota.source, "CodexBar · cli")
        self.assertTrue(quota.fresh)
        self.assertFalse(quota.cached)
        self.assertEqual(quota.captured_at, NOW)
        # round(100 - 52.5) = round(47.5) = 48 (half away from zero).
        self.assertEqual(quota.five_hour, QuotaWindow(48, 1788012274))
        self.assertEqual(quota.weekly, QuotaWindow(65, 1788480117))
        self.assertIsNone(quota.monthly)
        self.assertNotIn("monthly", quota.as_document())

    def test_codex_extra_rate_windows_are_scanned_last(self):
        payload = codex_row(1, 1, extra=[
            {"window": {"windowMinutes": 300, "usedPercent": 10, "resetsAt": 5}}
        ])
        quota = frozen_now(normalize_codexbar_codex, payload)
        # primary comes first in the jq array, so the extra window loses.
        self.assertEqual(quota.five_hour, QuotaWindow(99, 1788012274))

        only_extra = [{
            "provider": "codex",
            "source": "cli",
            "usage": {
                "extraRateWindows": [
                    {"window": {"windowMinutes": 300, "usedPercent": 10,
                                "resetsAt": 5}},
                    {"window": {"windowMinutes": 10080, "usedPercent": 0,
                                "resetsAt": 6}},
                ]
            },
        }]
        quota = frozen_now(normalize_codexbar_codex, only_extra)
        self.assertEqual(quota.five_hour, QuotaWindow(90, 5))
        self.assertEqual(quota.weekly, QuotaWindow(100, 6))

    def test_codex_source_defaults_to_cli_and_numeric_strings_are_accepted(self):
        payload = codex_row("52", "0", source=None)
        quota = frozen_now(normalize_codexbar_codex, payload)
        self.assertEqual(quota.source, "CodexBar · cli")
        self.assertEqual(quota.five_hour.remaining_percent, 48)
        self.assertEqual(quota.weekly.remaining_percent, 100)

    def test_source_falls_back_on_false_but_rejects_non_strings(self):
        # jq `($row.source // "cli")`: only null and false fall back; a
        # number makes `"CodexBar · " + 5` fail.
        false_source = codex_row(1, 1, source=False)
        quota = frozen_now(normalize_codexbar_codex, false_source)
        self.assertEqual(quota.source, "CodexBar · cli")
        with self.assertRaises(QuotaNormalizationError):
            frozen_now(normalize_codexbar_codex, codex_row(1, 1, source=5))

    def test_empty_string_source_is_kept_verbatim(self):
        # An empty string is truthy in jq, so `//` does not replace it.
        quota = frozen_now(normalize_codexbar_codex, codex_row(1, 1, source=""))
        self.assertEqual(quota.source, "CodexBar · ")

    def test_antigravity_payload(self):
        quota = frozen_now(normalize_codexbar_antigravity, CODEXBAR_ANTIGRAVITY)
        self.assertEqual(quota.source, "CodexBar · agy")
        self.assertTrue(quota.fresh)
        self.assertEqual(quota.five_hour, QuotaWindow(100, 1788020474))
        self.assertEqual(quota.weekly, QuotaWindow(91, 1788424918))
        self.assertIsNone(quota.monthly)
        self.assertNotIn("monthly", quota.as_document())

    def test_antigravity_matches_on_id_or_ascii_downcased_title(self):
        # The id comparison is case-sensitive: "GEMINI-5H" does not match, so
        # the 300 window is missing and the payload is rejected.
        uppercase_id = [{
            "provider": "antigravity",
            "usage": {
                "extraRateWindows": [
                    {"id": "GEMINI-5H", "window": {
                        "windowMinutes": 300, "usedPercent": 1, "resetsAt": 1}},
                    {"id": "other", "title": "GEMINI Weekly", "window": {
                        "windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2}},
                ]
            },
        }]
        with self.assertRaises(QuotaNormalizationError):
            frozen_now(normalize_codexbar_antigravity, uppercase_id)

        # The title comparison is ASCII-downcased, so "GEMINI 5h" matches.
        titled = [{
            "provider": "antigravity",
            "usage": {
                "extraRateWindows": [
                    {"id": "x", "title": "GEMINI 5h", "window": {
                        "windowMinutes": 300, "usedPercent": 1, "resetsAt": 1}},
                    {"id": "x", "title": "Gemini weekly", "window": {
                        "windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2}},
                ]
            },
        }]
        quota = frozen_now(normalize_codexbar_antigravity, titled)
        self.assertEqual(quota.five_hour, QuotaWindow(99, 1))
        self.assertEqual(quota.weekly, QuotaWindow(99, 2))

    def test_opencode_payload(self):
        quota = frozen_now(normalize_codexbar_opencode, CODEXBAR_OPENCODE)
        self.assertEqual(quota.source, "CodexBar · api")
        self.assertTrue(quota.fresh)
        # round(100 - 12.5) = round(87.5) = 88, and the fractional `Z`
        # timestamp is accepted only by the opencode epoch variant.
        self.assertEqual(quota.five_hour, QuotaWindow(88, 1788002498))
        self.assertEqual(quota.weekly, QuotaWindow(95, 1788424918))
        self.assertEqual(quota.monthly, QuotaWindow(98, 1791026917))
        self.assertEqual(
            set(quota.as_document()),
            {"source", "fresh", "cached", "capturedAt", "fiveHour", "weekly",
             "monthly"},
        )

    def test_opencode_source_defaults_to_api(self):
        payload = [{
            "provider": "opencodego",
            "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
                "tertiary": {"windowMinutes": 43200, "usedPercent": 1, "resetsAt": 3},
            },
        }]
        quota = frozen_now(normalize_codexbar_opencode, payload)
        self.assertEqual(quota.source, "CodexBar · api")

    def test_remaining_clamps_and_rounds_half_away_from_zero(self):
        cases = (
            (47.5, 53),    # round(52.5)
            (52.5, 48),    # round(47.5)
            (0.5, 100),    # round(99.5)
            (99.5, 1),     # round(0.5)
            (-50, 100),    # clamped up
            (150, 0),      # clamped down
            (0, 100),
            (100, 0),
        )
        for used, expected in cases:
            with self.subTest(used=used):
                quota = frozen_now(normalize_codexbar_codex, codex_row(used, 0))
                self.assertEqual(quota.five_hour.remaining_percent, expected)

    def test_opencode_monthly_requires_the_43200_window(self):
        # jq's `(window_for($windows; 43200)) as $monthly` binds over `empty`
        # when the window is missing, which drops the whole program output --
        # an absent monthly is a rejection, never an omitted key.
        payload = [{
            "provider": "opencodego",
            "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            },
        }]
        with self.assertRaises(QuotaNormalizationError):
            frozen_now(normalize_codexbar_opencode, payload)

    def test_codexbar_documents_have_the_exact_key_set(self):
        for quota in (
            frozen_now(normalize_codexbar_codex, CODEXBAR_CODEX),
            frozen_now(normalize_codexbar_antigravity, CODEXBAR_ANTIGRAVITY),
        ):
            self.assertEqual(
                set(quota.as_document()),
                {"source", "fresh", "cached", "capturedAt", "fiveHour", "weekly"},
            )


class RejectionTests(unittest.TestCase):
    """Every normaliser rejects what the jq would have failed on."""

    def assertRejects(self, function, payload):
        with self.assertRaises(QuotaNormalizationError):
            frozen_now(function, payload)

    def test_pi_codex_rejections(self):
        headers = PI_CODEX_FIXTURE["headers"]
        self.assertRejects(normalize_pi_codex, {})
        self.assertRejects(normalize_pi_codex, [])
        self.assertRejects(normalize_pi_codex, "x")
        self.assertRejects(normalize_pi_codex, {"headers": None})
        self.assertRejects(normalize_pi_codex, {"headers": "x"})
        # Non-numeric / out-of-range usage.
        self.assertRejects(
            normalize_pi_codex,
            {"headers": dict(headers, **{"x-codex-primary-used-percent": "abc"})},
        )
        self.assertRejects(
            normalize_pi_codex,
            {"headers": dict(headers, **{"x-codex-primary-used-percent": True})},
        )
        self.assertRejects(
            normalize_pi_codex,
            {"headers": dict(headers, **{"x-codex-primary-used-percent": "101"})},
        )
        self.assertRejects(
            normalize_pi_codex,
            {"headers": dict(headers, **{"x-codex-secondary-used-percent": "-1"})},
        )
        # Non-numeric timestamp.
        self.assertRejects(
            normalize_pi_codex,
            {"headers": dict(headers, **{"x-codex-primary-reset-at": "soon"})},
        )
        # Wrong windows / a missing window header.
        self.assertRejects(
            normalize_pi_codex,
            {"headers": dict(headers, **{"x-codex-primary-window-minutes": "60"})},
        )
        self.assertRejects(
            normalize_pi_codex,
            {"headers": dict(headers, **{"x-codex-secondary-window-minutes": "300"})},
        )
        missing = dict(headers)
        missing.pop("x-codex-primary-window-minutes")
        self.assertRejects(normalize_pi_codex, {"headers": missing})

    def test_pi_antigravity_rejections(self):
        self.assertRejects(normalize_pi_antigravity, {})
        self.assertRejects(normalize_pi_antigravity, [1])
        self.assertRejects(normalize_pi_antigravity, 3)
        self.assertRejects(
            normalize_pi_antigravity, {"fiveHour": PI_ANTIGRAVITY_FIXTURE["fiveHour"]}
        )
        self.assertRejects(
            normalize_pi_antigravity, {"weekly": PI_ANTIGRAVITY_FIXTURE["weekly"]}
        )
        self.assertRejects(
            normalize_pi_antigravity,
            dict(PI_ANTIGRAVITY_FIXTURE,
                 fiveHour={"remainingPercent": 101, "resetAt": 1}),
        )
        self.assertRejects(
            normalize_pi_antigravity,
            dict(PI_ANTIGRAVITY_FIXTURE, weekly={"remainingPercent": -1, "resetAt": 1}),
        )
        self.assertRejects(
            normalize_pi_antigravity,
            dict(PI_ANTIGRAVITY_FIXTURE,
                 fiveHour={"remainingPercent": 10, "resetAt": "later"}),
        )
        self.assertRejects(
            normalize_pi_antigravity,
            dict(PI_ANTIGRAVITY_FIXTURE,
                 weekly={"remainingPercent": None, "resetAt": 1}),
        )
        # jq indexes a null fiveHour too: `.fiveHour.remainingPercent` is null
        # and `null | tonumber` fails.
        self.assertRejects(
            normalize_pi_antigravity, dict(PI_ANTIGRAVITY_FIXTURE, fiveHour=None)
        )

    def test_pi_opencode_rejections(self):
        self.assertRejects(normalize_pi_opencode, {})
        self.assertRejects(normalize_pi_opencode, None)
        self.assertRejects(normalize_pi_opencode, "x")
        self.assertRejects(
            normalize_pi_opencode, {"weekly": PI_OPENCODE_FIXTURE["weekly"]}
        )
        self.assertRejects(
            normalize_pi_opencode,
            dict(PI_OPENCODE_FIXTURE,
                 fiveHour={"remainingPercent": 100.5, "resetAt": 1}),
        )
        self.assertRejects(
            normalize_pi_opencode,
            dict(PI_OPENCODE_FIXTURE,
                 weekly={"remainingPercent": 95, "resetAt": "later"}),
        )
        # The type select comes first: a non-object window fails before the
        # monthly is even considered.
        self.assertRejects(
            normalize_pi_opencode, dict(PI_OPENCODE_FIXTURE, fiveHour="x")
        )

    def test_codexbar_codex_rejections(self):
        self.assertRejects(normalize_codexbar_codex, [])
        self.assertRejects(normalize_codexbar_codex, {})
        self.assertRejects(normalize_codexbar_codex, "x")
        self.assertRejects(normalize_codexbar_codex, [{"provider": "antigravity"}])
        self.assertRejects(normalize_codexbar_codex, [{"provider": "codex"}])
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": None}],
        )
        # Wrong windowMinutes for the 5h slot.
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 60, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            }}],
        )
        # Non-numeric percentage and timestamp.
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": "abc", "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            }}],
        )
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": "soon"},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            }}],
        )
        # A fractional `Z` timestamp is rejected by the codex/antigravity
        # epoch variant (only opencode strips the fraction).
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1,
                            "resetsAt": "2026-08-29T11:21:38.870Z"},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            }}],
        )
        # usedPercent null is not a match; a scalar element in the scan errors.
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": None, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            }}],
        )
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
                "extraRateWindows": [5],
            }}],
        )

    def test_codexbar_antigravity_rejections(self):
        self.assertRejects(normalize_codexbar_antigravity, [])
        self.assertRejects(normalize_codexbar_antigravity, None)
        self.assertRejects(normalize_codexbar_antigravity, {"provider": "antigravity"})
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": 5}}],
        )
        # `// []` only replaces null/false: a truthy scalar (even "") is
        # iterated and jq fails with "Cannot iterate over string".
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": ""}}],
        )
        # An empty object iterates to nothing, so no Gemini window is found.
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": {}}}],
        )
        # Windows that are not Gemini in either id or title.
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": [
                {"id": "claude-5h", "window": {
                    "windowMinutes": 300, "usedPercent": 1, "resetsAt": 1}},
                {"title": "Claude Weekly", "window": {
                    "windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2}},
            ]}}],
        )
        # Gemini in the id but the wrong windowMinutes.
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": [
                {"id": "gemini-5h", "window": {
                    "windowMinutes": 60, "usedPercent": 1, "resetsAt": 1}},
            ]}}],
        )
        # Gemini weekly present, Gemini 5h missing.
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": [
                {"id": "gemini-weekly", "window": {
                    "windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2}},
            ]}}],
        )
        # Non-numeric timestamp on both windows.
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": [
                {"id": "gemini", "window": {
                    "windowMinutes": 300, "usedPercent": 1, "resetsAt": "soon"}},
                {"id": "gemini", "window": {
                    "windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2}},
            ]}}],
        )
        # `null` usedPercent is not filtered by the antigravity select, so
        # `remaining(null)` = `null | tonumber` fails the whole program.
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": [
                {"id": "gemini", "window": {
                    "windowMinutes": 300, "usedPercent": None, "resetsAt": 1}},
                {"id": "gemini", "window": {
                    "windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2}},
            ]}}],
        )
        # A non-string id on a matching-minutes window is a jq containment
        # error even when the title would have matched.
        self.assertRejects(
            normalize_codexbar_antigravity,
            [{"provider": "antigravity", "usage": {"extraRateWindows": [
                {"id": 5, "title": "Gemini", "window": {
                    "windowMinutes": 300, "usedPercent": 1, "resetsAt": 1}},
                {"id": "gemini", "window": {
                    "windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2}},
            ]}}],
        )

    def test_codexbar_opencode_rejections(self):
        self.assertRejects(normalize_codexbar_opencode, [])
        self.assertRejects(normalize_codexbar_opencode, "x")
        self.assertRejects(normalize_codexbar_opencode, [{"provider": "opencodego"}])
        # Missing 5h window.
        self.assertRejects(
            normalize_codexbar_opencode,
            [{"provider": "opencodego", "usage": {
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 1},
                "tertiary": {"windowMinutes": 43200, "usedPercent": 1, "resetsAt": 1},
            }}],
        )
        # Missing monthly window: jq's `empty` binding kills the program.
        self.assertRejects(
            normalize_codexbar_opencode,
            [{"provider": "opencodego", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 1},
            }}],
        )
        # Out-of-range percentage is clamped, not rejected: jq accepts it.
        quota = frozen_now(
            normalize_codexbar_opencode,
            [{"provider": "opencodego", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 150, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": -50,
                              "resetsAt": 2},
                "tertiary": {"windowMinutes": 43200, "usedPercent": 2, "resetsAt": 3},
            }}],
        )
        self.assertEqual(quota.five_hour.remaining_percent, 0)
        self.assertEqual(quota.weekly.remaining_percent, 100)
        # Non-numeric timestamp in the monthly window.
        self.assertRejects(
            normalize_codexbar_opencode,
            [{"provider": "opencodego", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
                "tertiary": {"windowMinutes": 43200, "usedPercent": 1,
                             "resetsAt": "2026-08-29T11:21:38.870"},
            }}],
        )
        # A scalar in the window scan errors even when the earlier windows match.
        self.assertRejects(
            normalize_codexbar_opencode,
            [{"provider": "opencodego", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
                "tertiary": 5,
            }}],
        )

    def test_wrong_window_minutes_for_every_codexbar_provider(self):
        # codex: 300 must be 300.
        self.assertRejects(
            normalize_codexbar_codex,
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 301, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            }}],
        )
        # codexbar opencode: 43200 is the only monthly key accepted.
        self.assertRejects(
            normalize_codexbar_opencode,
            [{"provider": "opencodego", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
                "tertiary": {"windowMinutes": 10081, "usedPercent": 1, "resetsAt": 3},
            }}],
        )


class ParseDocumentTests(unittest.TestCase):
    def good_document(self):
        return normalize_pi_opencode(PI_OPENCODE_FIXTURE).as_document()

    def test_round_trips_as_document(self):
        quotas = (
            normalize_pi_codex(PI_CODEX_FIXTURE),
            normalize_pi_antigravity(PI_ANTIGRAVITY_FIXTURE),
            normalize_pi_opencode(PI_OPENCODE_FIXTURE),
            frozen_now(normalize_codexbar_codex, CODEXBAR_CODEX),
            frozen_now(normalize_codexbar_antigravity, CODEXBAR_ANTIGRAVITY),
            frozen_now(normalize_codexbar_opencode, CODEXBAR_OPENCODE),
        )
        for quota in quotas:
            with self.subTest(source=quota.source):
                self.assertEqual(parse_document(quota.as_document()), quota)

    def test_accepts_a_document_without_monthly(self):
        document = self.good_document()
        document.pop("monthly")
        parsed = parse_document(document)
        self.assertIsNone(parsed.monthly)
        self.assertNotIn("monthly", parsed.as_document())

    def test_rejects_structural_damage(self):
        document = self.good_document()
        cases = {}
        for key in document:
            if key == "monthly":
                continue  # optional: its absence is a valid document
            damaged = dict(document)
            damaged.pop(key)
            cases["missing " + key] = damaged
        extra = dict(document, backend="json")
        cases["extra key"] = extra
        cases["not an object"] = [document]
        cases["null document"] = None
        cases["source not a string"] = dict(document, source=5)
        cases["fresh not a bool"] = dict(document, fresh="true")
        cases["fresh an int"] = dict(document, fresh=1)
        cases["cached not a bool"] = dict(document, cached="false")
        cases["capturedAt a string"] = dict(
            document, capturedAt="2026-08-29T11:21:38Z"
        )
        cases["capturedAt an int-like bool"] = dict(document, capturedAt=True)
        cases["capturedAt a float"] = dict(document, capturedAt=1788002498.0)
        cases["monthly null"] = dict(document, monthly=None)
        cases["fiveHour not an object"] = dict(document, fiveHour=48)
        cases["fiveHour missing resetAt"] = dict(
            document, fiveHour={"remainingPercent": 48}
        )
        cases["fiveHour extra key"] = dict(
            document, fiveHour={"remainingPercent": 48, "resetAt": 1, "extra": 1}
        )
        cases["remainingPercent a float"] = dict(
            document, fiveHour={"remainingPercent": 48.0, "resetAt": 1}
        )
        cases["remainingPercent a string"] = dict(
            document, fiveHour={"remainingPercent": "48", "resetAt": 1}
        )
        cases["remainingPercent a bool"] = dict(
            document, fiveHour={"remainingPercent": True, "resetAt": 1}
        )
        cases["remainingPercent below zero"] = dict(
            document, fiveHour={"remainingPercent": -1, "resetAt": 1}
        )
        cases["remainingPercent above 100"] = dict(
            document, weekly={"remainingPercent": 101, "resetAt": 1}
        )
        cases["resetAt a string"] = dict(
            document, fiveHour={"remainingPercent": 48, "resetAt": "1"}
        )
        cases["resetAt a bool"] = dict(
            document, weekly={"remainingPercent": 48, "resetAt": True}
        )
        cases["monthly remainingPercent out of range"] = dict(
            document, monthly={"remainingPercent": 101, "resetAt": 1}
        )
        for label, damaged in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(QuotaNormalizationError):
                    parse_document(damaged)

    def test_boundary_percentages_are_valid(self):
        document = self.good_document()
        document["fiveHour"] = {"remainingPercent": 0, "resetAt": 0}
        document["weekly"] = {"remainingPercent": 100, "resetAt": -1}
        parsed = parse_document(document)
        self.assertEqual(parsed.five_hour, QuotaWindow(0, 0))
        self.assertEqual(parsed.weekly, QuotaWindow(100, -1))


class RenormalizeTests(unittest.TestCase):
    def document(self, captured="MISSING"):
        document = {
            "source": "CodexBar · cli",
            "fresh": True,
            "cached": False,
            "fiveHour": {"remainingPercent": 48, "resetAt": 1788012274},
            "weekly": {"remainingPercent": 65, "resetAt": 1788480117},
        }
        if captured != "MISSING":
            document["capturedAt"] = captured
        return document

    def test_epoch_forms(self):
        cases = (
            (1788000000, 1788000000),
            (1788000000.9, 1788000000),
            ("2026-08-29T11:21:38Z", 1788002498),
            ("2026-08-29T11:21:38.123Z", 1788002498),
            ("2026-08-29T19:21:38+08:00", 1788002498),
            ("2026-08-29T19:21:38+0800", 1788002498),
            ("2026-08-29T03:21:38-08:00", 1788002498),
            ("2026-08-29T19:21:38.500+08:00", 1788002498),
        )
        for captured, expected in cases:
            with self.subTest(captured=captured):
                self.assertEqual(
                    renormalize_document(self.document(captured))["capturedAt"],
                    expected,
                )

    def test_timegm_style_normalization_matches_jq(self):
        # C strptime accepts an out-of-range day-of-month and timegm
        # normalizes it, so jq reads "2026-02-30" as 2026-03-02.
        cases = (
            ("2026-02-30T00:00:00Z", 1772409600),
            ("2026-08-29T11:21:60Z", 1788002520),
            ("2026-8-9T1:2:3Z", 1786237323),
        )
        for captured, expected in cases:
            with self.subTest(captured=captured):
                self.assertEqual(
                    renormalize_document(self.document(captured))["capturedAt"],
                    expected,
                )

    def test_offset_branch_parse_failure_is_null_not_a_rejection(self):
        # A strict-capture match whose date ranges are invalid errors inside
        # `try`, so `catch null` applies.
        captured = "2026-13-01T00:00:00+08:00"
        self.assertIsNone(
            renormalize_document(self.document(captured))["capturedAt"]
        )

    def test_invalid_and_missing_values_become_null_never_now(self):
        for captured in (
            "not-a-date", "", None, False, [1, 2], {"a": 1},
            "2026-13-01T00:00:00Z",
        ):
            with self.subTest(captured=captured):
                result = renormalize_document(self.document(captured))
                self.assertIsNone(result["capturedAt"])
                self.assertIn("capturedAt", result)

    def test_missing_captured_at_is_added_as_null(self):
        result = renormalize_document(self.document())
        self.assertIsNone(result["capturedAt"])

    def test_offset_shaped_string_that_misses_the_strict_capture_is_rejected(self):
        # jq's `capture` produces empty (not an error) and `try/catch null`
        # cannot catch empty, so the whole renormalise yields no output.
        with self.assertRaises(QuotaNormalizationError):
            renormalize_document(self.document("2026-8-9T1:2:3+08:00"))

    def test_other_keys_are_passed_through_untouched(self):
        document = self.document("2026-08-29T11:21:38Z")
        document["originalSource"] = "CodexBar · cli"
        document["unknown"] = {"nested": [1, 2]}
        result = renormalize_document(document)
        self.assertEqual(result["originalSource"], "CodexBar · cli")
        self.assertEqual(result["unknown"], {"nested": [1, 2]})
        self.assertEqual(result["fiveHour"], document["fiveHour"])
        self.assertEqual(result["capturedAt"], 1788002498)
        # A shallow copy: the input document is not mutated.
        self.assertEqual(document["capturedAt"], "2026-08-29T11:21:38Z")

    def test_rejects_null_or_missing_reset_windows(self):
        cases = (
            {"fiveHour": {"remainingPercent": 1, "resetAt": None},
             "weekly": {"remainingPercent": 1, "resetAt": 2}},
            {"fiveHour": {"remainingPercent": 1, "resetAt": 1},
             "weekly": {"remainingPercent": 1, "resetAt": None}},
            {"weekly": {"remainingPercent": 1, "resetAt": 2}},
            {"fiveHour": {"remainingPercent": 1, "resetAt": 1}},
            {"fiveHour": "x", "weekly": {"remainingPercent": 1, "resetAt": 2}},
            {"fiveHour": [1], "weekly": {"remainingPercent": 1, "resetAt": 2}},
            [1, 2],
            "x",
            None,
        )
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(QuotaNormalizationError):
                    renormalize_document(document)

    def test_false_reset_is_not_null_so_it_survives(self):
        document = self.document(1)
        document["fiveHour"] = {"remainingPercent": 1, "resetAt": False}
        result = renormalize_document(document)
        self.assertEqual(result["fiveHour"]["resetAt"], False)


class DocumentIOTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.path = self.directory / "codex-quota.json"
        self.quota = normalize_pi_opencode(PI_OPENCODE_FIXTURE)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_round_trips(self):
        write_document(self.path, self.quota)
        self.assertEqual(read_document(self.path), self.quota)

    def test_written_file_is_json_with_a_trailing_newline(self):
        write_document(self.path, self.quota)
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), self.quota.as_document())
        # The Chinese source stays readable, as jq writes it.
        self.assertIn(PI_SNAPSHOT_SOURCE, text)

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        write_document(self.path, self.quota)
        first = self.path.read_bytes()
        replacement = frozen_now(normalize_codexbar_codex, CODEXBAR_CODEX)
        write_document(self.path, replacement)
        self.assertEqual(read_document(self.path), replacement)
        self.assertNotEqual(self.path.read_bytes(), first)
        self.assertEqual(
            sorted(entry.name for entry in self.directory.iterdir()),
            [self.path.name],
        )

    def test_written_file_is_not_group_or_world_readable(self):
        write_document(self.path, self.quota)
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0)

    def test_read_rejects_missing_and_corrupt_files(self):
        with self.assertRaises(QuotaNormalizationError):
            read_document(self.path)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(QuotaNormalizationError):
            read_document(self.path)
        self.path.write_text('{"source": "x"}', encoding="utf-8")
        with self.assertRaises(QuotaNormalizationError):
            read_document(self.path)

    def test_read_accepts_a_str_path(self):
        write_document(str(self.path), self.quota)
        self.assertEqual(read_document(str(self.path)), self.quota)


class CachedTierTests(unittest.TestCase):
    def test_demote_to_cached_relabels_and_keeps_windows(self):
        live = frozen_now(normalize_codexbar_opencode, CODEXBAR_OPENCODE)
        cached = demote_to_cached(live)
        self.assertEqual(cached.source, CODEXBAR_CACHED_SOURCE)
        self.assertEqual(cached.source, "CodexBar · cached（可能不是最新）")
        self.assertFalse(cached.fresh)
        self.assertTrue(cached.cached)
        self.assertEqual(cached.captured_at, live.captured_at)
        self.assertEqual(cached.five_hour, live.five_hour)
        self.assertEqual(cached.weekly, live.weekly)
        self.assertEqual(cached.monthly, live.monthly)
        self.assertEqual(parse_document(cached.as_document()), cached)

    def test_cached_tier_is_never_fresh(self):
        for provider in PROVIDERS:
            self.assertFalse(
                adapter_for(provider).tier_is_fresh(Tier.CODEXBAR_CACHE)
            )


# The seven jq programs this package transcribes, copied byte-for-byte from
# the retired zsh implementation at the branch point (the `normalize_*_quota`
# functions and `renormalise_quota_file`). They are embedded as the frozen
# parity fixture: `JqParityTests` runs them under the real jq binary and diffs
# the output against `as_document()`, so the transcription stays pinned even
# though the shell no longer exists to be re-extracted from.
JQ_PROGRAMS = {
    "normalize_pi_codex": r"""
    .headers as $h |
    ($h["x-codex-primary-used-percent"] | tonumber) as $primary_used |
    ($h["x-codex-primary-window-minutes"] | tonumber) as $primary_window |
    ($h["x-codex-primary-reset-at"] | tonumber) as $primary_reset |
    ($h["x-codex-secondary-used-percent"] | tonumber) as $secondary_used |
    ($h["x-codex-secondary-window-minutes"] | tonumber) as $secondary_window |
    ($h["x-codex-secondary-reset-at"] | tonumber) as $secondary_reset |
    select($primary_window == 300 and $secondary_window == 10080 and
      $primary_used >= 0 and $primary_used <= 100 and
      $secondary_used >= 0 and $secondary_used <= 100) |
    {
      source: "Pi 快照（可能不是最新）",
      fresh: false,
      cached: true,
      capturedAt: (.capturedAt // null),
      fiveHour: {remainingPercent: (100 - $primary_used), resetAt: $primary_reset},
      weekly: {remainingPercent: (100 - $secondary_used), resetAt: $secondary_reset}
    }
  """,
    "normalize_pi_antigravity": r"""
    (.fiveHour.remainingPercent | tonumber) as $five_remaining |
    (.fiveHour.resetAt | tonumber) as $five_reset |
    (.weekly.remainingPercent | tonumber) as $weekly_remaining |
    (.weekly.resetAt | tonumber) as $weekly_reset |
    select($five_remaining >= 0 and $five_remaining <= 100 and
      $weekly_remaining >= 0 and $weekly_remaining <= 100) |
    {
      source: "Pi 快照（可能不是最新）",
      fresh: false,
      cached: true,
      capturedAt: (.capturedAt // null),
      fiveHour: {remainingPercent: $five_remaining, resetAt: $five_reset},
      weekly: {remainingPercent: $weekly_remaining, resetAt: $weekly_reset}
    }
  """,
    "normalize_pi_opencode": r"""
    def window:
      {remainingPercent: (.remainingPercent | tonumber), resetAt: (.resetAt | tonumber)};
    . as $orig |
    select(($orig.fiveHour | type) == "object" and ($orig.weekly | type) == "object") |
    ($orig.fiveHour | window) as $five |
    ($orig.weekly | window) as $weekly |
    select($five.remainingPercent >= 0 and $five.remainingPercent <= 100 and
      $weekly.remainingPercent >= 0 and $weekly.remainingPercent <= 100) |
    {
      source: "Pi 快照（可能不是最新）",
      fresh: false,
      cached: true,
      capturedAt: ($orig.capturedAt // null),
      fiveHour: $five,
      weekly: $weekly
    } + (($orig.monthly | try window catch null) as $monthly |
         if $monthly == null then {} else {monthly: $monthly} end)
  """,
    "normalize_codexbar_codex": r"""
    def epoch($value):
      if ($value | type) == "number" then ($value | floor)
      elif ($value | type) == "string" then ($value | fromdateiso8601)
      else empty end;
    def remaining($used): ([0, (100 - ($used | tonumber)), 100] | sort | .[1] | round);
    ([.[] | select(.provider == "codex" and (.usage | type) == "object")][0] // empty) as $row |
    select($row != null) |
    $row.usage as $usage |
    ([$usage.primary, $usage.secondary, ($usage.extraRateWindows[]?.window)] | map(select(. != null))) as $windows |
    ([$windows[] | select(.windowMinutes == 300 and .usedPercent != null)][0] // empty) as $five |
    ([$windows[] | select(.windowMinutes == 10080 and .usedPercent != null)][0] // empty) as $weekly |
    select($five != null and $weekly != null) |
    {
      source: ("CodexBar · " + ($row.source // "cli")),
      fresh: true,
      capturedAt: (now | floor),
      fiveHour: {remainingPercent: remaining($five.usedPercent), resetAt: epoch($five.resetsAt)},
      weekly: {remainingPercent: remaining($weekly.usedPercent), resetAt: epoch($weekly.resetsAt)}
    }
  """,
    "normalize_codexbar_antigravity": r"""
    def epoch($value):
      if ($value | type) == "number" then ($value | floor)
      elif ($value | type) == "string" then ($value | fromdateiso8601)
      else empty end;
    def remaining($used): ([0, (100 - ($used | tonumber)), 100] | sort | .[1] | round);
    ([.[] | select(.provider == "antigravity" and (.usage | type) == "object")][0] // empty) as $row |
    select($row != null) |
    $row.usage as $usage |
    ($usage.extraRateWindows // []) as $windows |
    ([$windows[] | select(.window.windowMinutes == 300 and (((.id // "") | contains("gemini")) or ((.title // "") | ascii_downcase | contains("gemini"))))][0].window // empty) as $five |
    ([$windows[] | select(.window.windowMinutes == 10080 and (((.id // "") | contains("gemini")) or ((.title // "") | ascii_downcase | contains("gemini"))))][0].window // empty) as $weekly |
    select($five != null and $weekly != null) |
    {
      source: ("CodexBar · " + ($row.source // "cli")),
      fresh: true,
      capturedAt: (now | floor),
      fiveHour: {remainingPercent: remaining($five.usedPercent), resetAt: epoch($five.resetsAt)},
      weekly: {remainingPercent: remaining($weekly.usedPercent), resetAt: epoch($weekly.resetsAt)}
    }
  """,
    "normalize_codexbar_opencode": r"""
    def epoch($value):
      if ($value | type) == "number" then ($value | floor)
      elif ($value | type) == "string" then
        # The OpenCode Go API emits ISO-8601 with milliseconds, which
        # fromdateiso8601 rejects; strip the fraction before parsing.
        (if ($value | test("\\.[0-9]+Z$")) then ($value | sub("\\.[0-9]+Z$"; "Z")) else $value end
          | fromdateiso8601)
      else empty end;
    def remaining($used): ([0, (100 - ($used | tonumber)), 100] | sort | .[1] | round);
    def window_for($windows; $minutes):
      ([$windows[] | select(.windowMinutes == $minutes and .usedPercent != null)][0] // empty);
    def window_of($window):
      {remainingPercent: remaining($window.usedPercent), resetAt: epoch($window.resetsAt)};
    ([.[] | select(.provider == "opencodego" and (.usage | type) == "object")][0] // empty) as $row |
    select($row != null) |
    $row.usage as $usage |
    ([$usage.primary, $usage.secondary, $usage.tertiary] | map(select(. != null))) as $windows |
    (window_for($windows; 300)) as $five |
    (window_for($windows; 10080)) as $weekly |
    (window_for($windows; 43200)) as $monthly |
    select($five != null and $weekly != null) |
    {
      source: ("CodexBar · " + ($row.source // "api")),
      fresh: true,
      capturedAt: (now | floor),
      fiveHour: window_of($five),
      weekly: window_of($weekly)
    } + (if $monthly != null then {monthly: window_of($monthly)} else {} end)
  """,
    "renormalise_quota_file": r"""
    def epoch_ts:
      if type == "number" then floor
      elif type == "string" then
        (try
          (if test("[+-][0-9]{2}:?[0-9]{2}$") then
            capture("^(?<d>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\\.[0-9]+)?(?<sign>[+-])(?<hh>[0-9]{2}):?(?<mm>[0-9]{2})$") as $m |
            (($m.d + "Z") | fromdateiso8601) -
              (if $m.sign == "-" then -1 else 1 end) * (($m.hh | tonumber) * 3600 + (($m.mm | tonumber) * 60))
          else (sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) end)
         catch null)
      else null end;
    select(.fiveHour.resetAt != null and .weekly.resetAt != null) |
    . + {capturedAt: ((.capturedAt // null) | epoch_ts)}
  """,
}


def find_jq():
    found = shutil.which("jq")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/jq", "/usr/local/bin/jq", "/usr/bin/jq"):
        if os.path.exists(candidate):
            return candidate
    return None


JQ_BIN = find_jq()


@unittest.skipUnless(JQ_BIN, "jq unavailable; jq parity not checked")
class JqParityTests(unittest.TestCase):
    """Diff the transcription against the real jq programs.

    The Pi comparisons run the retired shell's `renormalise_quota_file`
    program after the normaliser, because `normalize_pi_*` models the composed
    `normalize | renormalise` path the shell fed into effective quota (and
    the typed `captured_at` field needs the epoch conversion).
    """

    def run_jq(self, key, payload):
        prefix = ""
        if key.startswith("normalize_codexbar"):
            # Shadow jq's `now` so `capturedAt` is deterministic.
            prefix = "def now: %d;\n" % NOW
        result = subprocess.run(
            [JQ_BIN, "-e", prefix + JQ_PROGRAMS[key]],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        return result.returncode, result.stdout

    def assertMatchesJq(self, key, payload, composed_renormalise=False):
        code, output = self.run_jq(key, payload)
        self.assertEqual(code, 0, "%s: jq produced no output" % key)
        reference = json.loads(output)
        if composed_renormalise:
            code, output = self.run_jq("renormalise_quota_file", reference)
            self.assertEqual(code, 0, "renormalise_quota_file produced no output")
            reference = json.loads(output)
        with mock.patch.object(normalize_module, "_now_epoch", return_value=NOW):
            document = getattr(normalize_module, key)(payload).as_document()
        if key.startswith("normalize_codexbar"):
            # The jq live programs never emit `cached`; the typed document
            # contract always carries it (false for a live reading).
            self.assertNotIn("cached", reference)
            document = dict(document)
            document.pop("cached")
        self.assertEqual(reference, document)

    def assertRejectsLikeJq(self, key, payload):
        code, output = self.run_jq(key, payload)
        self.assertTrue(
            code != 0 or not output.strip(),
            "%s: expected jq to produce no output" % key,
        )
        with self.assertRaises(QuotaNormalizationError):
            with mock.patch.object(normalize_module, "_now_epoch", return_value=NOW):
                getattr(normalize_module, key)(payload)

    def test_pi_fixtures_match_jq(self):
        self.assertMatchesJq("normalize_pi_codex", PI_CODEX_FIXTURE, True)
        self.assertMatchesJq(
            "normalize_pi_antigravity", PI_ANTIGRAVITY_FIXTURE, True
        )
        self.assertMatchesJq("normalize_pi_opencode", PI_OPENCODE_FIXTURE, True)

    def test_codexbar_payloads_match_jq(self):
        self.assertMatchesJq("normalize_codexbar_codex", CODEXBAR_CODEX)
        self.assertMatchesJq(
            "normalize_codexbar_antigravity", CODEXBAR_ANTIGRAVITY
        )
        self.assertMatchesJq("normalize_codexbar_opencode", CODEXBAR_OPENCODE)

    def test_remaining_rounding_matches_jq(self):
        self.assertMatchesJq("normalize_codexbar_codex", codex_row(47.5, 0.5))
        self.assertMatchesJq("normalize_codexbar_codex", codex_row(99.5, 150))
        self.assertMatchesJq("normalize_codexbar_codex", codex_row(-50, 100))

    def test_rejections_match_jq(self):
        self.assertRejectsLikeJq("normalize_pi_codex", {})
        self.assertRejectsLikeJq(
            "normalize_pi_codex",
            {"headers": dict(PI_CODEX_FIXTURE["headers"],
                             **{"x-codex-primary-window-minutes": "60"})},
        )
        self.assertRejectsLikeJq("normalize_codexbar_codex", [])
        self.assertRejectsLikeJq(
            "normalize_codexbar_opencode",
            [{"provider": "opencodego", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
            }}],
        )
        self.assertRejectsLikeJq(
            "normalize_codexbar_antigravity",
            [{"provider": "antigravity", "usage": {"extraRateWindows": [
                {"id": "claude", "window": {
                    "windowMinutes": 300, "usedPercent": 1, "resetsAt": 1}},
            ]}}],
        )
        self.assertRejectsLikeJq(
            "normalize_codexbar_codex",
            [{"provider": "codex", "usage": {
                "primary": {"windowMinutes": 300, "usedPercent": 1, "resetsAt": 1},
                "secondary": {"windowMinutes": 10080, "usedPercent": 1, "resetsAt": 2},
                "extraRateWindows": [{"window": "x"}],
            }}],
        )

    def test_renormalise_epoch_forms_match_jq(self):
        base = {
            "source": "CodexBar · cli",
            "fresh": True,
            "cached": False,
            "fiveHour": {"remainingPercent": 48, "resetAt": 1788012274},
            "weekly": {"remainingPercent": 65, "resetAt": 1788480117},
        }
        for captured in (
            1788000000,
            1788000000.9,
            "2026-08-29T11:21:38Z",
            "2026-08-29T11:21:38.123Z",
            "2026-08-29T19:21:38+08:00",
            "2026-08-29T19:21:38+0800",
            "2026-08-29T03:21:38-08:00",
            "not-a-date",
            None,
        ):
            with self.subTest(captured=captured):
                document = dict(base, capturedAt=captured)
                code, output = self.run_jq("renormalise_quota_file", document)
                self.assertEqual(code, 0)
                self.assertEqual(
                    json.loads(output), renormalize_document(document)
                )

    def test_renormalise_rejections_match_jq(self):
        for document in (
            {"fiveHour": {"remainingPercent": 1, "resetAt": None},
             "weekly": {"remainingPercent": 1, "resetAt": 2}},
            {"fiveHour": {"remainingPercent": 1, "resetAt": 1},
             "weekly": {"remainingPercent": 1, "resetAt": None}},
            {"capturedAt": "2026-8-9T1:2:3+08:00",
             "fiveHour": {"remainingPercent": 1, "resetAt": 1},
             "weekly": {"remainingPercent": 1, "resetAt": 2}},
        ):
            with self.subTest(document=document):
                code, output = self.run_jq("renormalise_quota_file", document)
                self.assertTrue(code != 0 or not output.strip())
                with self.assertRaises(QuotaNormalizationError):
                    renormalize_document(document)


# ---------------------------------------------------------------------------
# Native tier-1 helpers.
#
# `antigravity_usage.py` and `opencode_usage.py` are LIVE production code:
# `quota_sentinel.runtime.quota_probe` executes them as external helpers and
# parses their stdout with `parse_document`, and it regex-extracts the
# `antigravity_usage: <reason>` / `opencode_usage: <reason>` stderr prefix.
# Ported verbatim from the retired tests/antigravity-native-regression.py and
# tests/opencode-native-regression.py (only their shell-tier tests, which
# sourced the deleted zsh implementation, are gone); the probe suite stubs
# these helpers out, so this is the only place their internals are pinned.
# ---------------------------------------------------------------------------

def agy_report():
    return {
        "status": "SUCCESS", "num_turns": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "command": {"name": "usage", "data": {"groups": [
            {"name": "Claude and GPT models", "buckets": []},
            {"name": "Gemini Models", "buckets": [
                {"window": "5h", "remaining_fraction": 0.755,
                 "reset_time": "2026-09-16T13:08:03Z"},
                {"window": "weekly", "remaining_fraction": 0.9,
                 "reset_time": "2026-09-23T08:08:03Z"},
            ]},
        ]}},
    }


class AntigravityNativeHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def binary(self, body, name="agy"):
        path = self.directory / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def agy(self, version="1.2.4"):
        return self.binary(
            "import json,os,sys\n"
            f"if sys.argv[1:]==['--version']: print({version!r})\n"
            "else:\n"
            " assert sys.argv[1:]==['-p','/usage','--output-format','json']\n"
            " assert not os.listdir('.')\n"
            " assert os.stat('.').st_mode & 0o777 == 0o700\n"
            f" print({json.dumps(agy_report())!r})\n"
        )

    def test_gemini_both_windows_fresh_absolute_reset_and_rounding(self):
        result = antigravity_quota.normalize_report(agy_report(), 1789530000)
        self.assertEqual(result["source"], "Native · agy /usage")
        self.assertTrue(result["fresh"])
        self.assertEqual(result["capturedAt"], 1789530000)
        self.assertEqual(result["fiveHour"]["remainingPercent"], 76)
        self.assertEqual(result["weekly"]["remainingPercent"], 90)
        self.assertEqual(result["fiveHour"]["resetAt"], 1789564083)
        self.assertEqual(
            antigravity_quota.normalize_report(
                agy_report(), 1789530900)["fiveHour"], result["fiveHour"]
        )

    def test_fraction_endpoints_and_unknown_values(self):
        for fraction, expected in [(0, 0), (1, 100)]:
            data = agy_report()
            data["command"]["data"]["groups"][1]["buckets"][0][
                "remaining_fraction"] = fraction
            self.assertEqual(
                antigravity_quota.normalize_report(data, 1)["fiveHour"][
                    "remainingPercent"],
                expected,
            )
        for value in (None, True, "0.5", -0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value):
                data = agy_report()
                data["command"]["data"]["groups"][1]["buckets"][0][
                    "remaining_fraction"] = value
                with self.assertRaises(antigravity_quota.QuotaError):
                    antigravity_quota.normalize_report(data, 1)

    def test_missing_disabled_unknown_and_duplicate_windows_rejected(self):
        for mutation in ("missing", "enabled", "usage_known", "duplicate"):
            data = agy_report()
            buckets = data["command"]["data"]["groups"][1]["buckets"]
            if mutation == "missing":
                buckets.pop()
            elif mutation == "duplicate":
                buckets.append(copy.deepcopy(buckets[0]))
            else:
                buckets[0][mutation] = False
            with self.assertRaises(antigravity_quota.QuotaError):
                antigravity_quota.normalize_report(data, 1)
        data = agy_report()
        data["command"]["data"]["groups"][1]["enabled"] = False
        with self.assertRaises(antigravity_quota.QuotaError):
            antigravity_quota.normalize_report(data, 1)

    def test_reset_missing_invalid_or_timezone_naive_rejected(self):
        for reset in (
            None, "garbage", "2026-09-16T13:08:03", "1960-01-01T00:00:00Z"
        ):
            data = agy_report()
            data["command"]["data"]["groups"][1]["buckets"][0][
                "reset_time"] = reset
            with self.assertRaises(antigravity_quota.QuotaError):
                antigravity_quota.normalize_report(data, 1)

    def test_wrong_command_status_group_or_inference_rejected(self):
        variations = []
        for key, value in (("status", "ERROR"), ("num_turns", 1),
                           ("num_turns", False), ("error", "secret")):
            data = agy_report(); data[key] = value; variations.append(data)
        data = agy_report(); data["command"]["name"] = "model"
        variations.append(data)
        data = agy_report(); data["command"]["data"]["groups"].pop()
        variations.append(data)
        data = agy_report()
        data["command"]["data"]["groups"].append(
            copy.deepcopy(data["command"]["data"]["groups"][1])
        )
        variations.append(data)
        for field in ("input_tokens", "output_tokens", "total_tokens",
                      "thinking_tokens", "cache_read_tokens"):
            data = agy_report(); data["usage"][field] = 1; variations.append(data)
        data = agy_report(); del data["usage"]["total_tokens"]
        variations.append(data)
        for data in variations:
            with self.assertRaises(antigravity_quota.QuotaError):
                antigravity_quota.normalize_report(data, 1)

    def test_supported_binary_only_builtin_command_private_empty_cwd(self):
        self.assertTrue(antigravity_quota.fetch_quota(self.agy(), 3)["fresh"])

    def test_old_version_never_invokes_print_or_model(self):
        marker = self.directory / "unexpected-call"
        binary = self.binary(
            "import pathlib,sys\n"
            "if sys.argv[1:]==['--version']: print('1.1.10')\n"
            "else: pathlib.Path(" + repr(str(marker)) + ").touch()\n"
        )
        with self.assertRaisesRegex(
            antigravity_quota.QuotaError, "unsupported_agy_version"
        ):
            antigravity_quota.fetch_quota(binary, 3)
        self.assertFalse(marker.exists())
        self.assertTrue(antigravity_quota.version_supported(b"1.1.11\n"))
        self.assertFalse(antigravity_quota.version_supported(b"unknown"))

    def test_output_and_nonzero_exit_are_bounded(self):
        with self.assertRaisesRegex(
            antigravity_quota.QuotaError, "output_too_large"
        ):
            antigravity_quota.run_bounded(
                [sys.executable, "-c", "print('x'*1000)"], self.directory, 2, 100
            )
        with self.assertRaisesRegex(
            antigravity_quota.QuotaError, "command_failed"
        ):
            antigravity_quota.run_bounded(
                [sys.executable, "-c", "raise SystemExit(7)"],
                self.directory, 2, 100,
            )

    def test_timeout_reaps_child_does_not_touch_external_process(self):
        # The child records its OWN pid (the process-group leader) before it
        # spawns a descendant, so the reaping assertion does not depend on the
        # grandchild winning a scheduling race against the timeout.
        pid_file = self.directory / "child.pid"
        body = (
            "import os,pathlib,subprocess,sys,time\n"
            "pathlib.Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid()))\n"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            "time.sleep(60)\n"
        )
        binary = self.binary(body)
        external = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(60)"]
        )
        started = time.monotonic()
        try:
            try:
                antigravity_quota.run_bounded(
                    [str(binary)], self.directory, 2.0, 100
                )
                self.fail("run_bounded did not time out")
            except antigravity_quota.QuotaError as exc:
                self.assertIn("command_timeout", str(exc))
            except PermissionError:
                # macOS can recycle the process-group id inside the helper's
                # SIGTERM -> SIGKILL window, which surfaces as EPERM from
                # killpg. The reaping assertions below are the property this
                # test exists for.
                pass
            self.assertLess(time.monotonic() - started, 6)
            time.sleep(0.1)
            self.assertTrue(pid_file.exists(), "the child never recorded its pid")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            self.assertIsNone(external.poll())
        finally:
            external.terminate(); external.wait()

    def test_cli_logs_fixed_reason_not_subprocess_secret(self):
        binary = self.binary(
            "import sys\n"
            "if sys.argv[1:]==['--version']: print('1.2.4')\n"
            "else: print('app_secret=TOPSECRET',file=sys.stderr);"
            " raise SystemExit(7)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "antigravity_usage.py"),
             "--agy", str(binary)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("command_failed", result.stderr)
        self.assertNotIn("TOPSECRET", result.stderr)

    def test_cli_sigterm_cleans_up_owned_quota_process(self):
        marker = self.directory / "quota.pid"
        binary = self.binary(
            "import os,pathlib,sys,time\n"
            "if sys.argv[1:]==['--version']: print('1.2.4')\n"
            "else:\n"
            " pathlib.Path(" + repr(str(marker)) + ").write_text("
            "str(os.getpid()))\n"
            " time.sleep(60)\n"
        )
        helper = subprocess.Popen(
            [sys.executable, "-B", str(REPO_ROOT / "antigravity_usage.py"),
             "--agy", str(binary)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            helper.send_signal(signal.SIGTERM)
            out, err = helper.communicate(timeout=3)
            self.assertEqual(helper.returncode, 1)
            self.assertEqual(out, "")
            self.assertIn("command_cancelled", err)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)
        finally:
            if helper.poll() is None:
                helper.terminate(); helper.wait(timeout=3)


# Real values observed from https://opencode.ai/zen/go/v1/usage: percent is the
# USED fraction, so remaining is its complement.
OCG_ROLLING_RESET_ISO = "2026-09-17T08:18:58.000Z"
OCG_ROLLING_RESET_EPOCH = 1789633138
OCG_WEEKLY_RESET_ISO = "2026-09-21T00:00:00.000Z"
OCG_WEEKLY_RESET_EPOCH = 1789948800
OCG_MONTHLY_RESET_ISO = "2026-10-17T03:04:30.000Z"
OCG_MONTHLY_RESET_EPOCH = 1792206270


def ocg_report():
    return {"usage": {
        "rolling": {"status": "ok", "percent": 12,
                    "resetsAt": OCG_ROLLING_RESET_ISO},
        "weekly": {"status": "ok", "percent": 4,
                   "resetsAt": OCG_WEEKLY_RESET_ISO},
        "monthly": {"status": "ok", "percent": 2,
                    "resetsAt": OCG_MONTHLY_RESET_ISO},
    }}


def cp_report():
    """The live ClinePass payload shape: three limits under data.limits."""
    return {"success": True, "data": {"limits": [
        {"type": "five_hour", "percentUsed": 12,
         "resetsAt": CP_FIVE_HOUR_RESET_ISO},
        {"type": "weekly", "percentUsed": 4,
         "resetsAt": CP_WEEKLY_RESET_ISO},
        {"type": "monthly", "percentUsed": 2,
         "resetsAt": CP_MONTHLY_RESET_ISO},
    ]}}


CP_FIVE_HOUR_RESET_ISO = "2026-09-23T14:09:42.819795817Z"
CP_FIVE_HOUR_EPOCH = 1790172582
CP_WEEKLY_RESET_ISO = "2026-09-30T09:09:42.822018837Z"
CP_WEEKLY_EPOCH = 1790759382
CP_MONTHLY_RESET_ISO = "2026-10-23T09:09:42.824158127Z"
CP_MONTHLY_EPOCH = 1792746582


class OpencodeNativeHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, body, name):
        path = self.directory / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def curl(self, body=None, status="200", name="curl"):
        payload = json.dumps(ocg_report() if body is None else body)
        # Mirror the real invocation: body, then --write-out's "\n<status>".
        return self.script(
            "import json,pathlib,sys\n"
            "pathlib.Path(sys.argv[0] + '.argv').write_text("
            "json.dumps(sys.argv[1:]))\n"
            "pathlib.Path(sys.argv[0] + '.stdin').write_text(sys.stdin.read())\n"
            f"sys.stdout.write({payload!r} + '\\n' + {status!r})\n",
            name,
        )

    def test_used_percent_becomes_remaining_in_all_three_windows(self):
        result = opencode_quota.normalize_report(ocg_report(), 1789630000)
        self.assertEqual(result["source"], "Native · opencode-go /usage")
        self.assertTrue(result["fresh"])
        self.assertEqual(result["capturedAt"], 1789630000)
        self.assertEqual(result["fiveHour"], {
            "remainingPercent": 88, "resetAt": OCG_ROLLING_RESET_EPOCH})
        self.assertEqual(result["weekly"], {
            "remainingPercent": 96, "resetAt": OCG_WEEKLY_RESET_EPOCH})
        self.assertEqual(result["monthly"], {
            "remainingPercent": 98, "resetAt": OCG_MONTHLY_RESET_EPOCH})

    def test_monthly_is_optional_and_a_bad_one_is_dropped_not_fatal(self):
        data = ocg_report()
        del data["usage"]["monthly"]
        self.assertNotIn("monthly", opencode_quota.normalize_report(data, 1))
        for monthly in (
            {"status": "ok", "percent": None, "resetsAt": OCG_MONTHLY_RESET_ISO},
            {"status": "ok", "percent": 2, "resetsAt": "garbage"},
            "not-an-object",
        ):
            data = ocg_report()
            data["usage"]["monthly"] = monthly
            with self.subTest(monthly=monthly):
                self.assertNotIn(
                    "monthly", opencode_quota.normalize_report(data, 1)
                )

    def test_percent_endpoints_and_rate_limited_status(self):
        for percent, expected in ((0, 100), (100, 0)):
            data = ocg_report()
            for window in data["usage"].values():
                window["percent"] = percent
            self.assertEqual(
                opencode_quota.normalize_report(data, 1)["fiveHour"][
                    "remainingPercent"],
                expected,
            )
        data = ocg_report()
        data["usage"]["rolling"]["status"] = "rate-limited"
        self.assertEqual(
            opencode_quota.normalize_report(data, 1)["fiveHour"][
                "remainingPercent"],
            88,
        )

    def test_required_window_missing_or_malformed_rejected(self):
        for api_name in ("rolling", "weekly"):
            data = ocg_report()
            del data["usage"][api_name]
            with self.assertRaises(opencode_quota.QuotaError):
                opencode_quota.normalize_report(data, 1)
        variations = []
        for bad_status in ("", None, "unknown", 1):
            data = ocg_report()
            data["usage"]["rolling"]["status"] = bad_status
            variations.append(data)
        for bad_percent in (None, True, "12", -1, 101, float("nan"),
                            float("inf")):
            data = ocg_report()
            data["usage"]["rolling"]["percent"] = bad_percent
            variations.append(data)
        for bad_reset in (None, "garbage", "2026-09-17T08:18:58",
                          "1960-01-01T00:00:00Z", 0):
            data = ocg_report()
            data["usage"]["weekly"]["resetsAt"] = bad_reset
            variations.append(data)
        data = ocg_report(); variations.append({"usage": "nope"})
        data = ocg_report(); del data["usage"]["rolling"]["resetsAt"]
        variations.append(data)
        for data in variations:
            with self.subTest(data=data):
                with self.assertRaises(opencode_quota.QuotaError):
                    opencode_quota.normalize_report(data, 1)

    def test_output_and_nonzero_exit_are_bounded(self):
        with self.assertRaisesRegex(opencode_quota.QuotaError, "output_too_large"):
            opencode_quota.run_bounded(
                [sys.executable, "-c", "print('x'*1000)"], self.directory, 2, 100
            )
        with self.assertRaisesRegex(opencode_quota.QuotaError, "command_failed"):
            opencode_quota.run_bounded(
                [sys.executable, "-c", "raise SystemExit(7)"],
                self.directory, 2, 100,
            )

    def test_timeout_reaps_child_does_not_touch_external_process(self):
        # The child records its OWN pid (the process-group leader) before it
        # spawns a descendant, so the reaping assertion does not depend on the
        # grandchild winning a scheduling race against the timeout.
        pid_file = self.directory / "child.pid"
        body = (
            "import os,pathlib,subprocess,sys,time\n"
            "pathlib.Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid()))\n"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            "time.sleep(60)\n"
        )
        binary = self.script(body, "slow")
        external = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(60)"]
        )
        started = time.monotonic()
        try:
            try:
                opencode_quota.run_bounded([str(binary)], self.directory, 2.0, 100)
                self.fail("run_bounded did not time out")
            except opencode_quota.QuotaError as exc:
                self.assertIn("command_timeout", str(exc))
            except PermissionError:
                # macOS can recycle the process-group id inside the helper's
                # SIGTERM -> SIGKILL window, which surfaces as EPERM from
                # killpg. The reaping assertions below are the property this
                # test exists for.
                pass
            self.assertLess(time.monotonic() - started, 6)
            time.sleep(0.1)
            self.assertTrue(pid_file.exists(), "the child never recorded its pid")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            self.assertIsNone(external.poll())
        finally:
            external.terminate(); external.wait()

    def test_key_travels_on_stdin_into_a_curl_header_config(self):
        curl = self.curl()
        result = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "opencode_usage.py"),
             "--curl", str(curl)],
            input="sk-secret-value\n", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["fiveHour"]["remainingPercent"], 88
        )
        # The key reaches curl only as a stdin config header, never as an
        # argument: argv must not carry it.
        argv = json.loads(Path(str(curl) + ".argv").read_text())
        self.assertIn("--config", argv)
        self.assertNotIn("sk-secret-value", " ".join(argv))
        config = Path(str(curl) + ".stdin").read_text()
        self.assertIn("Authorization: Bearer sk-secret-value", config)

    def test_http_status_maps_to_fixed_reason_codes(self):
        for status, reason in (("401", "http_401"), ("403", "http_403"),
                               ("500", "http_error")):
            curl = self.curl(
                body={"type": "error"}, status=status, name=f"curl{status}"
            )
            with self.subTest(status=status):
                result = subprocess.run(
                    [sys.executable, "-B", str(REPO_ROOT / "opencode_usage.py"),
                     "--curl", str(curl)],
                    input="sk-whatever\n", capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)

    def test_missing_key_and_missing_curl_fail_closed(self):
        curl = self.curl()
        for args, stdin, reason in (
            (["--curl", str(curl)], "\n", "auth_missing"),
            (["--curl", str(self.directory / "absent")], "sk-x\n",
             "curl_unavailable"),
        ):
            with self.subTest(reason=reason):
                result = subprocess.run(
                    [sys.executable, "-B", str(REPO_ROOT / "opencode_usage.py")]
                    + args,
                    input=stdin, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)

    def test_cli_logs_fixed_reason_not_response_body_secrets(self):
        curl = self.script(
            "import sys\nsys.stdout.write('app_secret=TOPSECRET\\n401')\n",
            "curlLeaky",
        )
        result = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "opencode_usage.py"),
             "--curl", str(curl)],
            input="sk-real-key\n", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("http_401", result.stderr)
        self.assertNotIn("TOPSECRET", result.stderr)
        self.assertNotIn("sk-real-key", result.stderr)

    def test_invalid_json_body_is_rejected(self):
        curl = self.script("import sys\nsys.stdout.write('not json\\n200')\n",
                           "curlBadJson")
        result = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "opencode_usage.py"),
             "--curl", str(curl)],
            input="sk-x\n", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid_report", result.stderr)

    def test_cli_sigterm_cleans_up_owned_quota_process(self):
        marker = self.directory / "quota.pid"
        curl = self.script(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid()))\n"
            "time.sleep(60)\n",
            "curlSlow",
        )
        helper = subprocess.Popen(
            [sys.executable, "-B", str(REPO_ROOT / "opencode_usage.py"),
             "--curl", str(curl)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            helper.stdin.write("sk-key\n")
            helper.stdin.flush()
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            helper.send_signal(signal.SIGTERM)
            out, err = helper.communicate(timeout=3)
            self.assertEqual(helper.returncode, 1)
            self.assertEqual(out, "")
            self.assertIn("command_cancelled", err)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)
        finally:
            if helper.poll() is None:
                helper.terminate(); helper.wait(timeout=3)

    def test_outer_budget_covers_four_providers_and_delivery(self):
        from quota_sentinel.runtime.budgets import listener_usage_budget
        bound = listener_usage_budget({})
        # Per provider: every fresh rung's own budget plus CodexBar's kill
        # grace. ClinePass joined the roster with a native helper, so its two
        # budgeted rungs (15 + 20 + 10) are part of the worst case now.
        acquisition = (20 + 15 + 2 * (20 + 10) + 20 + 1 + 35 + 10 + 16
                       + (20 + 10) + (15 + 20 + 10))
        delivery = 45 + 3 * 45 + 3
        self.assertGreater(bound, acquisition + delivery)


class ClinePassNativeHelperTests(unittest.TestCase):
    """Tier ① for ClinePass: the same discipline as the OpenCode helper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, body, name):
        path = self.directory / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def curl(self, body=None, status="200", name="curl"):
        payload = json.dumps(cp_report() if body is None else body)
        # Mirror the real invocation: body, then --write-out's "\n<status>".
        return self.script(
            "import json,pathlib,sys\n"
            "pathlib.Path(sys.argv[0] + '.argv').write_text("
            "json.dumps(sys.argv[1:]))\n"
            "pathlib.Path(sys.argv[0] + '.stdin').write_text(sys.stdin.read())\n"
            f"sys.stdout.write({payload!r} + '\\n' + {status!r})\n",
            name,
        )

    def test_used_percent_becomes_remaining_in_all_three_windows(self):
        result = clinepass_quota.normalize_report(cp_report(), 1789630000)
        self.assertEqual(result["source"], "Native · clinepass /plan/usage-limits")
        self.assertTrue(result["fresh"])
        self.assertEqual(result["capturedAt"], 1789630000)
        self.assertEqual(result["fiveHour"], {
            "remainingPercent": 88, "resetAt": CP_FIVE_HOUR_EPOCH})
        self.assertEqual(result["weekly"], {
            "remainingPercent": 96, "resetAt": CP_WEEKLY_EPOCH})
        self.assertEqual(result["monthly"], {
            "remainingPercent": 98, "resetAt": CP_MONTHLY_EPOCH})

    def test_nanosecond_reset_is_truncated_to_whole_seconds(self):
        # The live gateway answers "...T09:09:42.819795817Z", which the 3.9
        # interpreter's fromisoformat refuses; the fraction is dropped.
        data = cp_report()
        data["data"]["limits"][0]["resetsAt"] = "2026-09-23T14:09:42.819795817Z"
        self.assertEqual(
            clinepass_quota.normalize_report(data, 1)["fiveHour"]["resetAt"],
            1790172582,
        )

    def test_monthly_is_optional_and_a_bad_one_is_dropped_not_fatal(self):
        for monthly in (None, {"type": "monthly"},
                        {"type": "monthly", "percentUsed": "2",
                         "resetsAt": CP_MONTHLY_RESET_ISO}):
            data = cp_report()
            limits = [limit for limit in data["data"]["limits"]
                      if limit["type"] != "monthly"]
            if monthly is not None:
                limits.append(monthly)
            data["data"]["limits"] = limits
            with self.subTest(monthly=monthly):
                self.assertNotIn(
                    "monthly", clinepass_quota.normalize_report(data, 1)
                )

    def test_percent_endpoints_are_clamped(self):
        for percent, expected in ((0, 100), (100, 0)):
            data = cp_report()
            for limit in data["data"]["limits"]:
                limit["percentUsed"] = percent
            self.assertEqual(
                clinepass_quota.normalize_report(data, 1)["fiveHour"][
                    "remainingPercent"],
                expected,
            )

    def test_required_window_missing_or_malformed_rejected(self):
        for api_name in ("five_hour", "weekly"):
            data = cp_report()
            data["data"]["limits"] = [
                limit for limit in data["data"]["limits"]
                if limit["type"] != api_name
            ]
            with self.assertRaises(clinepass_quota.QuotaError):
                clinepass_quota.normalize_report(data, 1)
        variations = []
        for bad_percent in (None, True, "12", -1, 101, float("nan"),
                            float("inf")):
            data = cp_report()
            data["data"]["limits"][0]["percentUsed"] = bad_percent
            variations.append(data)
        for bad_reset in (None, "garbage", "2026-09-23T14:09:42",
                          "1960-01-01T00:00:00Z", 1790172582):
            data = cp_report()
            data["data"]["limits"][1]["resetsAt"] = bad_reset
            variations.append(data)
        for data in variations:
            with self.subTest(data=data):
                with self.assertRaises(clinepass_quota.QuotaError):
                    clinepass_quota.normalize_report(data, 1)

    def test_an_idle_account_without_resets_is_rejected_not_invented(self):
        # The live gateway omits resetsAt entirely while no window is open.
        # A fabricated reset would become a deadline, so this fails closed.
        data = cp_report()
        for limit in data["data"]["limits"]:
            limit.pop("resetsAt", None)
        with self.assertRaises(clinepass_quota.QuotaError):
            clinepass_quota.normalize_report(data, 1)

    def test_success_flag_and_shape_are_enforced(self):
        for mutation in ("success_false", "no_data", "limits_not_list",
                         "not_object"):
            data = cp_report()
            if mutation == "success_false":
                data["success"] = False
            elif mutation == "no_data":
                del data["data"]
            elif mutation == "limits_not_list":
                data["data"]["limits"] = {"type": "five_hour"}
            else:
                data = ["not", "an", "object"]
            with self.subTest(mutation=mutation):
                with self.assertRaises(clinepass_quota.QuotaError):
                    clinepass_quota.normalize_report(data, 1)

    def test_output_and_nonzero_exit_are_bounded(self):
        with self.assertRaisesRegex(clinepass_quota.QuotaError, "output_too_large"):
            clinepass_quota.run_bounded(
                [sys.executable, "-c", "print('x'*1000)"], self.directory, 2, 100
            )
        with self.assertRaisesRegex(clinepass_quota.QuotaError, "command_failed"):
            clinepass_quota.run_bounded(
                [sys.executable, "-c", "raise SystemExit(7)"],
                self.directory, 2, 100,
            )

    def test_curl_exit_code_survives_in_the_reason(self):
        # "command_failed" alone cannot tell a gateway reset from a DNS blip,
        # and the body and curl stderr are deliberately discarded, so the exit
        # code rides along in the reason instead.
        with self.assertRaisesRegex(clinepass_quota.QuotaError, "command_failed_7"):
            clinepass_quota.run_bounded(
                [sys.executable, "-c", "raise SystemExit(7)"],
                self.directory, 2, 100,
            )
        with self.assertRaisesRegex(clinepass_quota.QuotaError, "command_timeout"):
            clinepass_quota.run_bounded(
                [sys.executable, "-c", "raise SystemExit(28)"],
                self.directory, 2, 100,
            )

    def test_key_travels_on_stdin_into_a_curl_header_config(self):
        curl = self.curl()
        result = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "clinepass_usage.py"),
             "--curl", str(curl)],
            input="cline-secret-value\n", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["fiveHour"]["remainingPercent"], 88
        )
        argv = json.loads(Path(str(curl) + ".argv").read_text())
        self.assertIn("--config", argv)
        self.assertNotIn("cline-secret-value", " ".join(argv))
        config = Path(str(curl) + ".stdin").read_text()
        self.assertIn("Authorization: Bearer cline-secret-value", config)
        # Cline's docs: X-Title labels the caller in their usage logs.
        self.assertIn("X-Title: quota-sentinel", config)

    def test_http_status_maps_to_fixed_reason_codes(self):
        for status, reason in (("401", "http_401"), ("403", "http_403"),
                               ("500", "http_error")):
            curl = self.curl(
                body={"success": False}, status=status, name=f"curl{status}"
            )
            with self.subTest(status=status):
                result = subprocess.run(
                    [sys.executable, "-B", str(REPO_ROOT / "clinepass_usage.py"),
                     "--curl", str(curl)],
                    input="cline-x\n", capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)

    def test_missing_key_and_missing_curl_fail_closed(self):
        curl = self.curl()
        for args, stdin, reason in (
            (["--curl", str(curl)], "\n", "auth_missing"),
            (["--curl", str(self.directory / "absent")], "cline-x\n",
             "curl_unavailable"),
        ):
            with self.subTest(reason=reason):
                result = subprocess.run(
                    [sys.executable, "-B", str(REPO_ROOT / "clinepass_usage.py")]
                    + args,
                    input=stdin, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)

    def test_cli_logs_fixed_reason_not_response_body_secrets(self):
        curl = self.script(
            "import sys\nsys.stdout.write('app_secret=TOPSECRET\\n401')\n",
            "curlLeaky",
        )
        result = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "clinepass_usage.py"),
             "--curl", str(curl)],
            input="cline-real-key\n", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("http_401", result.stderr)
        self.assertNotIn("TOPSECRET", result.stderr)
        self.assertNotIn("cline-real-key", result.stderr)

    def test_invalid_json_body_is_rejected(self):
        curl = self.script("import sys\nsys.stdout.write('not json\\n200')\n",
                           "curlBadJson")
        result = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "clinepass_usage.py"),
             "--curl", str(curl)],
            input="cline-x\n", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid_report", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
