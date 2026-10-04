"""Notification selection policy.

Transport and card rendering live in the shell (stable, shell-native,
separately pinned); the decision of whether to notify, for whom and in
which layout lives here, next to the scheduler that produces the events.
"""
from __future__ import annotations

from .plan import (
    WIDE_PROVIDER,
    Layout,
    NotificationEvent,
    NotificationPlan,
    layout_for,
    plan_recovery,
    plan_task,
    plan_usage,
)

__all__ = [
    "NotificationEvent",
    "Layout",
    "NotificationPlan",
    "WIDE_PROVIDER",
    "layout_for",
    "plan_task",
    "plan_usage",
    "plan_recovery",
]
