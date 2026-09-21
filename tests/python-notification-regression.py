#!/usr/bin/env python3
"""Phase 5 tests: notification selection policy.

Transport and card rendering stay in the shell; this module owns WHETHER to
notify, for whom, and in which layout. The suite pins the rules that have
historically drifted, and proves the module is pure.

Run: PYTHONPATH=. uv run --frozen --no-sync python tests/python-notification-regression.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.notifications import (
    Layout,
    NotificationEvent,
    WIDE_PROVIDER,
    layout_for,
    plan_recovery,
    plan_task,
    plan_usage,
)
from quota_sentinel.state.migration import DEFAULT_PROVIDERS

SHELL = (REPO / "quota-sentinel.sh").read_text(encoding="utf-8")
ROSTER = list(DEFAULT_PROVIDERS)


class LayoutTests(unittest.TestCase):
    def test_layout_ladder(self):
        self.assertIs(layout_for([]), Layout.TEXT)
        self.assertIs(layout_for(["codex"]), Layout.SINGLE)
        self.assertIs(layout_for(["codex", "antigravity"]), Layout.TWO_COLUMN)
        self.assertIs(layout_for(["codex", "opencode"]), Layout.STACKED)
        self.assertIs(layout_for(["codex", "antigravity", "opencode"]), Layout.STACKED)
        self.assertIs(layout_for(["a", "b", "c"]), Layout.STACKED)

    def test_layout_is_order_independent(self):
        self.assertIs(
            layout_for(["antigravity", "codex"]), layout_for(["codex", "antigravity"])
        )

    def test_wide_provider_is_the_one_with_a_third_window(self):
        self.assertEqual(WIDE_PROVIDER, "opencode")


class PlanTests(unittest.TestCase):
    def test_task_plan_covers_exactly_what_was_attempted(self):
        plan = plan_task(["codex"])
        self.assertIs(plan.event, NotificationEvent.TASK)
        self.assertIs(plan.layout, Layout.SINGLE)
        self.assertEqual(plan.providers, ("codex",))
        self.assertTrue(plan.is_card)

    def test_task_plan_is_silent_when_nothing_ran(self):
        plan = plan_task([])
        self.assertIs(plan.layout, Layout.TEXT)
        self.assertEqual(plan.providers, ())
        self.assertTrue(plan.is_silent)

    def test_usage_plan_always_covers_the_full_roster(self):
        plan = plan_usage(ROSTER)
        self.assertIs(plan.event, NotificationEvent.USAGE)
        self.assertEqual(plan.providers, tuple(ROSTER))
        self.assertTrue(plan.is_card)
        # A partial /usage answer would be a silent regression, so the plan
        # must not depend on what happened to be probed.
        self.assertEqual(set(plan_usage(list(reversed(ROSTER))).providers), set(ROSTER))

    def test_recovery_is_silent_unless_a_debt_was_actually_repaid(self):
        self.assertIsNone(plan_recovery([]))
        plan = plan_recovery(["codex"])
        self.assertIsNotNone(plan)
        self.assertIs(plan.event, NotificationEvent.RECOVERY)
        self.assertEqual(plan.providers, ("codex",))
        self.assertEqual(plan.deduplication_key_prefix, "quota-sentinel-recovered")

    def test_plans_carry_a_deduplication_prefix(self):
        for plan in (plan_task(["codex"]), plan_usage(ROSTER), plan_recovery(["codex"])):
            self.assertTrue(plan.deduplication_key_prefix)
            self.assertNotIn(" ", plan.deduplication_key_prefix)


class ShellAgreementTests(unittest.TestCase):
    """The shell must not keep a second copy of the roster or the rules."""

    def test_shell_roster_matches_the_declared_roster(self):
        match = re.search(r"^readonly PROVIDERS=\(([^)]*)\)", SHELL, re.M)
        self.assertIsNotNone(match, "the shell no longer declares PROVIDERS")
        shell_roster = tuple(match.group(1).split())
        self.assertEqual(shell_roster, tuple(ROSTER))
        # ... and the usage plan's roster IS that roster.
        self.assertEqual(plan_usage(shell_roster).providers, shell_roster)

    def test_shell_delegates_layout_selection(self):
        start = SHELL.index("dispatch_task_notification() {")
        end = SHELL.index("\n}", start)
        body = SHELL[start:end]
        self.assertIn("notification_plan_field layout task", body)
        # The old inline "contains opencode?" rule must not come back.
        self.assertNotIn("provider_list_includes opencode", body)

    def test_shell_delegates_the_usage_roster(self):
        start = SHELL.index("send_usage_notification() {")
        end = SHELL.index("\n}", start)
        body = SHELL[start:end]
        self.assertIn("notification_plan_field providers usage", body)

    def test_shell_keeps_owning_transport_and_rendering(self):
        """The boundary is explicit: the plan says what and for whom, the
        shell still builds and sends the card."""
        for builder in (
            "build_feishu_v2_task_payload",
            "build_feishu_v2_stacked_payload",
            "dispatch_notification",
            "task_notification_message",
        ):
            with self.subTest(builder=builder):
                self.assertIn(builder, SHELL)


class PurityTests(unittest.TestCase):
    def test_planners_do_not_touch_the_filesystem_or_the_clock(self):
        """A plan is a pure function of its inputs: no state dir, no time."""
        source = (
            REPO / "quota_sentinel" / "notifications" / "plan.py"
        ).read_text(encoding="utf-8")
        for forbidden in ("open(", "Path(", "os.", "time.", "subprocess", "socket"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
