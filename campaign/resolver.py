"""Resolving what happened where nobody was looking.

The reconciliation rule says a `state` snapshot is the only thing that may
record a loss. That rule is about entities DCS actually instantiated. An entity
outside the bubble is never instantiated, never appears in a snapshot, and so --
under a literal reading -- can never be touched at all.

That literal reading did not leave a feature missing, it left the campaign
broken. Driven with one observer parked far from the war, which is the ordinary
state of a dedicated server, the engine fragged twelve packages, spent the
squadron's entire stock of GBU-38s, destroyed nothing, recorded no losses, and
then stalled permanently -- holding a save that passed every invariant check
while describing a war that could not continue.

So the rule needs its precise form:

    A loss for an entity DCS instantiated comes only from a snapshot.
    A loss for an entity DCS never instantiated comes only from here.

Never both, and the choice is made once, at the instant of weapons release, by
asking whether the target is instantiated right then. A target hit on paper and
spawned later comes into the world already carrying its damage, because a spawn
sends the target's current `units_alive`.

This is also the one place the campaign's seeded RNG earns its keep. Every roll
here comes off `Campaign.rng`, whose state round-trips through the save, so an
unobserved war is as replayable as an observed one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

#: Probability that one released weapon removes one target unit. A flat number
#: standing in for a real weaponeering model.
# TODO(weaponeering): per-weapon/per-target-class Pk, plus the aimpoint count
# that decides how many units one pass can service, belong behind this.
DEFAULT_WEAPON_PK = 0.45


@dataclass(frozen=True)
class StrikeOutcome:
    """What an unwatched strike achieved."""

    units_killed: int
    rounds_rolled: int

    @property
    def missed(self) -> bool:
        return self.units_killed == 0


def resolve_strike(
    *,
    rounds: int,
    target_units_alive: int,
    rng: random.Random,
    weapon_pk: float = DEFAULT_WEAPON_PK,
) -> StrikeOutcome:
    """Roll an unobserved strike outcome.

    Every round is rolled even once the target has nothing left to lose, so the
    number of draws taken from `rng` depends only on how many weapons were
    released -- never on the target's state. A resolver whose RNG consumption
    varied with the outcome would make the whole campaign's replay diverge from
    the first over-killed target onward.
    """
    if rounds <= 0 or target_units_alive <= 0:
        return StrikeOutcome(units_killed=0, rounds_rolled=max(0, rounds))
    killed = 0
    for _ in range(rounds):
        hit = rng.random() < weapon_pk
        if hit and killed < target_units_alive:
            killed += 1
    return StrikeOutcome(units_killed=killed, rounds_rolled=rounds)
