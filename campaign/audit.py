"""The reconciliation rule, as something a machine can check.

docs/protocol.md says events may enrich a loss and may never create or
withhold one. That is a property of the saved campaign, so it is checkable:
strip every event-derived field from two saves and the rest must match, byte
for byte, whether or not a single `event` frame was ever delivered.

Exactly two things in a save come from events, and both are named here rather
than inferred, so that adding a third without thinking about it shows up as a
failing test instead of as a campaign that has quietly started counting kills.
"""

from __future__ import annotations

import copy
from typing import Any

#: Where the tracker parks its unconsumed attribution hints.
HINTS_KEY = "attribution_hints"

#: The one field of a loss record an event is allowed to reach.
ATTRIBUTION_KEY = "attribution"


def strip_event_derived(state: dict[str, Any]) -> dict[str, Any]:
    """`state` with everything an `event` frame could have touched removed.

    Takes and returns a :meth:`campaign.campaign.Campaign.to_dict` mapping,
    and does not modify the original.
    """
    out = copy.deepcopy(state)
    tracker = out.get("tracker")
    if not isinstance(tracker, dict):
        return out
    tracker.pop(HINTS_KEY, None)
    for loss in tracker.get("losses", []):
        loss.pop(ATTRIBUTION_KEY, None)
    return out


def campaigns_agree(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when two saves are the same campaign apart from attribution."""
    return strip_event_derived(a) == strip_event_derived(b)


def attributions(state: dict[str, Any]) -> list[str]:
    """Every loss's attribution, in ledger order. The half that events own."""
    tracker = state.get("tracker") or {}
    return [loss.get(ATTRIBUTION_KEY, "") for loss in tracker.get("losses", [])]


__all__ = [
    "ATTRIBUTION_KEY",
    "HINTS_KEY",
    "attributions",
    "campaigns_agree",
    "strip_event_derived",
]
