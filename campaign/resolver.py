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

The same rule runs the other way at the same instant. A strike flight DCS is
not holding at its TOT is flown through the enemy's air defences on paper
(:func:`resolve_exposure`) before it releases anything, so an unwatched sortie
can be lost as well as won. A SEAD element flies first, and its missiles are
rolled here too (:func:`resolve_strike` at :data:`ARM_PK`); what it buys the
strikers is a lower kill probability (:func:`suppressed_kill_probability`),
never a different roll. A SEAD element whose missile out-ranges a site fires
from outside its envelope and is never rolled against it: the caller leaves
that site out of the list it passes :func:`resolve_exposure`. When SEAD may
act on paper at all is docs/design.md, section 5.

**Dice.** How many draws a resolution takes is decided by the situation --
the geometry, the content, and what earlier steps left alive -- and never by
how the dice in it fall. An out-ranged site is content, so the rolls it no
longer takes are a different situation, not a different outcome.

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


@dataclass(frozen=True)
class ExposureOutcome:
    """What an unwatched flight's pass through enemy air defences cost it."""

    aircraft_lost: int
    rolls: int


def resolve_exposure(
    *,
    aircraft: int,
    kill_probabilities: list[float],
    rng: random.Random,
) -> ExposureOutcome:
    """Roll a flight's exposure to the threat sites along its route.

    `kill_probabilities` has one entry per site whose envelope the flight
    has to enter, in the order the sites are to fire. Every site rolls once
    for every aircraft, and an aircraft is lost if any site's roll against
    it succeeds.

    Every aircraft is rolled against every site even once it is already dead,
    so the draws taken from `rng` are exactly `aircraft * len(sites)` whatever
    the dice say -- the same discipline as :func:`resolve_strike`, for the same
    reason: an early kill must not shift every later roll in the campaign.

    TODO(threat-model): the probabilities are flat per-site placeholders. A
    real model -- altitude bands, terrain masking, EW, per-type envelopes --
    decides the numbers passed in here; this function only has to roll them.
    The suppression a SEAD element buys (docs/design.md, section 5) already
    arrives that way, through :func:`suppressed_kill_probability`.
    """
    if aircraft <= 0:
        return ExposureOutcome(aircraft_lost=0, rolls=0)
    lost = [False] * aircraft
    for pk in kill_probabilities:
        for i in range(aircraft):
            if rng.random() < pk:
                lost[i] = True
    return ExposureOutcome(
        aircraft_lost=sum(lost), rolls=aircraft * len(kill_probabilities)
    )


#: Probability that one anti-radiation missile removes one unit of the site it
#: is fired at. Lower than a bomb's: an ARM guides on an emitter rather than an
#: aimpoint, and a radar that shuts down in time is not where it homes.
# TODO(threat-model): a real model kills the radar in particular, and a site
# without one stops shooting. Units here are counted, not typed, so an ARM
# kill costs the site whichever unit a respawn would drop -- a launcher.
ARM_PK = 0.25

#: Fraction of a site's kill probability each surviving SEAD aircraft takes
#: away from the strike element behind it. Suppression compounds per
#: aircraft, so a two-ship that got through leaves a quarter of the site's Pk
#: and a SEAD element that lost a jet leaves half: the parts of a package fail
#: independently (docs/design.md, section 5), and a SEAD element that is only
#: half there only half does its job. A flat placeholder like the Pk it cuts.
SEAD_SUPPRESSION_PER_AIRCRAFT = 0.5


def suppressed_kill_probability(kill_probability: float, suppressors: int) -> float:
    """A site's kill probability with `suppressors` SEAD aircraft on it.

    With none, the site's own number comes back untouched -- not multiplied by
    one -- so a package without a SEAD element rolls exactly the probability
    it always did. Draws nothing: suppression changes what the dice are rolled
    against, never how many are rolled.
    """
    if suppressors <= 0:
        return kill_probability
    return kill_probability * (1.0 - SEAD_SUPPRESSION_PER_AIRCRAFT) ** suppressors
