"""Threat: the enemy can hurt you where nobody is looking.

docs/design.md, section 3. Before this, a flight nobody was watching always
came home -- and once the war ran with DCS closed, blue could not lose it. The
audit that found the offline clock missing ran forty thousand paper seconds,
flew two packages, flattened the target and recorded `airframes lost = 0`.

What is pinned here:

  * a threat site is its own kind of entity -- strikable and instantiable,
    never picked as a strike target;
  * an unobserved flight is rolled against every live enemy envelope its
    route enters, once per aircraft per site, before it releases anything;
  * an observed flight is never rolled: DCS shoots at it, snapshots report;
  * paper losses go through the attrition tracker, so the squadron's books
    balance exactly as they do for observed ones;
  * and, the audit's headline reversed, blue now loses aircraft offline.
"""

from __future__ import annotations

import random
import unittest

from campaign.api import PAPER_STEP
from campaign.attrition import (
    ATTRIBUTION_UNOBSERVED,
    CAUSE_UNOBSERVED,
    KIND_FLIGHT,
    KIND_TARGET,
    KIND_THREAT,
    AttritionTracker,
)
from campaign.campaign import Campaign
from campaign.oob import launch_range
from campaign.planner import DESTROYED, OPEN_STATES, select_target
from campaign.protocol import GroupSnapshot, Spawn, StateReport
from campaign.resolver import ExposureOutcome, resolve_exposure
from campaign.theater import Theater, ThreatSite, build_slice_theater
from tests.test_domain import OBSERVER_AT_TARGET, FakeDCS, drive
from tests.test_offline import observe, run_steps, say_hello

DEPOT = "latakia_fuel_depot"
SA6 = "latakia_north_sa6"
SQUADRON = "vfa_incirlik_f16"
SEAD_SQUADRON = "vfa_incirlik_f16_sead"

#: Seeds pinned against the slice's placeholder Pk, found by flying blue's
#: first offline sortie: seed 3 gets through untouched, seed 8 loses one
#: strike aircraft on the way in and nothing else, seed 2806 -- against an
#: SA-6 the HARM cannot out-range, see the test that uses it -- loses the
#: whole package, both SEAD jets and then both strikers behind them.
#: Re-pinned when red started planning, again when SEAD elements arrived,
#: and again when SEAD elements began firing from standoff: a SEAD element
#: that out-ranges the site it faces throws no exposure dice, so every
#: escorted TOT, red's before blue's, draws fewer than it did. Seed 2 now
#: loses nothing on blue's first sortie; 3 still loses nothing; 480 is no
#: longer a whole package lost. Each new seed is the first, in order, that
#: does what its name says.
#:
#: Re-pinned again when site units became typed (docs/design.md, section 6):
#: an anti-radiation missile that hits now destroys the battery's radar, and
#: a battery without one throws no dice at the strikers behind it -- red's
#: raid, a TOT before blue's, included, so every later draw moved. At seed 8
#: red's Kh-58Us take the Patriot's radar and blue's HARMs the SA-6's, and
#: blue's first sortie loses nothing; at 2806 the SEAD element's HARMs blind
#: the in-reach SA-6 before the strikers arrive and nobody is lost. Seed 35
#: is the first, in order, at which blue's first sortie loses one strike
#: aircraft and nothing else, and seed 729 the first at which it loses both
#: elements whole. Seed 3 still gets through untouched.
SEED_UNTOUCHED = 3
SEED_ONE_LOST = 35
SEED_BOTH_LOST = 729


def theater_with(**site_changes: object) -> Theater:
    theater = build_slice_theater()
    site = theater.threats[SA6]
    for key, value in site_changes.items():
        setattr(site, key, value)
    return theater


def without_the_sa6() -> Theater:
    """The slice with no SAM on blue's route.

    Only the SA-6 goes. Red's raid still meets the Patriot, and has to: a map
    without it would roll red's exposure differently, and the RNG comparisons
    below are about whether *blue's* route took dice.
    """
    theater = build_slice_theater()
    del theater.threats[SA6]
    return theater


def blue_packages(campaign: Campaign) -> list:
    return [p for p in campaign.packages.values() if p.coalition == "blue"]


def first_blue_package(campaign: Campaign):
    packages = blue_packages(campaign)
    return packages[0] if packages else None


def fly_first_sortie(campaign: Campaign) -> None:
    """Step until blue's first package has reached its TOT, and one step more."""
    while not blue_packages(campaign) or not first_blue_package(campaign).weapons_released:
        campaign.advance(PAPER_STEP)
        assert campaign.clock < 10_000, "the first sortie never reached its TOT"


def flight_losses(campaign: Campaign, coalition: str = "blue") -> list:
    """One side's aircraft losses. Red flies too now, and loses aircraft to
    the Patriot on paper; a test about blue's route must not count them."""
    return [
        x
        for x in campaign.tracker.losses
        if x.entity_kind == KIND_FLIGHT and x.coalition == coalition
    ]


def squadron(campaign: Campaign):
    return campaign.inventories["blue"].squadron(SQUADRON)


def sead_squadron(campaign: Campaign):
    return campaign.inventories["blue"].squadron(SEAD_SQUADRON)


def element_losses(campaign: Campaign, role: str) -> list:
    """Blue's aircraft losses from elements of one role (docs/design.md, 5)."""
    spawn_ids = {
        e.spawn_id for p in blue_packages(campaign) for e in p.elements if e.role == role
    }
    return [x for x in flight_losses(campaign) if x.spawn_id in spawn_ids]


# ---------------------------------------------------------------------------
# The roll
# ---------------------------------------------------------------------------


class TestResolveExposure(unittest.TestCase):
    def test_the_same_seed_gives_the_same_answer(self):
        a = resolve_exposure(aircraft=4, kill_probabilities=[0.3, 0.6], rng=random.Random(9))
        b = resolve_exposure(aircraft=4, kill_probabilities=[0.3, 0.6], rng=random.Random(9))
        self.assertEqual(a, b)

    def test_draws_depend_on_the_situation_never_on_the_dice(self):
        """Every aircraft is rolled against every site, dead or not.

        A flight shot down by the first site is still rolled by the second.
        If it were not, how many draws a sortie took would depend on how the
        first dice fell, and every later roll in the campaign would shift.
        """
        after = []
        for pk in (0.0, 0.5, 1.0):
            rng = random.Random(4)
            outcome = resolve_exposure(aircraft=3, kill_probabilities=[pk, pk], rng=rng)
            self.assertEqual(outcome.rolls, 6)
            after.append(rng.random())
        self.assertEqual(len(set(after)), 1, "draw count varied with the outcome")

    def test_certain_death_and_certain_safety(self):
        self.assertEqual(
            resolve_exposure(aircraft=2, kill_probabilities=[1.0], rng=random.Random(1)),
            ExposureOutcome(aircraft_lost=2, rolls=2),
        )
        self.assertEqual(
            resolve_exposure(aircraft=2, kill_probabilities=[0.0], rng=random.Random(1)),
            ExposureOutcome(aircraft_lost=0, rolls=2),
        )

    def test_no_site_in_range_takes_no_draws(self):
        rng = random.Random(5)
        state = rng.getstate()
        self.assertEqual(
            resolve_exposure(aircraft=2, kill_probabilities=[], rng=rng),
            ExposureOutcome(aircraft_lost=0, rolls=0),
        )
        self.assertEqual(rng.getstate(), state)


# ---------------------------------------------------------------------------
# The entity
# ---------------------------------------------------------------------------


class TestThreatSitesAreNotTargets(unittest.TestCase):
    def test_the_slice_route_runs_through_the_sa6_envelope(self):
        theater = build_slice_theater()
        route = (theater.airbases["incirlik"].pos, theater.targets[DEPOT].pos)
        self.assertEqual([s.id for s in theater.live_threats_along("red", *route)], [SA6])
        self.assertNotIn(SA6, theater.targets)

    def test_the_strike_planner_never_selects_one(self):
        theater = build_slice_theater()
        theater.targets.clear()
        # Even the only enemy entity left on the map is not a strike target.
        self.assertIsNone(select_target(theater, "red"))
        campaign = Campaign(theater=theater)
        run_steps(campaign, 200)
        self.assertEqual(campaign.packages, {}, "a strike was fragged against a SAM site")

    def test_it_is_instantiated_like_any_other_entity(self):
        campaign = Campaign()
        site = campaign.theater.threats[SA6]
        say_hello(campaign)
        frames = observe(campaign, 5.0, [site.pos])
        spawn = next(f for f in frames if isinstance(f, Spawn) and f.spawn_id == site.spawn_id)
        self.assertEqual(spawn.template, "SA-6_Kub_site")
        self.assertEqual(spawn.category, "ground")
        self.assertEqual(spawn.coalition, "red")
        self.assertEqual(spawn.units, 5)
        self.assertEqual(spawn.position, site.pos)
        self.assertEqual(spawn.tasking, {"kind": "air_defence"})

    def test_it_is_strikable_and_a_destroyed_site_rolls_nothing(self):
        """Killed in DCS, the site stops shooting on paper too."""
        campaign = Campaign(theater=theater_with(kill_probability=1.0))
        site = campaign.theater.threats[SA6]
        dcs = FakeDCS(campaign=campaign, deliver_events=False)
        dcs.connect(0.0)
        dcs.observers(0.0, [site.pos])
        self.assertIn(site.spawn_id, dcs.groups)
        dcs.groups[site.spawn_id].units = 0
        dcs.groups[site.spawn_id].alive = False
        dcs.state(30)

        self.assertTrue(site.destroyed)
        self.assertEqual(
            [x.entity_kind for x in campaign.tracker.losses], [KIND_THREAT] * 5
        )
        self.assertIn(site.spawn_id, {f.spawn_id for f in dcs.downlink if hasattr(f, "reason")})

        campaign.on_disconnect()
        fly_first_sortie(campaign)
        self.assertEqual(flight_losses(campaign), [], "a destroyed site shot down a flight")

    def test_it_survives_a_save_with_its_damage(self):
        campaign = Campaign()
        campaign.tracker.record_unobserved(
            campaign.theater.threats[SA6].spawn_id, 2, 0.0, unit_type="Kub 2P25 ln"
        )
        campaign.theater.threats[SA6].units_alive = 3
        reloaded = Campaign.from_dict(campaign.to_dict())
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())
        self.assertIsInstance(reloaded.theater.threats[SA6], ThreatSite)
        self.assertEqual(reloaded.theater.threats[SA6].units_alive, 3)


# ---------------------------------------------------------------------------
# Paper exposure
# ---------------------------------------------------------------------------


class TestPaperExposure(unittest.TestCase):
    def test_an_unobserved_flight_through_a_live_envelope_takes_losses(self):
        campaign = Campaign(seed=SEED_ONE_LOST)
        fly_first_sortie(campaign)
        package = first_blue_package(campaign)

        losses = flight_losses(campaign)
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0].cause, CAUSE_UNOBSERVED)
        self.assertEqual(losses[0].attribution, ATTRIBUTION_UNOBSERVED)
        self.assertEqual(losses[0].entity_id, package.id)
        self.assertEqual(campaign.tracker.units_alive(package.strike.spawn_id), 1)
        self.assertEqual(squadron(campaign).airframes_lost, 1)

    def test_a_route_that_misses_every_envelope_takes_none(self):
        """Certain death, placed where the route does not go."""
        far = theater_with(kill_probability=1.0, pos=(-60_000.0, 0.0, 120_000.0))
        missed = Campaign(theater=far, seed=SEED_ONE_LOST)
        fly_first_sortie(missed)
        self.assertEqual(flight_losses(missed), [])

        # And it took no dice either: the same war as a map with no SAM on
        # blue's route at all.
        bare = Campaign(theater=without_the_sa6(), seed=SEED_ONE_LOST)
        fly_first_sortie(bare)
        self.assertEqual(missed.rng.getstate(), bare.rng.getstate())

    def test_a_site_destroyed_before_the_sortie_rolls_nothing(self):
        dead = Campaign(theater=theater_with(kill_probability=1.0, units_alive=0), seed=SEED_ONE_LOST)
        fly_first_sortie(dead)
        self.assertEqual(flight_losses(dead), [])
        bare = Campaign(theater=without_the_sa6(), seed=SEED_ONE_LOST)
        fly_first_sortie(bare)
        self.assertEqual(dead.rng.getstate(), bare.rng.getstate())

    def test_an_instantiated_flight_is_never_paper_rolled(self):
        """DCS is holding the flight at its TOT, so DCS decides what it loses.

        The site here kills whatever it rolls against, so a single roll would
        show up as a loss -- and not one die may be thrown for the flight at
        all: nothing else in the campaign rolls at this TOT (the depot is
        instantiated too, and red's raid reaches its own TOT about fifteen
        seconds earlier), so the RNG must come through it untouched.
        """
        campaign = Campaign(theater=theater_with(kill_probability=1.0))
        seen: dict[str, object] = {}

        def watch(c: Campaign, t: int) -> None:
            package = first_blue_package(c)
            if package is None:
                return
            if not package.weapons_released:
                seen["before"] = c.rng.getstate()
                seen["held_at_tot"] = c.tracker.is_instantiated(package.strike.spawn_id)
            elif "after" not in seen:
                seen["after"] = c.rng.getstate()

        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[],
            deliver_events=False,
            start=0,
            duration=1_800,
            hook=watch,
        )
        self.assertTrue(seen.get("held_at_tot"), "the flight was not instantiated; vacuous")
        self.assertIn("after", seen, "the flight never reached its TOT")
        self.assertEqual(seen["after"], seen["before"], "an observed flight was rolled on paper")
        self.assertEqual(flight_losses(campaign), [])

    def test_losses_on_ingress_reduce_the_weapons_released(self):
        partly = Campaign(seed=SEED_ONE_LOST)
        fly_first_sortie(partly)
        sqn = squadron(partly)
        self.assertEqual(sqn.munitions_expended.get("GBU-38", 0), 2, "the dead jet bombed")
        self.assertEqual(sqn.munitions_lost.get("GBU-38", 0), 2)

        # Seed 53: red's first raid, which reaches Incirlik twenty seconds
        # before blue reaches the depot, misses. The last line below asserts
        # that no target anywhere took a loss, and it can only mean what it
        # says -- that blue's dead jets bombed nothing -- in a war where red
        # has not hit anything yet either. The default seed was one until
        # SEAD elements changed red's dice, and seed 3 until they fired from
        # standoff. Since then a Pk of one is no longer certain death for
        # blue's strikers: the SEAD element survives the SA-6, which it
        # out-ranges, and two suppressors leave a quarter of the Pk. Seed 53
        # was the first at which both strikers die anyway and red misses,
        # until site units were typed (docs/design.md, section 6): a HARM that
        # hits now takes the SA-6's radar and a blind battery fires at no one,
        # so at 53 the strikers live and flatten the depot. Seed 369 is the
        # first, in order, at which no HARM hits, both strikers die, and red's
        # raid -- which blinded the Patriot -- misses the storage area.
        wholly = Campaign(theater=theater_with(kill_probability=1.0), seed=369)
        fly_first_sortie(wholly)
        sqn = squadron(wholly)
        self.assertEqual(sqn.munitions_expended.get("GBU-38", 0), 0)
        self.assertEqual(sqn.munitions_lost.get("GBU-38", 0), 4)
        depot = wholly.theater.targets[DEPOT]
        self.assertEqual(depot.units_alive, depot.units_initial, "a flight shot down inbound hit the target")
        self.assertFalse([x for x in wholly.tracker.losses if x.entity_kind == KIND_TARGET])

    def test_a_flight_lost_whole_on_paper_closes_out_like_any_other(self):
        """Both elements shot down on paper, and each closes out on its own.

        Against an SA-6 whose envelope reaches as far as the AGM-88C: the SEAD
        element can be lost on paper only to a site it cannot out-range, and
        the slice's own SA-6 it out-ranges (docs/design.md, section 5).
        """
        campaign = Campaign(
            theater=theater_with(engagement_radius=launch_range("AGM-88C")),
            seed=SEED_BOTH_LOST,
        )
        frames: list = []
        while not blue_packages(campaign) or not first_blue_package(campaign).weapons_released:
            frames.extend(campaign.advance(PAPER_STEP))
        package = campaign.packages["pkg0001"]
        self.assertEqual(package.coalition, "blue")

        self.assertEqual(package.state, DESTROYED)
        self.assertEqual(package.strike.state, DESTROYED)
        self.assertNotIn(package.strike.spawn_id, campaign.tracker.groups)
        sqn = squadron(campaign)
        self.assertNotIn(package.strike.reservation_id, sqn.open_reservations)
        self.assertEqual(sqn.airframes_lost, 2)
        sqn.check_invariant()
        self.assertIn(f"{package.callsign} is lost.", [getattr(f, "text", "") for f in frames])
        # Its SEAD element went the same way, and closed out the same way.
        self.assertEqual(package.sead.state, DESTROYED)
        self.assertNotIn(package.sead.spawn_id, campaign.tracker.groups)
        self.assertNotIn(package.sead.reservation_id, sead_squadron(campaign).open_reservations)
        self.assertEqual(sead_squadron(campaign).airframes_lost, 2)
        self.assertIn(
            f"{package.name_of(package.sead)} is lost.", [getattr(f, "text", "") for f in frames]
        )
        # And the war goes on: blue's next package is tasked in the same pulse.
        self.assertTrue([p for p in blue_packages(campaign) if p.state in OPEN_STATES])

    def test_conservation_holds_through_paper_flight_losses(self):
        """A deadlier site, a long war, the books checked at every step.

        Seed 370, not the 4 this flew before SEAD elements fired from
        standoff, the 14 before SEAD, nor the 5 before red planned: red's
        raids end most wars within two or three blue sorties, a suppressed
        SA-6 kills fewer strikers, and since red's SEAD element stopped dying
        to the Patriot red's raids end them sooner still. At 4 blue now wins at
        4000 s and its strike element loses nothing. Seed 370 is the
        first, in order, at which it loses at least four: the war runs to
        11295 s and it loses six.

        Re-pinned from 370 when site units became typed (docs/design.md,
        section 6): the first HARM to hit now blinds the SA-6, and the slice
        never repairs it, so every sortie after that crosses a battery that
        cannot fire. At 370 that happens early enough that blue's strike
        element loses three, and red wins at 8925 s. Seed 1632 is the first,
        in order, at which it still loses at least four: five, before red
        wins at 6435 s with the SA-6's radar never hit.
        """
        campaign = Campaign(theater=theater_with(kill_probability=0.5), seed=1632)
        sqn = squadron(campaign)
        sead = sead_squadron(campaign)
        for _ in range(12_000):
            campaign.advance(PAPER_STEP)
            sqn.check_invariant()
            sead.check_invariant()
            for package in blue_packages(campaign):
                held = package.strike.reservation_id in sqn.open_reservations
                self.assertEqual(held, package.strike.is_open, package.id)
                if package.sead is not None:
                    held = package.sead.reservation_id in sead.open_reservations
                    self.assertEqual(held, package.sead.is_open, package.id)
                self.assertEqual(
                    package.state in OPEN_STATES,
                    any(e.is_open for e in package.elements),
                    package.id,
                )

        lost = element_losses(campaign, "strike")
        self.assertGreaterEqual(len(lost), 4, "too few losses to prove anything")
        self.assertEqual(sqn.airframes_lost, len(lost))
        # Every one of them died inbound, carrying its bombs.
        self.assertEqual(sqn.munitions_lost.get("GBU-38", 0), 2 * len(lost))
        self.assertEqual(
            sqn.airframes_available + sqn.airframes_lost, sqn.airframes_total,
            "an airframe is still reserved by a finished war",
        )
        # And the SEAD squadron answers for its own, missiles included.
        sead_lost = element_losses(campaign, "sead")
        self.assertEqual(sead.airframes_lost, len(sead_lost))
        self.assertEqual(sead.munitions_lost.get("AGM-88C", 0), 2 * len(sead_lost))
        self.assertEqual(
            sead.airframes_available + sead.airframes_lost, sead.airframes_total,
            "a SEAD airframe is still reserved by a finished war",
        )

    def test_the_audits_headline_reversed_blue_loses_aircraft_offline(self):
        """Forty thousand paper seconds on the slice as shipped.

        The audit's run of this ended `airframes lost = 0`, because nothing on
        the map could shoot. It used the default seed, and so did this test
        until red started planning. Since then the default seed's war is won
        in two blue sorties that both slip past the SA-6 -- about an even
        chance at the placeholder Pk -- so it no longer shows the one thing
        this test is for. Seed 9 was a war blue won *and* paid for, until
        SEAD elements changed every seed's dice; then seed 2, until SEAD
        elements fired from standoff and red's raids, no longer losing their
        SEAD element to the Patriot, began winning most wars by 3945 s. Seed
        48 is the first, in order, that blue wins (at 4000 s) and its strike
        element pays for. Whether both sides pay is TestBothSidesFight's
        business.

        Re-pinned from 48 when site units became typed (docs/design.md,
        section 6): a HARM hit now blinds the SA-6 instead of taking a
        launcher, and at 48 red's raids win at 3945 s with blue's strikers
        untouched. Seed 118 is the first, in order, at which blue wins (at
        4000 s) and its strike element loses an aircraft.
        """
        campaign = Campaign(seed=118)
        run_steps(campaign, int(40_000 / PAPER_STEP))
        sqn = squadron(campaign)
        self.assertGreaterEqual(sqn.airframes_lost, 1, "blue cannot lose a war it does not watch")
        self.assertEqual(sqn.airframes_lost, len(element_losses(campaign, "strike")))
        self.assertEqual(sead_squadron(campaign).airframes_lost, len(element_losses(campaign, "sead")))
        self.assertTrue(all(x.cause == CAUSE_UNOBSERVED for x in flight_losses(campaign)))
        self.assertTrue(campaign.theater.targets[DEPOT].destroyed, "and the war is still winnable")
        sqn.check_invariant()


# ---------------------------------------------------------------------------
# Paper damage is the tracker's damage
# ---------------------------------------------------------------------------


class TestPaperDamageReachesTheTracker(unittest.TestCase):
    def test_a_target_damaged_on_paper_spawns_with_what_survived(self):
        """docs/design.md, section 1: a spawn carries the current unit count.

        Paper damage used to go onto the ledger and the theater but not the
        tracker, and a spawn reads its count from the tracker -- so a depot
        half-flattened offline came back into DCS whole.
        """
        campaign = Campaign(seed=SEED_UNTOUCHED)
        depot = campaign.theater.targets[DEPOT]
        while depot.units_alive == depot.units_initial:
            campaign.advance(PAPER_STEP)
            self.assertLess(campaign.clock, 20_000, "the depot was never hit")
        self.assertFalse(depot.destroyed, "flattened in one pass; pick another seed")

        say_hello(campaign)
        frames = observe(campaign, 5.0, OBSERVER_AT_TARGET)
        spawn = next(f for f in frames if isinstance(f, Spawn) and f.spawn_id == depot.spawn_id)
        self.assertEqual(spawn.units, depot.units_alive)
        self.assertEqual(campaign.tracker.units_alive(depot.spawn_id), depot.units_alive)

    def test_the_tracker_refuses_a_paper_loss_for_something_dcs_is_holding(self):
        tracker = AttritionTracker()
        tracker.track("a1", entity_id="pkg", entity_kind=KIND_FLIGHT, coalition="blue", units_initial=2)
        tracker.mark_instantiated("a1", 0.0)
        self.assertEqual(tracker.record_unobserved("a1", 2, 10.0), [])
        self.assertEqual(tracker.units_alive("a1"), 2)
        self.assertEqual(tracker.losses, [])

        tracker.mark_removed("a1")
        self.assertEqual(len(tracker.record_unobserved("a1", 5, 10.0)), 2, "not clamped")
        self.assertFalse(tracker.is_alive("a1"))

    def test_a_snapshot_after_a_paper_loss_does_not_book_it_twice(self):
        """Paper, then observed: one aircraft, one loss."""
        tracker = AttritionTracker()
        tracker.track("a1", entity_id="pkg", entity_kind=KIND_FLIGHT, coalition="blue", units_initial=2)
        tracker.record_unobserved("a1", 1, 10.0)
        tracker.mark_instantiated("a1", 20.0)
        revealed = tracker.ingest(
            StateReport(seq=1, t=60.0, groups=[GroupSnapshot(spawn_id="a1", alive=True, units=1, units_initial=1)])
        )
        self.assertEqual(revealed, [])
        self.assertEqual(len(tracker.losses), 1)


if __name__ == "__main__":
    unittest.main()
