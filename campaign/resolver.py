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
rolled here too (:func:`resolve_arm`); what it buys the
strikers is a lower kill probability (:func:`suppressed_kill_probability`),
never a different roll. A SEAD element whose missile out-ranges a site fires
from outside its envelope and is never rolled against it: the caller leaves
that site out of the list it passes :func:`resolve_exposure`, and so does a
site whose radar is gone or off the air, which cannot engage at all
(docs/design.md, section 7). When SEAD may act on paper at all is docs/design.md, section 5.

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


#: Probability that one anti-radiation missile hits home on the radar of the
#: site it is fired at -- destroying it, or, far more often, forcing it off
#: the air (`ARM_DESTROY_FRACTION`). Lower than a bomb's: an ARM guides on an
#: emitter rather than an aimpoint, and a radar that shuts down in time is
#: not where it homes. Site units are typed (docs/design.md, section 7), so
#: a kill is the radar, never a launcher. A flat placeholder.
ARM_PK = 0.25

#: Of the missiles that home on a radar, the fraction that destroy it rather
#: than make it shut down (docs/design.md, section 7). Derived from the one
#: published count of fired against destroyed found: in Allied Force "US and
#: NATO aircraft fired at least 743 HARMs", and NATO could "confirm the
#: destruction of only three of Serbia's approximately 25 known mobile SA-6
#: batteries" (Benjamin S. Lambeth, "Kosovo and the Continuing SEAD
#: Challenge", Aerospace Power Journal; mirrored at
#: ausairpower.net/APJ-Lambeth-Mirror.html). Three destroyed of 743 fired is
#: 0.4% a missile; at ARM_PK a hit, 1.6% of hits. Every bias in that count
#: makes it low -- only SA-6 batteries counted, only confirmed kills, and
#: Serbia's operators were unusually disciplined about emissions -- so it is
#: a floor, not an estimate, and is used as it stands rather than raised by a
#: guess.
ARM_DESTROY_FRACTION = (3 / 743) / ARM_PK

#: Seconds a radar stays off the air after an anti-radiation missile forced
#: it down: ten minutes. A GAME-DESIGN CHOICE, not a researched figure: no
#: published post-shutdown time was found (Lambeth describes Serb operators
#: emitting for 20 seconds and then going quiet, a firing tactic, not this).
#: Ten minutes outlasts the package behind the missiles: the paper track's
#: 140 m/s crosses the largest envelope on the Syria map, 50 km, in six. The
#: radar is then simply back on; nothing was broken, so nothing is repaired.
ARM_SHUTDOWN_TIME = 600.0


@dataclass(frozen=True)
class ArmOutcome:
    """What an unwatched SEAD element's missiles did to one site."""

    radars_destroyed: int
    shut_down: bool
    rounds_rolled: int


def resolve_arm(
    *,
    rounds: int,
    radars_alive: int,
    rng: random.Random,
    pk: float | None = None,
    destroy_fraction: float | None = None,
) -> ArmOutcome:
    """Roll anti-radiation missiles at one site, one die each.

    A die below `pk * destroy_fraction` destroys a radar; below `pk`, it
    forces the battery off the air; above, it misses. One die a missile
    carries both the hit and the destroy-or-shutdown decision, so the draws
    are exactly `rounds` whatever they say -- the same discipline as
    :func:`resolve_strike`. A missile has to home on an emitter: once a hit
    has shut the battery down, or every radar is gone, the missiles after it
    find nothing and do nothing, though each is still rolled.
    """
    # Read here, not bound as defaults, so the module's figures are the ones
    # in force when the missiles fly.
    pk = ARM_PK if pk is None else pk
    destroy_fraction = ARM_DESTROY_FRACTION if destroy_fraction is None else destroy_fraction
    if rounds <= 0:
        return ArmOutcome(radars_destroyed=0, shut_down=False, rounds_rolled=0)
    destroyed = 0
    dark = False
    for _ in range(rounds):
        die = rng.random()
        if dark or destroyed >= radars_alive or die >= pk:
            continue
        if die < pk * destroy_fraction:
            destroyed += 1
        else:
            dark = True
    return ArmOutcome(
        radars_destroyed=destroyed,
        shut_down=dark and destroyed < radars_alive,
        rounds_rolled=rounds,
    )


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
