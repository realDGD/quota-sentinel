"""Notification selection policy (Phase 5).

The transport and the card RENDERING stay in the shell — Feishu auth,
retries, the card JSON builders and the message payloads are stable,
shell-native and covered by their own suites. What moves here is the
decision that precedes them: **whether to notify, for which providers, and
in which layout**, expressed as a pure function.

That split is deliberate. The shell is very good at "render this card and
send it"; the interesting policy is the part that has repeatedly drifted —
"exactly one card for a recovery", "the /usage card always covers the full
roster", "a continued outage is log-only so a long failure never spams a
card every fifteen minutes". Those are rules about events, not about
Feishu, so they belong next to the scheduler that produces the events.

Nothing here performs I/O, and nothing here knows a card's field names.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple


class NotificationEvent(Enum):
    """One reason to talk to the user."""

    TASK = "task"            # a scheduled run finished (success or failure)
    USAGE = "usage"          # an explicit /usage request
    RECOVERY = "recovery"    # a pending debt was finally repaid
    BUSY = "busy"            # /usage could not get the quota lock
    TEST = "test"            # an operator-triggered test card


class Layout(Enum):
    """Which card shape the renderer should build."""

    SINGLE = "single"        # one provider fills the card
    TWO_COLUMN = "two-column"  # the original two providers side by side
    STACKED = "stacked"      # one full-width block per provider
    TEXT = "text"            # no card at all (busy/plain payload)


# The provider whose third (monthly) window forces the stacked layout: no
# other provider's blocks fit beside a three-window column.
WIDE_PROVIDER = "opencode"


@dataclass(frozen=True)
class NotificationPlan:
    """What to send, for whom, in which shape."""

    event: NotificationEvent
    layout: Layout
    providers: Tuple[str, ...]
    deduplication_key_prefix: str
    reason: str

    @property
    def is_card(self) -> bool:
        return self.layout is not Layout.TEXT

    @property
    def is_silent(self) -> bool:
        return not self.providers and self.layout is Layout.TEXT


def layout_for(providers: Sequence[str]) -> Layout:
    """The card shape for a provider set.

    The rule reproduces the shell's builder selection exactly for the
    current roster: a single provider fills the card, the original pair
    shares a two-column card, and anything involving OpenCode (or more than
    two providers, should the roster ever grow) stacks.
    """
    members = tuple(providers)
    if not members:
        return Layout.TEXT
    if len(members) == 1:
        return Layout.SINGLE
    if len(members) > 2 or WIDE_PROVIDER in members:
        return Layout.STACKED
    return Layout.TWO_COLUMN


def plan_task(providers: Sequence[str]) -> NotificationPlan:
    """One card per run, covering exactly the providers that were attempted."""
    members = tuple(providers)
    if not members:
        return NotificationPlan(
            NotificationEvent.TASK, Layout.TEXT, (),
            "quota-sentinel", "nothing was attempted; nothing to report",
        )
    return NotificationPlan(
        NotificationEvent.TASK, layout_for(members), members,
        "quota-sentinel", f"task card for {len(members)} attempted provider(s)",
    )


def plan_usage(roster: Sequence[str]) -> NotificationPlan:
    """/usage always answers, and always covers the FULL roster.

    Even a provider that was not involved in this run has a quota the user
    asked about; a partial /usage card would be a silent regression. With
    the current three-provider roster this resolves to the stacked layout,
    which is exactly what the shell hardcoded before this module existed.
    """
    members = tuple(roster)
    return NotificationPlan(
        NotificationEvent.USAGE, layout_for(members), members,
        "quota-sentinel", "usage card covers the full roster",
    )


def plan_recovery(providers: Sequence[str]) -> Optional[NotificationPlan]:
    """Recovery is worth exactly ONE card; silence is the default.

    A pending debt that is repaid after an outage is news. A debt that is
    still unpaid is not: the retry phase runs on the launchd grid, and one
    card per grid point would turn a long provider outage into a spam
    channel. Returning None is therefore the common case, not an error.
    """
    members = tuple(providers)
    if not members:
        return None
    return NotificationPlan(
        NotificationEvent.RECOVERY, layout_for(members), members,
        "quota-sentinel-recovered",
        f"debt repaid for {len(members)} provider(s)",
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
