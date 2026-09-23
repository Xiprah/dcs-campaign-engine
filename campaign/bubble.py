"""What DCS is allowed to know about.

The bubble is the whole reason the campaign can be larger than a mission. The
engine simulates everything on paper and instantiates only what a human is
close enough to see. Getting the *membership* rule right matters more than the
radius: a naive single-radius test makes an observer sitting on the boundary
spawn and despawn the same group on every observer frame, which in DCS means a
group destroyed and recreated several times a minute -- lost damage state, lost
AI task progress, and a frame hitch each time.

So membership is hysteretic. A group must come within `spawn_radius` to be
instantiated, but once instantiated it survives out to a strictly larger
`despawn_radius`. Between the two radii, membership depends on what it already
was, which is exactly what kills the thrash.

Both radii are parameters. Burying them in this module would mean the campaign
could not tune the bubble per mission, and would make the hysteresis untestable
without monkeypatching.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set as AbstractSet

from campaign.protocol import Vec3
from campaign.theater import ground_distance


class BubbleConfigError(ValueError):
    """Radii that would not produce hysteresis."""


def nearest_observer_distance(pos: Vec3, observers: Sequence[Vec3]) -> float:
    """Ground distance to the closest observer, or infinity if there are none.

    No observers means no humans in the mission, which means nothing needs to
    exist in DCS at all -- infinity is the honest answer, not zero.
    """
    if not observers:
        return float("inf")
    return min(ground_distance(pos, o) for o in observers)


def resolve_bubble(
    observers: Sequence[Vec3],
    candidates: Mapping[str, Vec3],
    live: AbstractSet[str],
    *,
    spawn_radius: float,
    despawn_radius: float,
) -> frozenset[str]:
    """The set of entity ids that should be instantiated in DCS right now.

    Pure: same inputs, same output, no clock and no state of its own. `live`
    is the *current* membership and is what makes the result hysteretic.

    A candidate that is not offered at all -- a flight that has landed, a
    target that has been flattened -- is simply absent from the result, so the
    caller's diff against `live` produces its despawn without a special case.
    """
    if despawn_radius <= spawn_radius:
        raise BubbleConfigError(
            f"despawn_radius {despawn_radius} must be strictly greater than "
            f"spawn_radius {spawn_radius}; equal radii reintroduce the thrash "
            f"the hysteresis exists to prevent"
        )
    members = set()
    for entity_id, pos in candidates.items():
        distance = nearest_observer_distance(pos, observers)
        threshold = despawn_radius if entity_id in live else spawn_radius
        if distance <= threshold:
            members.add(entity_id)
    return frozenset(members)


def bubble_delta(
    live: AbstractSet[str], wanted: AbstractSet[str]
) -> tuple[list[str], list[str]]:
    """(to_spawn, to_despawn), each sorted so frame order is deterministic."""
    return sorted(wanted - live), sorted(live - wanted)


def observer_positions(observers: Iterable[object]) -> list[Vec3]:
    """Pull Vec3s out of protocol Observer records.

    Kept here so the bubble's only contact with the wire format is one line,
    and `resolve_bubble` stays a function of plain positions.
    """
    return [o.pos for o in observers]  # type: ignore[attr-defined]
