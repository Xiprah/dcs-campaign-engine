"""Packages: a set of flights with one TOT, and the SEAD element.

docs/design.md, section 5. A package is a set of elements -- a strike, and a
SEAD element when the strike route is exposed and there are anti-radiation
missiles to send -- each its own entity with its own spawn id, reservation
and bubble membership, so each can fail on its own.

What is pinned here:

  * the shape: two elements, two spawn ids, two reservations, one TOT, the
    SEAD element ahead on the same track;
  * when a SEAD element is attached, and when it is not;
  * the paper order at the TOT -- SEAD exposure, SEAD missiles, suppression,
    strike exposure, release -- with the dice scripted so that every draw is
    accounted for and every outcome is forced;
  * standoff: a SEAD element is shot at on paper only by a site its missile
    does not out-range, and on the slice as shipped both sides out-range;
  * a SEAD element shot down, on paper or in DCS, and the strike flying on;
  * a destroyed or suppressed site costing the strike less;
  * every mixed-authority combination at the TOT, with the sim having spent
    none, some or all of the SEAD element's missiles before it, and none of
    them resolving one aircraft, one site unit or one missile twice;
  * the players told their own package's composition and never the enemy's;
  * the books, missiles included, through a long war;
  * and the question the whole thing exists to answer: is SEAD worth flying,
    for the strikers and, from standoff, for the package as a whole?

Scripted dice: `Dice` is a `random.Random` whose `random()` returns the values
it is given, in order, and fails the test on a draw nobody scripted. The
resolver draws only through `random()`, and the planner's callsign draw goes
through `getrandbits`, which `Dice` leaves alone. Every scripted test also
checks the script was used up exactly, so it pins how many dice were thrown,
not only what they said.
"""

from __future__ import annotations

import random
import unittest
from unittest import mock

from campaign.api import PAPER_STEP
from campaign.attrition import (
    CAUSE_UNOBSERVED,
    KIND_FLIGHT,
    KIND_THREAT,
)
from campaign.campaign import Campaign
from campaign.oob import ANTI_RADIATION_MUNITIONS, build_slice_oob, launch_range
from campaign.planner import (
    ABORTED,
    COMPLETE,
    DESTROYED,
    ENROUTE,
    OPEN_STATES,
    ROLE_SEAD,
    ROLE_STRIKE,
    SEAD_LEAD,
    build_package,
    element_position,
)
from campaign.protocol import (
    PROTOCOL_VERSION,
    Ack,
    GroupSnapshot,
    Hello,
    Message,
    Observer,
    ObserverReport,
    Spawn,
    StateReport,
    group_name,
)
from campaign.resolver import (
    SEAD_SUPPRESSION_PER_AIRCRAFT,
    suppressed_kill_probability,
)
from campaign.theater import Theater, build_slice_theater, enemy_of, ground_distance
from tests.test_domain import OBSERVER_AT_TARGET, OBSERVER_FAR_AWAY, Damage, drive
from tests.test_single_element import strike_only_oob

DEPOT = "latakia_fuel_depot"
STORAGE = "incirlik_munitions_storage"
SA6 = "latakia_north_sa6"
PATRIOT = "incirlik_patriot"
STRIKE_SQN = "vfa_incirlik_f16"
SEAD_SQN = "vfa_incirlik_f16_sead"

#: The SA-6's placeholder Pk is 0.15. A die of 0.1 kills an aircraft at that
#: Pk, and spares it at the Pk two surviving SEAD aircraft leave (0.0375), so
#: the strike element's losses say whether suppression applied.
KILLS_UNLESS_SUPPRESSED = 0.1
SURVIVES = 0.99
HITS = 0.0


class Dice(random.Random):
    """A Random whose `random()` is scripted. Unscripted draws fail loudly."""

    def __init__(self, values: list[float]) -> None:
        super().__init__(0)
        self.script = list(values)
        self.drawn = 0

    def random(self) -> float:
        if not self.script:
            raise AssertionError(f"draw {self.drawn + 1} was not scripted")
        self.drawn += 1
        return self.script.pop(0)

    def getrandbits(self, k: int) -> int:
        # Defined here, not inherited, on purpose: a subclass that overrides
        # random() alone has choice() routed through random() by
        # Random.__init_subclass__, and the planner's callsign draw would eat
        # the script.
        return super().getrandbits(k)


def slice_with(**changes) -> Theater:
    theater = build_slice_theater()
    for entity_id, fields in changes.items():
        entity = theater.targets.get(entity_id) or theater.threats.get(entity_id)
        for key, value in fields.items():
            setattr(entity, key, value)
    return theater


#: The AGM-88C's launch range. An SA-6 that reaches this far cannot be
#: out-ranged by it, so blue's SEAD element has to fly into the envelope to
#: fire -- the one situation in which section 5's paper order has a SEAD
#: exposure step. The slice as shipped is the other one (its SA-6 reaches
#: 24 km), and `TestStandoff` pins that.
HARM_RANGE = launch_range("AGM-88C")


#: The slice's SA-6 rebuilt as a battery whose every unit is its own radar,
#: as a Tor's is (`theater.SITE_UNIT_TYPES`). Since docs/design.md, section
#: 7, an anti-radiation missile destroys only an emitter, so against the
#: real SA-6 -- one radar, four launchers -- the first hit blinds the battery
#: and no later one destroys anything. The tests that count missiles by the
#: units they take, and then fly the strike against what is left, are
#: flown against this battery instead: every hit is a unit, and the battery
#: engages until its last one, as every site did before units were typed.
#: Same position, radius and Pk; `units_alive` after `template`, because a
#: bare count re-types the battery for the template it now has.
ALL_EMITTERS = {"template": "SA-15_Tor_site", "units_alive": 5}


def sa6_in_reach(**fields) -> Theater:
    """The slice with an SA-6 the HARM does not out-range, and `fields` on it.

    Exactly the launch range, because the rule is that a site is out-ranged
    only by a missile that reaches *further* than its envelope.
    """
    return slice_with(**{SA6: {"engagement_radius": HARM_RANGE, **fields}})


def blue_package(campaign: Campaign):
    return next(p for p in sorted(campaign.packages.values(), key=lambda p: p.id)
                if p.coalition == "blue")


def red_package(campaign: Campaign):
    return next(p for p in sorted(campaign.packages.values(), key=lambda p: p.id)
                if p.coalition == "red")


def losses_of(campaign: Campaign, spawn_id: str) -> list:
    return [x for x in campaign.tracker.losses if x.spawn_id == spawn_id]


def texts(frames: list) -> list[str]:
    return [f.text for f in frames if isinstance(f, Message)]


def to_the_brink(campaign: Campaign) -> list:
    """Paper-step until blue's first TOT is one step away. Red's is behind us.

    Red's raid reaches its TOT at 1454 s and blue's package at 1471 s, so the
    step from 1470 to 1475 resolves blue's TOT and nothing else: the dice
    scripted for that step are blue's alone.
    """
    frames: list = []
    campaign.advance(PAPER_STEP)
    package = blue_package(campaign)
    while campaign.clock + PAPER_STEP < package.t_tot:
        frames.extend(campaign.advance(PAPER_STEP))
    red = red_package(campaign)
    assert red.weapons_released, "red's TOT is not behind us; the dice would be shared"
    assert not package.weapons_released
    return frames


def resolve_blue_tot(campaign: Campaign, script: list[float]) -> tuple[list, Dice]:
    """Take the one step that resolves blue's TOT, on scripted dice."""
    dice = Dice(script)
    campaign.rng = dice
    frames = campaign.advance(PAPER_STEP)
    assert blue_package(campaign).weapons_released, "the step did not reach the TOT"
    return frames, dice


def hold(campaign: Campaign, *spawn_ids: str) -> None:
    """DCS acknowledges these spawns: exactly what `on_ack` does with an ok."""
    for spawn_id in spawn_ids:
        campaign.tracker.mark_instantiated(spawn_id, campaign.clock)
        campaign._new_instantiation(spawn_id)


#: What a two-ship of SEAD jets carries in the sim once its pylons are
#: loaded: the two missiles an aircraft the engine reserves.
FULL_LOAD = 4


def share_the_sim(campaign: Campaign, package, site, spent: int) -> None:
    """DCS holds the SEAD element and the site together, the element fires
    `spent` missiles there and every one misses, and then DCS lets both go.

    The only way the engine may learn what was fired: a snapshot, carrying
    the spawn-time reading and what is aboard now.
    """
    hold(campaign, package.sead.spawn_id, site.spawn_id)
    left = FULL_LOAD - spent
    campaign.on_state(
        StateReport(
            seq=8,
            t=campaign.clock,
            groups=[
                GroupSnapshot(
                    spawn_id=package.sead.spawn_id, alive=True, units=2, units_initial=2,
                    ammo={"AGM-88C": left} if left else {},
                    ammo_initial={"AGM-88C": FULL_LOAD},
                ),
                GroupSnapshot(spawn_id=site.spawn_id, alive=True, units=5, units_initial=5,
                              unit_types=dict(site.units_by_type)),
            ],
        )
    )
    campaign.tracker.mark_removed(package.sead.spawn_id)
    campaign.tracker.mark_removed(site.spawn_id)


def _ledger_count(campaign: Campaign, entity_id: str) -> int:
    return len([x for x in campaign.tracker.losses if x.entity_id == entity_id])


def standing(campaign: Campaign) -> dict[str, tuple[int, int]]:
    """Each target's and site's (units alive, ledger entries), to measure from."""
    entities = list(campaign.theater.targets.values()) + list(campaign.theater.threats.values())
    return {e.id: (e.units_alive, _ledger_count(campaign, e.id)) for e in entities}


def conserved_everywhere(
    test: unittest.TestCase,
    campaign: Campaign,
    start: dict[str, tuple[int, int]] | None = None,
) -> None:
    """Books balance, and nothing died twice or died unrecorded.

    Every flight's ledger entries are exactly the aircraft it no longer has;
    every target's and site's new entries are exactly the units it has lost
    since `start` (by default, since the map was built).
    """
    for inventory in campaign.inventories.values():
        inventory.check_invariant()
    for group in campaign.tracker.groups.values():
        if group.entity_kind != KIND_FLIGHT:
            continue
        test.assertEqual(
            len(losses_of(campaign, group.spawn_id)),
            group.units_initial - group.units_alive,
            f"{group.spawn_id}: an aircraft died twice, or died unrecorded",
        )
    entities = list(campaign.theater.targets.values()) + list(campaign.theater.threats.values())
    for entity in entities:
        alive, booked = (entity.units_initial, 0) if start is None else start[entity.id]
        test.assertEqual(
            _ledger_count(campaign, entity.id) - booked,
            alive - entity.units_alive,
            f"{entity.id}: a unit died twice, or died unrecorded",
        )


# ---------------------------------------------------------------------------
# The shape of a package
# ---------------------------------------------------------------------------


class TestAPackageIsASetOfElements(unittest.TestCase):
    def test_strike_and_sead_are_two_entities_sharing_one_tot(self):
        campaign = Campaign()
        campaign.advance(PAPER_STEP)
        for package in (blue_package(campaign), red_package(campaign)):
            with self.subTest(coalition=package.coalition):
                self.assertEqual([e.role for e in package.elements], [ROLE_SEAD, ROLE_STRIKE])
                sead, strike = package.sead, package.strike
                self.assertNotEqual(sead.spawn_id, strike.spawn_id)
                self.assertNotEqual(sead.reservation_id, strike.reservation_id)
                self.assertNotEqual(sead.squadron_id, strike.squadron_id)
                inventory = campaign.inventories[package.coalition]
                self.assertIn(
                    strike.reservation_id,
                    inventory.squadron(strike.squadron_id).open_reservations,
                )
                self.assertIn(
                    sead.reservation_id,
                    inventory.squadron(sead.squadron_id).open_reservations,
                )
                self.assertIn(sead.munition, ANTI_RADIATION_MUNITIONS)
                self.assertNotIn(strike.munition, ANTI_RADIATION_MUNITIONS)
                # Each is tracked on its own, as a two-ship, under the package.
                for element in package.elements:
                    group = campaign.tracker.groups[element.spawn_id]
                    self.assertEqual(group.units_alive, 2)
                    self.assertEqual(group.entity_id, package.id)
                # One TOT: the strike's is the package's, and the SEAD element
                # is scheduled SEAD_LEAD ahead of it all the way round.
                self.assertEqual(package.t_tot, strike.t_tot)
                self.assertEqual(sead.t_tot, strike.t_tot - SEAD_LEAD)
                self.assertEqual(sead.t_takeoff, strike.t_takeoff - SEAD_LEAD)
                self.assertEqual(sead.t_rtb, strike.t_rtb - SEAD_LEAD)
                self.assertGreater(sead.t_takeoff, package.t_created)

    def test_the_sead_element_is_ahead_on_the_same_track(self):
        """Where the SEAD element is, the strike will be SEAD_LEAD later."""
        campaign = Campaign()
        campaign.advance(PAPER_STEP)
        package = blue_package(campaign)
        base = campaign.theater.airbases[package.base_id]
        target = campaign.theater.targets[package.target_id]
        for t in range(int(package.sead.t_takeoff), int(package.sead.t_rtb), 30):
            lead = element_position(package.sead, base, target, float(t))
            behind = element_position(package.strike, base, target, float(t) + SEAD_LEAD)
            self.assertLess(ground_distance(lead, behind), 1e-6, t)
        # And over the whole outbound leg it is the nearer to the target.
        for t in range(int(package.strike.t_takeoff), int(package.sead.t_tot), 60):
            self.assertLess(
                ground_distance(element_position(package.sead, base, target, float(t)), target.pos),
                ground_distance(element_position(package.strike, base, target, float(t)), target.pos),
                t,
            )

    def test_the_players_hear_their_own_composition_and_never_the_enemys(self):
        for player, own_target, enemy_target in (
            ("blue", "Latakia Fuel Depot", "Incirlik Munitions Storage"),
            ("red", "Incirlik Munitions Storage", "Latakia Fuel Depot"),
        ):
            with self.subTest(player=player):
                campaign = Campaign(player_coalition=player)
                said = texts(campaign.advance(PAPER_STEP))
                own = campaign.packages[
                    next(k for k, p in sorted(campaign.packages.items()) if p.coalition == player)
                ]
                self.assertEqual(
                    said,
                    [
                        f"{own.callsign} fragged: 2-ship strike with 2-ship SEAD on "
                        f"{own_target}, TOT {own.t_tot:.0f}."
                    ],
                )
                self.assertFalse([t for t in said if enemy_target in t])
                # And again on connecting: what is on task, by role.
                briefed = texts(
                    campaign.on_hello(
                        Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
                    )
                )
                self.assertEqual(
                    briefed,
                    [f"{own.callsign} on task: strike with SEAD on {own_target}, "
                     f"TOT {own.t_tot - campaign.mission_epoch:.0f}."],
                )

    def test_a_brief_leaves_out_an_element_no_longer_on_task(self):
        # An SA-6 the HARM cannot out-range: the only kind that gets a shot
        # at the SEAD element on paper.
        campaign = Campaign(theater=sa6_in_reach())
        to_the_brink(campaign)
        # SEAD element shot down whole, strike untouched.
        resolve_blue_tot(campaign, [HITS, HITS, SURVIVES, SURVIVES] + [SURVIVES] * 4)
        package = blue_package(campaign)
        self.assertEqual(package.sead.state, DESTROYED)
        briefed = texts(
            campaign.on_hello(Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria"))
        )
        self.assertIn(f"{package.callsign} on task: strike on Latakia Fuel Depot, "
                      f"TOT {package.t_tot - campaign.mission_epoch:.0f}.", briefed)

    def test_a_package_survives_a_save_element_by_element(self):
        campaign = Campaign()
        to_the_brink(campaign)
        reloaded = Campaign.from_dict(campaign.to_dict())
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())
        self.assertEqual(
            [e.to_dict() for e in blue_package(reloaded).elements],
            [e.to_dict() for e in blue_package(campaign).elements],
        )
        for _ in range(400):
            campaign.advance(PAPER_STEP)
            reloaded.advance(PAPER_STEP)
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())


# ---------------------------------------------------------------------------
# When a SEAD element is attached
# ---------------------------------------------------------------------------


class TestWhenSeadIsAttached(unittest.TestCase):
    def _first(self, campaign: Campaign, coalition: str = "blue"):
        campaign.advance(PAPER_STEP)
        return next(p for p in campaign.packages.values() if p.coalition == coalition)

    def test_an_exposed_route_with_missiles_gets_one_on_both_sides(self):
        campaign = Campaign()
        self.assertIsNotNone(self._first(campaign, "blue").sead)
        self.assertIsNotNone(next(p for p in campaign.packages.values()
                                  if p.coalition == "red").sead)

    def test_a_route_no_live_envelope_touches_gets_none(self):
        for label, theater in (
            ("no site", slice_with()),
            ("site elsewhere", slice_with(**{SA6: {"pos": (-60_000.0, 0.0, 120_000.0)}})),
            ("site destroyed", slice_with(**{SA6: {"units_alive": 0}})),
        ):
            if label == "no site":
                del theater.threats[SA6]
            with self.subTest(label):
                campaign = Campaign(theater=theater)
                package = self._first(campaign)
                self.assertEqual([e.role for e in package.elements], [ROLE_STRIKE])
                sead_sqn = campaign.inventories["blue"].squadron(SEAD_SQN)
                self.assertEqual(sead_sqn.open_reservations, {})
                self.assertEqual(sead_sqn.airframes_available, sead_sqn.airframes_total)

    def test_no_anti_radiation_missiles_means_none(self):
        campaign = Campaign(inventories=strike_only_oob())
        package = self._first(campaign)
        self.assertEqual([e.role for e in package.elements], [ROLE_STRIKE])

    def test_a_sead_squadron_run_dry_sends_the_strike_alone(self):
        for label, change in (
            ("no missiles", {"munitions_available": {"AGM-88C": 3},
                             "munitions_expended": {"AGM-88C": 21}}),
            ("no airframes", {"airframes_available": 1, "airframes_lost": 7}),
        ):
            with self.subTest(label):
                blue, red = build_slice_oob()
                sead = blue.squadron(SEAD_SQN)
                for key, value in change.items():
                    setattr(sead, key, value)
                sead.check_invariant()
                campaign = Campaign(inventories={"blue": blue, "red": red})
                package = self._first(campaign)
                self.assertEqual([e.role for e in package.elements], [ROLE_STRIKE])
                self.assertEqual(sead.open_reservations, {})

    def test_a_squadron_of_only_missiles_never_flies_a_strike(self):
        blue, red = build_slice_oob()
        strike = blue.squadron(STRIKE_SQN)
        strike.airframes_available, strike.airframes_lost = 0, 12
        campaign = Campaign(inventories={"blue": blue, "red": red})
        campaign.advance(PAPER_STEP)
        self.assertEqual(
            [p for p in campaign.packages.values() if p.coalition == "blue"], [],
            "the SEAD squadron was sent to bomb a fuel depot with HARMs",
        )
        self.assertEqual(blue.squadron(SEAD_SQN).open_reservations, {})

    def test_the_planner_attaches_one_only_when_told_the_route_is_exposed(self):
        theater = build_slice_theater()
        base, target = theater.airbase("incirlik"), theater.targets[DEPOT]
        issued: list[str] = []

        def next_id() -> str:
            issued.append(f"s{len(issued)}")
            return issued[-1]

        for threats, want in (([], [ROLE_STRIKE]), ([theater.threats[SA6]], [ROLE_SEAD, ROLE_STRIKE])):
            blue, _ = build_slice_oob()
            package = build_package(
                package_id="pkg0001", spawn_id="0005", inventory=blue, base=base,
                target=target, now=0.0, rng=random.Random(1), threats=threats,
                next_spawn_id=next_id,
            )
            self.assertEqual([e.role for e in package.elements], want)
        # A spawn id is drawn only for an element actually attached.
        self.assertEqual(issued, ["s0"])

    def test_attaching_one_throws_no_dice(self):
        """RNG consumption is the situation's, never the dice's: planning a
        package draws its callsign and nothing else, escorted or not."""
        escorted, alone = Campaign(seed=7), Campaign(seed=7, inventories=strike_only_oob())
        escorted.advance(PAPER_STEP)
        alone.advance(PAPER_STEP)
        self.assertIsNotNone(blue_package(escorted).sead)
        self.assertIsNone(blue_package(alone).sead)
        self.assertEqual(escorted.rng.getstate(), alone.rng.getstate())
        self.assertEqual(blue_package(escorted).callsign, blue_package(alone).callsign)


# ---------------------------------------------------------------------------
# The paper order at the TOT
# ---------------------------------------------------------------------------


class TestThePaperTot(unittest.TestCase):
    """Section 5's paper order, every step of it, the SEAD exposure included.

    The SEAD element has an exposure step only against a site its missile
    cannot out-range, so these TOTs are flown against an SA-6 that reaches as
    far as the HARM (`sa6_in_reach`), and their dice are the dice they always
    were. The same order from standoff, on the slice as shipped, is
    `TestStandoff`'s.
    """

    def test_suppression_cuts_the_kill_probability_per_surviving_aircraft(self):
        self.assertEqual(suppressed_kill_probability(0.15, 0), 0.15)
        self.assertAlmostEqual(
            suppressed_kill_probability(0.15, 1), 0.15 * (1 - SEAD_SUPPRESSION_PER_AIRCRAFT)
        )
        self.assertAlmostEqual(
            suppressed_kill_probability(0.15, 2), 0.15 * (1 - SEAD_SUPPRESSION_PER_AIRCRAFT) ** 2
        )
        self.assertLess(suppressed_kill_probability(0.15, 2), suppressed_kill_probability(0.15, 1))
        self.assertLess(suppressed_kill_probability(0.15, 1), 0.15)

    def test_a_suppressed_site_spares_the_strikers_it_would_have_killed(self):
        """The same strike dice, with and without the SEAD element in front."""
        escorted = Campaign(theater=sa6_in_reach())
        to_the_brink(escorted)
        frames, dice = resolve_blue_tot(
            escorted,
            [SURVIVES, SURVIVES]                     # SEAD exposure: both through
            + [SURVIVES] * 4                         # four missiles, all miss
            + [KILLS_UNLESS_SUPPRESSED] * 2          # strike exposure
            + [SURVIVES] * 4,                        # two strikers' bombs, all miss
        )
        self.assertEqual(dice.script, [], "fewer draws than the order says")
        package = blue_package(escorted)
        self.assertEqual(losses_of(escorted, package.strike.spawn_id), [])
        self.assertEqual(losses_of(escorted, package.sead.spawn_id), [])
        self.assertIn(f"{package.callsign} off target, 2 aircraft egressing.", texts(frames))

        alone = Campaign(theater=sa6_in_reach(), inventories=strike_only_oob())
        to_the_brink(alone)
        _, dice = resolve_blue_tot(alone, [KILLS_UNLESS_SUPPRESSED] * 2)
        self.assertEqual(dice.script, [])
        self.assertEqual(len(losses_of(alone, blue_package(alone).strike.spawn_id)), 2)

    def test_a_site_the_missiles_destroy_does_not_fire_at_the_strikers(self):
        """And every missile is rolled, though the first one finished it.

        Seed 1, not the default. Red's raid resolves fifteen seconds before
        blue's TOT, and the threat-site loss below must be the SA-6's alone.
        Since red's SEAD element stands off from the Patriot it no longer dies
        on the way in, so at the default seed its Kh-58Us take two Patriot
        launchers first; at seed 1 they miss.
        """
        campaign = Campaign(seed=1, theater=sa6_in_reach(units_alive=1))
        to_the_brink(campaign)
        start = standing(campaign)
        site = campaign.theater.threats[SA6]
        self.assertFalse(site.destroyed)
        frames, dice = resolve_blue_tot(
            campaign,
            [SURVIVES, SURVIVES]        # SEAD exposure
            + [HITS] * 4                # four missiles: the first kills the last unit
            + [SURVIVES] * 4,           # no site left, so no strike exposure; bombs
        )
        self.assertEqual(dice.script, [], "a missile went unrolled, or a dead site fired")
        self.assertTrue(site.destroyed)
        package = blue_package(campaign)
        self.assertEqual(losses_of(campaign, package.strike.spawn_id), [])
        self.assertIn("Latakia North SA-6 destroyed.", texts(frames))
        (loss,) = [x for x in campaign.tracker.losses if x.entity_kind == KIND_THREAT]
        self.assertEqual((loss.entity_id, loss.cause), (SA6, CAUSE_UNOBSERVED))
        conserved_everywhere(self, campaign, start)

        # The same TOT with no SEAD element: the site is alive and fires.
        alone = Campaign(seed=1, theater=sa6_in_reach(units_alive=1),
                         inventories=strike_only_oob())
        to_the_brink(alone)
        _, dice = resolve_blue_tot(alone, [KILLS_UNLESS_SUPPRESSED] * 2)
        self.assertEqual(dice.script, [])
        self.assertEqual(len(losses_of(alone, blue_package(alone).strike.spawn_id)), 2)

    def test_draws_are_the_situations_never_the_dices(self):
        """Every phase draws a number fixed by the state it starts from.

        The site has one unit left, so the first missile to hit finishes it.
        All four are rolled whether the first one hits or none do. What the
        missiles did is then the situation the strike's exposure starts from:
        a destroyed site throws no dice at the strikers.
        """
        for label, missiles, strike_exposure in (
            ("first missile kills", [HITS, SURVIVES, SURVIVES, SURVIVES], []),
            ("last missile kills", [SURVIVES, SURVIVES, SURVIVES, HITS], []),
            ("every missile misses", [SURVIVES] * 4, [SURVIVES, SURVIVES]),
        ):
            with self.subTest(label):
                campaign = Campaign(theater=sa6_in_reach(units_alive=1))
                to_the_brink(campaign)
                _, dice = resolve_blue_tot(
                    campaign,
                    [SURVIVES, SURVIVES] + missiles + strike_exposure + [SURVIVES] * 4,
                )
                self.assertEqual(dice.script, [])
                self.assertEqual(dice.drawn, 2 + 4 + len(strike_exposure) + 4)

    def test_the_missiles_are_resolved_through_the_tracker(self):
        """A site damaged on paper comes into DCS with only what survived.

        Against `ALL_EMITTERS`: four hits on the real SA-6 take its radar and
        nothing more. What a respawned battery without its radar looks like
        is tests/test_typed_sites.py's.
        """
        campaign = Campaign(theater=sa6_in_reach(**ALL_EMITTERS))
        to_the_brink(campaign)
        resolve_blue_tot(campaign, [SURVIVES] * 2 + [HITS] * 4 + [SURVIVES] * 2 + [SURVIVES] * 4)
        site = campaign.theater.threats[SA6]
        self.assertEqual(site.units_alive, 1)
        self.assertEqual(campaign.tracker.units_alive(site.spawn_id), 1)
        campaign.on_hello(Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria"))
        frames = campaign.on_observer(
            ObserverReport(seq=2, t=5.0, observers=[Observer(id="p", pos=site.pos)])
        )
        spawn = next(f for f in frames if isinstance(f, Spawn) and f.spawn_id == site.spawn_id)
        self.assertEqual(spawn.units, 1)
        conserved_everywhere(self, campaign)


# ---------------------------------------------------------------------------
# Standoff
# ---------------------------------------------------------------------------


class TestStandoff(unittest.TestCase):
    """A SEAD element is exposed only to a site it cannot out-range.

    docs/design.md, section 5. An anti-radiation missile is fired from
    outside the envelope of a site it out-ranges, so on paper that site
    never gets a shot at the SEAD element; its missiles and its suppression
    still apply. The slice as shipped is such a slice on both sides, from
    the published figures (`oob.ANTI_RADIATION_LAUNCH_RANGE`, `theater`).
    """

    def test_both_sides_missiles_out_range_the_site_they_face(self):
        theater = build_slice_theater()
        blue, red = build_slice_oob()
        for inventory, site_id in ((blue, SA6), (red, PATRIOT)):
            with self.subTest(side=inventory.coalition):
                (munition,) = [
                    m for s in inventory.squadrons.values() for m in s.munitions_total
                    if m in ANTI_RADIATION_MUNITIONS
                ]
                site = theater.threats[site_id]
                self.assertTrue(
                    site.outranged_by(launch_range(munition)),
                    f"{munition} ({launch_range(munition):.0f} m) does not out-range "
                    f"{site.name} ({site.engagement_radius:.0f} m)",
                )

    def test_from_standoff_the_sead_element_throws_no_exposure_dice(self):
        """Section 5's order with step 1 empty: missiles, suppression, strike."""
        campaign = Campaign()
        to_the_brink(campaign)
        frames, dice = resolve_blue_tot(
            campaign,
            [SURVIVES] * 4                           # four missiles, all miss
            + [KILLS_UNLESS_SUPPRESSED] * 2          # strike exposure, suppressed
            + [SURVIVES] * 4,                        # two strikers' bombs
        )
        self.assertEqual(dice.script, [], "the SEAD element was rolled after all")
        self.assertEqual(dice.drawn, 4 + 2 + 4)
        package = blue_package(campaign)
        self.assertEqual(losses_of(campaign, package.sead.spawn_id), [])
        self.assertEqual(losses_of(campaign, package.strike.spawn_id), [])
        said = texts(frames)
        self.assertIn(f"{package.callsign} SEAD off target, 2 aircraft egressing.", said)
        self.assertIn(f"{package.callsign} off target, 2 aircraft egressing.", said)
        self.assertFalse([t for t in said if "air defences inbound" in t])
        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        self.assertEqual(sead.munitions_expended, {"AGM-88C": 4})
        self.assertEqual(sead.airframes_lost, 0)
        conserved_everywhere(self, campaign)

    def test_a_site_that_reaches_as_far_as_the_missile_gets_its_shot(self):
        """The boundary: out-ranged means the missile reaches *further*."""
        for radius, sead_dice in ((HARM_RANGE, 2), (HARM_RANGE - 1.0, 0)):
            with self.subTest(radius=radius):
                campaign = Campaign(theater=slice_with(**{SA6: {"engagement_radius": radius}}))
                to_the_brink(campaign)
                _, dice = resolve_blue_tot(campaign, [SURVIVES] * (sead_dice + 4 + 2 + 4))
                self.assertEqual(dice.script, [])
                self.assertEqual(dice.drawn, sead_dice + 4 + 2 + 4)

    def test_the_strike_element_still_flies_into_the_envelope(self):
        """Standoff is the SEAD element's: the strikers have to reach the target."""
        campaign = Campaign()
        to_the_brink(campaign)
        _, dice = resolve_blue_tot(
            campaign, [SURVIVES] * 4 + [HITS, HITS]  # missiles; certain strike deaths
        )
        self.assertEqual(dice.script, [])
        package = blue_package(campaign)
        self.assertEqual(len(losses_of(campaign, package.strike.spawn_id)), 2)
        self.assertEqual(losses_of(campaign, package.sead.spawn_id), [])

    def test_red_stands_off_from_the_patriot_too(self):
        """A Patriot that kills whatever it rolls against, and red's SEAD lives.

        The Kh-58U's published 250 km out-ranges the Patriot's published
        160 km, so red's SEAD element is never rolled against it; the same
        Patriot at the Kh-58U's own range kills both jets.
        """
        reach = launch_range("Kh-58U")
        for radius, sead_lost in ((None, 0), (reach, 2)):
            with self.subTest(radius=radius):
                fields = {"kill_probability": 1.0}
                if radius is not None:
                    fields["engagement_radius"] = radius
                campaign = Campaign(theater=slice_with(**{PATRIOT: fields}))
                while not any(p.coalition == "red" and p.weapons_released
                              for p in campaign.packages.values()):
                    campaign.advance(PAPER_STEP)
                    self.assertLess(campaign.clock, 5_000)
                package = red_package(campaign)
                self.assertEqual(len(losses_of(campaign, package.sead.spawn_id)), sead_lost)
                sead = campaign.inventories["red"].squadron(package.sead.squadron_id)
                self.assertEqual(sead.airframes_lost, sead_lost)
                self.assertEqual(sead.munitions_expended.get("Kh-58U", 0), 4 - 2 * sead_lost)
                conserved_everywhere(self, campaign)


# ---------------------------------------------------------------------------
# The parts fail independently
# ---------------------------------------------------------------------------


class TestTheSeadElementCanBeShotDown(unittest.TestCase):
    def test_on_paper_and_the_strike_flies_on_unsuppressed(self):
        # On paper only a site the HARM cannot out-range shoots at the SEAD
        # element at all; from standoff, this cannot happen.
        campaign = Campaign(theater=sa6_in_reach())
        to_the_brink(campaign)
        frames, dice = resolve_blue_tot(
            campaign,
            [HITS, HITS]                        # the SEAD element, both jets
            + [KILLS_UNLESS_SUPPRESSED, SURVIVES]  # nobody left to suppress
            + [SURVIVES] * 2,                   # one striker's bombs
        )
        self.assertEqual(dice.script, [])
        package = blue_package(campaign)
        self.assertEqual(package.sead.state, DESTROYED)
        self.assertEqual(len(losses_of(campaign, package.sead.spawn_id)), 2)
        self.assertEqual(len(losses_of(campaign, package.strike.spawn_id)), 1)
        self.assertEqual(package.strike.state, ENROUTE)
        self.assertEqual(package.state, ENROUTE)
        said = texts(frames)
        self.assertIn(f"{package.callsign} SEAD lost 2 aircraft to air defences inbound.", said)
        self.assertIn(f"{package.callsign} SEAD is lost.", said)
        self.assertIn(f"{package.callsign} off target, 1 aircraft egressing.", said)

        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        self.assertEqual(sead.airframes_lost, 2)
        self.assertEqual(sead.munitions_lost, {"AGM-88C": 4}, "the missiles died with the jets")
        self.assertEqual(sead.munitions_expended.get("AGM-88C", 0), 0)
        self.assertEqual(sead.open_reservations, {})
        strike = campaign.inventories["blue"].squadron(STRIKE_SQN)
        self.assertEqual(strike.munitions_expended, {"GBU-38": 2})
        self.assertEqual(strike.munitions_lost, {"GBU-38": 2})

        # The strike element gets home, and with it the package.
        while package.is_open:
            campaign.advance(PAPER_STEP)
        self.assertEqual(package.strike.state, COMPLETE)
        self.assertEqual(package.state, COMPLETE)
        conserved_everywhere(self, campaign)

    def test_in_dcs_before_the_tot_and_the_strike_flies_on(self):
        """Killed in the sim on the way in: a snapshot says so, nothing else."""
        campaign = Campaign()
        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[Damage(t=900, category="plane", remove=2, event="kill",
                            initiator="sam:kub_01", weapon="9M38", kind="sead",
                            coalition="blue")],
            deliver_events=True,
            start=0,
            duration=2_700,
        )
        package = blue_package(campaign)
        self.assertEqual(package.sead.state, DESTROYED)
        lost = losses_of(campaign, package.sead.spawn_id)
        self.assertEqual([x.t for x in lost], [900.0, 900.0])
        self.assertNotIn(CAUSE_UNOBSERVED, {x.cause for x in lost})
        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        self.assertEqual(sead.airframes_lost, 2)
        self.assertEqual(sead.munitions_lost, {"AGM-88C": 4})
        # The strike flew its sortie and came home with both jets.
        self.assertTrue(package.weapons_released)
        self.assertEqual(package.strike.state, COMPLETE)
        self.assertEqual(losses_of(campaign, package.strike.spawn_id), [])
        self.assertEqual(package.state, COMPLETE)
        conserved_everywhere(self, campaign)

    def test_a_scrubbed_sead_element_leaves_the_strike_to_fly(self):
        campaign = Campaign()
        campaign.advance(PAPER_STEP)
        package = blue_package(campaign)
        campaign.pending[99] = ("spawn", package.sead.spawn_id)
        frames = campaign.on_ack(Ack(seq=1, t=0.0, ref=99, ok=False, error="unknown template"))
        self.assertEqual(package.sead.state, ABORTED)
        self.assertEqual(package.strike.state, "planned")
        self.assertIn(package.state, OPEN_STATES)
        self.assertEqual(texts(frames), [f"{package.callsign} SEAD scrubbed: client rejected spawn."])
        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        self.assertEqual(sead.open_reservations, {})
        self.assertEqual(sead.airframes_available, sead.airframes_total)


# ---------------------------------------------------------------------------
# Mixed authority
# ---------------------------------------------------------------------------


class TestMixedAuthority(unittest.TestCase):
    """Every combination of who holds what at blue's TOT, and what the sim spent.

    The rule (docs/design.md, section 5): the SEAD element's paper effect on a
    site -- missiles rolled at it, and suppression of it for the strike -- is
    granted only when the SEAD element and the site are both outside DCS at
    the TOT, and then only with what the sim has not already spent: the
    missiles the engine reserved less those the snapshots showed leaving the
    sim. The aircraft that fire them, and only those, suppress. An element
    DCS holds is never flown through the sites on paper; a site DCS holds
    loses units only to snapshots.

    The table's rows, as each case below flies them:

      SEAD paper, site paper, the sim spent nothing  -> all four, suppressed
      SEAD paper, site paper, the sim spent some     -> the rest, suppressed
      SEAD paper, site paper, the sim spent all four -> none, full Pk
      SEAD paper, site held                          -> none, full Pk
      SEAD held, site either                         -> none, full Pk

    The dice are scripted so that each question has a visible answer: the
    SEAD element always survives its exposure, every missile hits, and each
    strike die kills an aircraft unless the site is suppressed -- by one
    aircraft or two, either is enough. The script is built from the rule and
    must be used up exactly, so a single die thrown at the wrong entity -- or
    not thrown -- fails the case.

    Each combination is flown twice: against an SA-6 the HARM cannot
    out-range, where a paper SEAD element flies its exposure, and against the
    slice's own, which it engages from standoff and so takes no exposure
    from, whoever holds the site. Standoff takes dice out of the SEAD
    element's exposure and nowhere else: the missiles, the suppression and
    the strike are the same in both.

    Both batteries are `ALL_EMITTERS`, so that "every missile hits" still
    means one unit a missile and a battery that still engages the strike:
    this is the rule for who may fire what, not the rule for what a missile
    kills (docs/design.md, section 7, and tests/test_typed_sites.py).
    """

    def _run(
        self,
        *,
        sead_held: bool,
        strike_held: bool,
        site_held: bool,
        spent: int = 0,
        standoff: bool = False,
    ):
        campaign = Campaign(
            theater=slice_with(**{SA6: ALL_EMITTERS}) if standoff
            else sa6_in_reach(**ALL_EMITTERS)
        )
        to_the_brink(campaign)
        package = blue_package(campaign)
        site = campaign.theater.threats[SA6]
        if spent:
            share_the_sim(campaign, package, site, spent)
        held = [
            sid for sid, yes in (
                (package.sead.spawn_id, sead_held),
                (package.strike.spawn_id, strike_held),
                (site.spawn_id, site_held),
            ) if yes
        ]
        hold(campaign, *held)

        paper_sead = not sead_held
        paper_rounds = FULL_LOAD - spent if paper_sead and not site_held else 0
        shooters = min(2, -(-paper_rounds // 2))
        paper_strike = not strike_held
        # A strike die of 0.1 kills unless the site was suppressed.
        strikers = 2 if (strike_held or shooters) else 0
        script = (
            ([SURVIVES, SURVIVES] if paper_sead and not standoff else [])
            + [HITS] * paper_rounds
            + ([KILLS_UNLESS_SUPPRESSED] * 2 if paper_strike else [])
            + [SURVIVES] * (2 * strikers)
        )
        frames, dice = resolve_blue_tot(campaign, script)
        self.assertEqual(dice.script, [], "fewer dice were thrown than the rule says")

        # What each entity lost, and by whose authority.
        self.assertEqual(losses_of(campaign, package.sead.spawn_id), [])
        self.assertEqual(len(losses_of(campaign, package.strike.spawn_id)), 2 - strikers)
        site_losses = [x for x in campaign.tracker.losses if x.entity_id == SA6]
        self.assertEqual(len(site_losses), paper_rounds)
        self.assertTrue(all(x.cause == CAUSE_UNOBSERVED for x in site_losses))
        self.assertEqual(site.units_alive, 5 - paper_rounds)
        self.assertEqual(package.sead.sim_spent, spent)
        # Every missile is booked once: what the sim spent when a snapshot
        # showed it gone, the rest at the TOT, whoever held what then.
        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        self.assertEqual(sead.munitions_expended, {"AGM-88C": 4})

        # Then the sim reports what it did to what it held: the site lost two
        # units to the SEAD element in DCS. A snapshot is the only source of
        # that, and it lands once, on top of nothing the paper did.
        if site_held:
            groups = [GroupSnapshot(spawn_id=site.spawn_id, alive=True, units=3, units_initial=5,
                                    unit_types={site.radar_type: 3})]
            groups += [
                GroupSnapshot(spawn_id=e.spawn_id, alive=True,
                              units=campaign.tracker.units_alive(e.spawn_id),
                              units_initial=e.flight_size)
                for e in package.elements
                if campaign.tracker.is_instantiated(e.spawn_id)
            ]
            campaign.on_state(StateReport(seq=9, t=campaign.clock, groups=groups))
            observed = [x for x in campaign.tracker.losses
                        if x.entity_id == SA6 and x.cause != CAUSE_UNOBSERVED]
            self.assertEqual(len(observed), 2)
            self.assertEqual(site.units_alive, 3)
        conserved_everywhere(self, campaign)
        return campaign

    def test_every_combination(self):
        for standoff in (False, True):
            for spent in (0, 1, FULL_LOAD):
                for sead_held in (False, True):
                    for strike_held in (False, True):
                        for site_held in (False, True):
                            with self.subTest(standoff=standoff, spent=spent,
                                              sead_held=sead_held,
                                              strike_held=strike_held,
                                              site_held=site_held):
                                self._run(sead_held=sead_held, strike_held=strike_held,
                                          site_held=site_held, spent=spent,
                                          standoff=standoff)

    def test_what_the_sim_spent_is_withheld_and_the_rest_is_fired(self):
        """Replaces the old all-or-nothing contact rule: sharing the sim with
        the site costs the paper exactly what the sim fired, no more."""
        for standoff in (False, True):
            for strike_held in (False, True):
                for spent in range(FULL_LOAD + 1):
                    with self.subTest(standoff=standoff, strike_held=strike_held,
                                      spent=spent):
                        campaign = self._run(sead_held=False, strike_held=strike_held,
                                             site_held=False, spent=spent,
                                             standoff=standoff)
                        site = campaign.theater.threats[SA6]
                        self.assertEqual(site.units_alive, 1 + spent)

    def test_what_the_sim_spent_is_booked_when_the_snapshot_shows_it(self):
        campaign = Campaign(theater=sa6_in_reach())
        to_the_brink(campaign)
        package = blue_package(campaign)
        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        share_the_sim(campaign, package, campaign.theater.threats[SA6], 3)
        self.assertEqual(sead.munitions_expended, {"AGM-88C": 3})
        self.assertEqual(sead.open_reservations[package.sead.reservation_id].rounds, 1)
        conserved_everywhere(self, campaign)

    def test_ammunition_after_the_tot_changes_nothing(self):
        """The paper has nothing left to withhold once the TOT is resolved."""
        campaign = Campaign(theater=sa6_in_reach())
        to_the_brink(campaign)
        package = blue_package(campaign)
        resolve_blue_tot(campaign, [SURVIVES] * (2 + 4 + 2 + 4))
        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        before = (dict(sead.munitions_expended), dict(sead.munitions_lost))
        hold(campaign, package.sead.spawn_id)
        campaign.on_state(StateReport(seq=9, t=campaign.clock, groups=[
            GroupSnapshot(spawn_id=package.sead.spawn_id, alive=True, units=2,
                          units_initial=2, ammo={}, ammo_initial={"AGM-88C": 4}),
        ]))
        self.assertEqual(package.sead.sim_spent, 0)
        self.assertEqual((dict(sead.munitions_expended), dict(sead.munitions_lost)), before)


class TestSpentThroughTheProtocol(unittest.TestCase):
    """The ammunition rule, driven by the real frames that carry it.

    The SEAD element shares the sim with the SA-6 early in its sortie --
    the observer sits on the battery -- and then the observer leaves, so at
    the TOT the element and the site are both on paper. Every missile hits
    and no site can kill, so what the SA-6 loses says how many missiles the
    paper fired -- the SA-6 rebuilt as `ALL_EMITTERS`, so that each hit is a
    unit lost rather than the first one blinding it.
    """

    def _war(self, *, early_contact: bool, loadouts=None, damages=()):
        campaign = Campaign(
            theater=slice_with(**{SA6: {**ALL_EMITTERS, "kill_probability": 0.0}})
        )
        site = campaign.theater.threats[SA6]
        start = [(site.pos[0], 100.0, site.pos[2])] if early_contact else OBSERVER_FAR_AWAY
        with mock.patch("campaign.campaign.ARM_PK", 1.0):
            dcs = drive(campaign, observer_positions=start, damages=list(damages),
                        deliver_events=False, start=0, duration=1_200,
                        loadouts=loadouts)
            drive(campaign, observer_positions=OBSERVER_FAR_AWAY, damages=[],
                  deliver_events=False, start=1_205, duration=400, dcs=dcs)
        return campaign

    def test_an_unarmed_element_that_shared_the_sim_early_fires_its_whole_load(self):
        """The case that retired the contact rule.

        In DCS today the pylons are empty, so the element spent nothing in
        the sim; the old rule took its whole sortie off the paper because it
        had been held near the battery once. Now it fires all four.
        """
        watched = self._war(early_contact=True)
        package = blue_package(watched)
        self.assertTrue(package.weapons_released)
        self.assertFalse(watched.tracker.is_instantiated(package.sead.spawn_id))
        self.assertEqual(watched.tracker.losses_for(package.sead.spawn_id), [])
        self.assertEqual(watched.theater.threats[SA6].units_alive, 1)
        self.assertEqual(package.sead.sim_spent, 0)

        unwatched = self._war(early_contact=False)
        package = blue_package(unwatched)
        self.assertTrue(package.weapons_released)
        self.assertEqual(unwatched.theater.threats[SA6].units_alive, 1)
        self.assertEqual(package.sead.sim_spent, 0)

    def test_what_it_fired_in_the_sim_it_does_not_fire_again(self):
        loaded = {"F-16C_sead_harm": {"AGM-88C": 2}}
        fired = Damage(t=1_200, category="plane", kind="sead", coalition="blue",
                       fire=1, munition="AGM-88C")
        campaign = self._war(early_contact=True, loadouts=loaded, damages=[fired])
        package = blue_package(campaign)
        self.assertTrue(package.weapons_released)
        # One fired in DCS and missed; the three left were fired on paper.
        self.assertEqual(campaign.theater.threats[SA6].units_alive, 2)
        self.assertEqual(package.sead.sim_spent, 1)
        sead = campaign.inventories["blue"].squadron(SEAD_SQN)
        self.assertEqual(sead.munitions_expended.get("AGM-88C", 0), 4)
        conserved_everywhere(self, campaign)

    def test_what_the_sim_spent_survives_a_save(self):
        loaded = {"F-16C_sead_harm": {"AGM-88C": 2}}
        fired = Damage(t=1_200, category="plane", kind="sead", coalition="blue",
                       fire=3, munition="AGM-88C")
        campaign = self._war(early_contact=True, loadouts=loaded, damages=[fired])
        reloaded = Campaign.from_dict(campaign.to_dict())
        self.assertEqual(blue_package(reloaded).sead.sim_spent, 3)
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())


class TestAFlightHomeAfterItsTotIsNotTaskedAgain(unittest.TestCase):
    """A strike resolved at its TOT must not strike again on the way home.

    The client hangs an attack on the last waypoint when there is no attack
    waypoint left, so a flight spawned after its TOT with its strike tasking
    intact would bomb its target a second time in DCS -- after the paper had
    already resolved its bombs. Found flying the harness with SEAD: red's
    strikers, now surviving the Patriot behind their SEAD element, flew home
    past the observer and struck Incirlik twice.
    """

    def test_every_element_spawned_after_the_tot_is_sent_home_untasked(self):
        campaign = Campaign()
        to_the_brink(campaign)
        campaign.advance(PAPER_STEP)
        package = blue_package(campaign)
        self.assertTrue(package.weapons_released)
        campaign.on_hello(Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria"))
        base = campaign.theater.airbases[package.base_id]
        target = campaign.theater.targets[package.target_id]
        where = element_position(package.strike, base, target, campaign.clock)
        frames = campaign.on_observer(
            ObserverReport(seq=2, t=5.0, observers=[Observer(id="p", pos=where)])
        )
        spawned = {f.spawn_id: f for f in frames if isinstance(f, Spawn)}
        for element in package.elements:
            self.assertIn(element.spawn_id, spawned, element.role)
            self.assertEqual(
                spawned[element.spawn_id].tasking,
                {"kind": "egress", "callsign": package.name_of(element)},
            )
            self.assertNotIn("attack", [w.action for w in spawned[element.spawn_id].route])

    def test_before_the_tot_each_is_tasked_for_its_role(self):
        campaign = Campaign()
        drive(campaign, observer_positions=OBSERVER_AT_TARGET, damages=[],
              deliver_events=False, start=0, duration=1_200)
        package = blue_package(campaign)
        site = campaign.theater.threats[SA6]
        spawns = {}
        for f in campaign.on_hello(Hello(seq=1, t=1200.0, protocol=PROTOCOL_VERSION,
                                         theater="Syria")):
            if isinstance(f, Spawn):
                spawns[f.spawn_id] = f
        self.assertEqual(spawns[package.sead.spawn_id].tasking, {
            "kind": "sead",
            "targets": [group_name(site.spawn_id)],
            "tot": package.sead.t_tot - campaign.mission_epoch,
            "callsign": f"{package.callsign} SEAD",
        })
        self.assertEqual(spawns[package.sead.spawn_id].template, "F-16C_sead_harm")
        self.assertEqual(spawns[package.strike.spawn_id].tasking["kind"], "strike")
        self.assertEqual(spawns[package.strike.spawn_id].tasking["callsign"], package.callsign)


# ---------------------------------------------------------------------------
# The books
# ---------------------------------------------------------------------------


class TestConservationThroughALongWar(unittest.TestCase):
    """Every squadron, every step, missiles included, on the slice as shipped.

    Beyond the invariant, the missiles are reconciled against the ledger: a
    SEAD aircraft lost at the TOT takes its two missiles down with it, and
    every one that got through fired its two.
    """

    def test_the_missiles_balance_against_the_ledger(self):
        for seed in (0, 2, 4, 9):
            with self.subTest(seed=seed):
                campaign = Campaign(seed=seed)
                for _ in range(int(20_000 / PAPER_STEP)):
                    campaign.advance(PAPER_STEP)
                    for inventory in campaign.inventories.values():
                        inventory.check_invariant()
                for coalition in ("blue", "red"):
                    escorted = [
                        p for p in campaign.packages.values()
                        if p.coalition == coalition and p.sead is not None
                    ]
                    self.assertTrue(escorted, "no SEAD element flew; vacuous")
                    sead = campaign.inventories[coalition].squadron(escorted[0].sead.squadron_id)
                    (munition,) = sead.munitions_total
                    lost = sum(len(losses_of(campaign, p.sead.spawn_id)) for p in escorted)
                    fired = sum(
                        p.sead.flight_size - len(losses_of(campaign, p.sead.spawn_id))
                        for p in escorted
                        if p.sead.weapons_released
                    )
                    self.assertEqual(sead.airframes_lost, lost)
                    self.assertEqual(sead.munitions_lost.get(munition, 0), 2 * lost)
                    self.assertEqual(sead.munitions_expended.get(munition, 0), 2 * fired)
                    self.assertEqual(sead.open_reservations, {})
                conserved_everywhere(self, campaign)


# ---------------------------------------------------------------------------
# The point of it
# ---------------------------------------------------------------------------


def first_sorties(seed: int, inventories) -> dict[str, dict]:
    """Each side's first sortie: what its strike element and its whole
    package lost, and whether its SEAD element (if any) out-ranged every site
    the route met."""
    campaign = Campaign(seed=seed, inventories=inventories)
    flown: dict = {}
    while len(flown) < 2:
        campaign.advance(PAPER_STEP)
        for package in sorted(campaign.packages.values(), key=lambda p: p.id):
            if package.coalition not in flown and package.weapons_released:
                flown[package.coalition] = package
        assert campaign.clock < 10_000, "a first sortie never reached its TOT"
    out = {}
    for side, package in flown.items():
        # Every enemy site on the route, standing or not: the content decides
        # standoff, and a site the missiles destroyed was out-ranged too.
        base = campaign.theater.airbases[package.base_id]
        target = campaign.theater.targets[package.target_id]
        sites = [
            site for site in campaign.theater.threats.values()
            if site.coalition == enemy_of(side) and site.covers(base.pos, target.pos)
        ]
        out[side] = {
            "strike": len(losses_of(campaign, package.strike.spawn_id)),
            "package": sum(len(losses_of(campaign, e.spawn_id)) for e in package.elements),
            "standoff": package.sead is not None and all(
                site.outranged_by(launch_range(package.sead.munition)) for site in sites
            ),
        }
    return out


def first_sortie_strike_losses(seed: int, inventories) -> dict[str, int]:
    """Strike-element aircraft each side lost on its first sortie."""
    return {side: sortie["strike"] for side, sortie in first_sorties(seed, inventories).items()}


class TestSeadIsWorthFlying(unittest.TestCase):
    """The goal: SEAD is worth flying, for the strikers and for the package.

    The same hundred seeds, each side's first sortie -- the one moment both
    configurations fly the same situation into the same site -- once on the
    slice as shipped and once without a single anti-radiation missile.

    Two claims. The strike element loses fewer aircraft escorted: at the
    placeholder numbers (Pk 0.15, suppression halving it per surviving SEAD
    aircraft) 3 against 26 for blue and 4 against 26 for red -- 7 and 10
    before site units were typed, when a missile that hit took a launcher
    rather than the battery's radar (docs/design.md, section 7). And, where the
    SEAD element's missile out-ranges the site it faces, the whole package
    loses fewer aircraft escorted than the strike alone does: the SEAD
    element fires from standoff and takes no exposure, so it costs no
    airframes on paper. Before standoff that second claim was false -- the
    SEAD element flew into the site at its full kill probability first, and
    escorted packages lost 46 (blue) and 44 (red) aircraft over these seeds
    against 26 and 26 alone. A side whose missile does not out-range the site
    it faces still flies into it, and for that side the claim is not made:
    its subtest is skipped and says so, rather than asserting something the
    model does not promise. On the slice as shipped both sides out-range.
    """

    SEEDS = range(100)

    @classmethod
    def setUpClass(cls) -> None:
        cls.escorted = [first_sorties(seed, None) for seed in cls.SEEDS]
        cls.alone = [first_sorties(seed, strike_only_oob()) for seed in cls.SEEDS]

    def test_escorted_strikers_lose_fewer_aircraft(self):
        escorted = {"blue": 0, "red": 0}
        alone = {"blue": 0, "red": 0}
        for seed in range(len(self.SEEDS)):
            for side, sortie in self.escorted[seed].items():
                escorted[side] += sortie["strike"]
            for side, sortie in self.alone[seed].items():
                alone[side] += sortie["strike"]
        for side in ("blue", "red"):
            with self.subTest(side=side):
                self.assertGreater(alone[side], 0, "nobody was ever shot down; vacuous")
                self.assertLess(
                    escorted[side], alone[side],
                    f"{side} strikers lost {escorted[side]} aircraft escorted and "
                    f"{alone[side]} alone over {len(self.SEEDS)} first sorties",
                )

    def test_from_standoff_an_escorted_package_loses_fewer_aircraft_in_total(self):
        for side in ("blue", "red"):
            with self.subTest(side=side):
                standoff = {sorties[side]["standoff"] for sorties in self.escorted}
                self.assertEqual(len(standoff), 1, "the same content out-ranged differently")
                if standoff != {True}:
                    self.skipTest(f"{side}'s SEAD missile does not out-range the site it faces")
                escorted = sum(sorties[side]["package"] for sorties in self.escorted)
                alone = sum(sorties[side]["package"] for sorties in self.alone)
                self.assertGreater(alone, 0, "nobody was ever shot down; vacuous")
                self.assertLess(
                    escorted, alone,
                    f"{side} packages lost {escorted} aircraft escorted and {alone} "
                    f"alone over {len(self.SEEDS)} first sorties",
                )


if __name__ == "__main__":
    unittest.main()
