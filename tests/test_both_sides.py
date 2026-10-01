"""The enemy: both sides fight.

docs/design.md, section 4. Until this, only `player_coalition` ever planned.
Red's inventory was initialised and read by nothing, and the one red threat
that ever shot down a blue jet in early testing was scripted by the test
harness, not the engine. A war with one side is a target range.

What is pinned here:

  * every side with squadrons and an airbase plans -- with nobody connected,
    and whichever side the humans fly;
  * each side holds at most one open package, and both hold one at once;
  * red's raids face blue's air defences through the same paper path blue's
    strikes face red's, can be shot down there, and are never rolled while
    DCS is holding them;
  * both sides' books balance through a long war;
  * the war ends when one side's strategic targets are gone, either way, and
    the humans are told the right thing, and nothing after it;
  * and the headline: left alone for a day, both sides lose aircraft and both
    sides destroy something.

The helpers find a package's side through the inventory that owns its
squadron rather than through `Package.coalition`, so that these tests fail on
their assertions -- not on an AttributeError -- against an engine in which
red does not plan.
"""

from __future__ import annotations

import unittest

from campaign.api import PAPER_STEP
from campaign.attrition import CAUSE_UNOBSERVED, KIND_FLIGHT, KIND_TARGET
from campaign.campaign import Campaign
from campaign.oob import ANTI_RADIATION_MUNITIONS, launch_range
from campaign.planner import ABORTED, DESTROYED, OPEN_STATES, PLANNED
from campaign.protocol import Message
from campaign.theater import Theater, build_slice_theater
from tests.test_domain import drive

DEPOT = "latakia_fuel_depot"
STORAGE = "incirlik_munitions_storage"
SA6 = "latakia_north_sa6"
PATRIOT = "incirlik_patriot"
BASSEL = "bassel_al_assad"

WON = "All assigned strategic targets destroyed."
LOST = "All our strategic targets have been destroyed. The war is lost."
DRAWN = (
    "Every strategic target on both sides has been destroyed. "
    "The war ends without a victor."
)

#: Seeds pinned by flying the slice as shipped, offline, to its end: at seed
#: 3 blue wins at 6525 s, at seed 6 red wins at 3945 s with blue's depot
#: still standing. Re-pinned when SEAD elements arrived: they draw dice at
#: every escorted TOT, so every seed's war fell differently, and the default
#: seed became a red win (3945 s). Re-pinned again when SEAD elements began
#: firing from standoff: an element that out-ranges the site it faces draws
#: no exposure dice, so every escorted TOT draws fewer. Seed 3 still ends in
#: a blue win, now at 6525 s rather than 4000 s. Seed 1 still ends in a red
#: win at 6435 s, but blue's third package, already airborne, flattens the
#: depot 90 s later; seed 6 is the first, in order, at which red wins and the
#: depot survives. The default seed is now a blue win, at 4000 s.
SEED_BLUE_WINS = 3
SEED_RED_WINS = 6
#: A day of war on the slice as shipped in which each side's strike element
#: loses aircraft to the other's air defences and each side's bombs destroy
#: part of the other's target. Found by flying seeds in order: at seed 20 red
#: wins at 3945 s after both strike elements have lost a jet. Re-pinned from
#: 9 with SEAD, and from 2 with standoff, for the reasons above: at 2 neither
#: strike element now loses anything. With neither SEAD element exposed and
#: both sites suppressed, a strike element that loses a jet is rarer than it
#: was, and seed 20 is the first at which both sides' do.
SEED_BOTH_BLEED = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def side_of(campaign: Campaign, package) -> str | None:
    for coalition in sorted(campaign.inventories):
        if package.strike.squadron_id in campaign.inventories[coalition].squadrons:
            return coalition
    return None


def packages_of(campaign: Campaign, coalition: str) -> list:
    return [p for p in campaign.packages.values() if side_of(campaign, p) == coalition]


def open_by_side(campaign: Campaign) -> dict[str, int]:
    counts: dict[str, int] = {}
    for package in campaign.packages.values():
        if package.state in OPEN_STATES:
            side = side_of(campaign, package) or "?"
            counts[side] = counts.get(side, 0) + 1
    return counts


def squadrons_of(campaign: Campaign, coalition: str) -> list:
    inventory = campaign.inventories.get(coalition)
    return [] if inventory is None else list(inventory.squadrons.values())


def flight_losses(campaign: Campaign, coalition: str) -> list:
    return [
        x for x in campaign.tracker.losses
        if x.entity_kind == KIND_FLIGHT and x.coalition == coalition
    ]


def element_losses(campaign: Campaign, coalition: str, role: str) -> list:
    """One side's aircraft losses from elements of one role.

    A package is its elements now (docs/design.md, section 5), and a side's
    SEAD element loses aircraft to the same sites its strike does. What these
    tests pinned about "the raid" is the strike element, and they still pin
    exactly that.
    """
    spawn_ids = {
        element.spawn_id
        for package in packages_of(campaign, coalition)
        for element in package.elements
        if element.role == role
    }
    return [x for x in flight_losses(campaign, coalition) if x.spawn_id in spawn_ids]


def strike_squadron(campaign: Campaign, coalition: str):
    """The side's strike squadron: the one that is not loaded with ARMs."""
    (squadron,) = [
        s for s in squadrons_of(campaign, coalition)
        if not set(s.munitions_total) <= ANTI_RADIATION_MUNITIONS
    ]
    return squadron


def sead_squadron(campaign: Campaign, coalition: str):
    (squadron,) = [
        s for s in squadrons_of(campaign, coalition)
        if set(s.munitions_total) <= ANTI_RADIATION_MUNITIONS
    ]
    return squadron


def texts(frames: list) -> list[str]:
    return [f.text for f in frames if isinstance(f, Message)]


def run(campaign: Campaign, seconds: float, frames: list | None = None) -> list:
    out = [] if frames is None else frames
    for _ in range(int(seconds / PAPER_STEP)):
        out.extend(campaign.advance(PAPER_STEP))
    return out


def fly_until(campaign: Campaign, predicate, limit: float, frames: list | None = None) -> list:
    """Step on paper until `predicate` holds, or fail with the clock it reached."""
    out = [] if frames is None else frames
    while not predicate(campaign):
        if campaign.clock >= limit:
            raise AssertionError(f"not reached by campaign time {campaign.clock:.0f}")
        out.extend(campaign.advance(PAPER_STEP))
    return out


def first_red_raid_released(campaign: Campaign) -> bool:
    red = packages_of(campaign, "red")
    return bool(red) and red[0].weapons_released


def slice_with(**changes) -> Theater:
    """The slice as shipped, with named entities changed: {id: {field: value}}."""
    theater = build_slice_theater()
    for entity_id, fields in changes.items():
        entity = theater.targets.get(entity_id) or theater.threats.get(entity_id)
        assert entity is not None, f"{entity_id} is not on the map"
        for key, value in fields.items():
            setattr(entity, key, value)
    return theater


def conserved(test: unittest.TestCase, campaign: Campaign) -> None:
    """Every squadron's books, and every element's reservation, on both sides.

    Each element holds its own reservation for exactly as long as it is
    open, and a package is open exactly while one of its elements is.
    """
    for coalition in sorted(campaign.inventories):
        for squadron in squadrons_of(campaign, coalition):
            squadron.check_invariant()
    for package in campaign.packages.values():
        side = side_of(campaign, package)
        test.assertIsNotNone(side, package.id)
        for element in package.elements:
            squadron = campaign.inventories[side].squadrons[element.squadron_id]
            held = element.reservation_id in squadron.open_reservations
            test.assertEqual(held, element.is_open, element.reservation_id)
        test.assertEqual(
            package.state in OPEN_STATES,
            any(e.is_open for e in package.elements),
            package.id,
        )


# ---------------------------------------------------------------------------
# Both sides plan
# ---------------------------------------------------------------------------


class TestRedPlansAndFlies(unittest.TestCase):
    def test_red_plans_and_flies_with_no_client_connected(self):
        campaign = Campaign()
        self.assertFalse(campaign.connected)
        frames = run(campaign, PAPER_STEP)
        red = packages_of(campaign, "red")
        self.assertEqual(len(red), 1, "red planned nothing in its first pulse")
        package = red[0]
        target = campaign.theater.targets[package.target_id]
        self.assertEqual(target.coalition, "blue", "red fragged a strike on its own side")
        self.assertEqual(package.base_id, BASSEL)

        fly_until(campaign, first_red_raid_released, limit=5_000.0, frames=frames)
        self.assertIn(package.state, OPEN_STATES | {DESTROYED})
        squadron = campaign.inventories["red"].squadron(package.strike.squadron_id)
        spent = sum(squadron.munitions_expended.values()) + sum(squadron.munitions_lost.values())
        self.assertEqual(spent, package.strike.rounds, "the raid's bombs are not accounted for")
        squadron.check_invariant()
        # Nobody flies red, so red was told nothing -- not its fragging, not
        # its egress -- and nothing went to anyone but the humans' side.
        self.assertEqual({f.to for f in frames if isinstance(f, Message)}, {"blue"})
        fragged = [t for t in texts(frames) if "fragged" in t]
        self.assertTrue(fragged, "blue's own tasking was never announced")
        self.assertFalse(
            [t for t in fragged if target.name in t],
            "the humans were told the enemy's tasking",
        )

    def test_a_red_strike_damages_a_blue_target_offline(self):
        campaign = Campaign()
        storage = campaign.theater.targets.get(STORAGE)
        self.assertIsNotNone(storage, "blue has no strategic target for red to strike")
        frames = fly_until(
            campaign,
            lambda c: storage.units_alive < storage.units_initial,
            limit=30_000.0,
        )
        hits = [x for x in campaign.tracker.losses if x.entity_id == STORAGE]
        self.assertTrue(hits)
        self.assertTrue(all(x.cause == CAUSE_UNOBSERVED for x in hits))
        self.assertEqual(len(hits), storage.units_initial - storage.units_alive)
        # Through the tracker, so the storage area would come back into DCS
        # carrying the damage.
        self.assertEqual(campaign.tracker.units_alive(storage.spawn_id), storage.units_alive)
        # A red package put it there, having carried bombs to Incirlik.
        raids = [p for p in packages_of(campaign, "red") if p.weapons_released]
        self.assertTrue(raids)
        # The owner is told, and told what is left.
        told = [t for t in texts(frames) if t.startswith(storage.name)]
        self.assertTrue(told, f"blue was never told: {texts(frames)}")
        self.assertIn(f"{storage.units_alive} of {storage.units_initial} remaining", told[-1])

    def test_both_sides_hold_an_open_package_at_once_one_each(self):
        campaign = Campaign()
        run(campaign, PAPER_STEP)
        self.assertEqual(open_by_side(campaign), {"blue": 1, "red": 1})
        both = 0
        for _ in range(int(20_000 / PAPER_STEP)):
            campaign.advance(PAPER_STEP)
            counts = open_by_side(campaign)
            self.assertLessEqual(max(counts.values(), default=0), 1, counts)
            both += counts == {"blue": 1, "red": 1}
        self.assertGreater(both, 100, "the two sides were never in the air together")

    def test_player_coalition_does_not_decide_who_plans(self):
        """Humans flying red: blue still plans, and it is the same war.

        `player_coalition` decides who is told what, so two campaigns that
        differ only in it must fight the same war and differ only in the
        messages -- and in the seq numbers those messages spent.
        """
        as_red = Campaign(player_coalition="red")
        as_blue = Campaign()
        red_frames = run(as_red, 10_000.0)
        run(as_blue, 10_000.0)

        blue = packages_of(as_red, "blue")
        self.assertTrue(blue, "blue did not plan while the humans flew red")
        self.assertTrue(any(p.weapons_released for p in blue), "blue planned but never flew")
        blue_squadron = squadrons_of(as_red, "blue")[0]
        self.assertGreater(sum(blue_squadron.munitions_expended.values()), 0)

        def war(campaign: Campaign) -> dict:
            state = campaign.to_dict()
            state.pop("player_coalition")
            state.pop("out_seq")
            return state

        self.assertEqual(war(as_red), war(as_blue))

        told = [f for f in red_frames if isinstance(f, Message)]
        self.assertTrue(told)
        self.assertEqual({f.to for f in told}, {"red"})
        # Red's own tasking, and none of blue's: every package red hears
        # fragged is a strike on blue's storage area.
        fragged = [f.text for f in told if "fragged" in f.text]
        self.assertTrue(fragged, "the red humans were never told their own tasking")
        storage = as_red.theater.targets[STORAGE].name
        self.assertTrue(all(storage in t for t in fragged), fragged)


# ---------------------------------------------------------------------------
# Red faces blue's air defences
# ---------------------------------------------------------------------------


class TestRedFacesBlueAirDefences(unittest.TestCase):
    def test_the_red_route_runs_through_the_patriot_envelope(self):
        theater = build_slice_theater()
        self.assertIn(PATRIOT, theater.threats)
        self.assertEqual(theater.threats[PATRIOT].coalition, "blue")
        route = (theater.airbases[BASSEL].pos, theater.targets[STORAGE].pos)
        self.assertEqual([s.id for s in theater.live_threats_along("blue", *route)], [PATRIOT])

    def test_a_red_raid_can_be_shot_down_on_paper(self):
        # A Patriot that reaches as far as the Kh-58U, so red's SEAD element
        # has to fly into it to fire: from standoff -- the slice's own
        # Patriot -- it would live, suppress, and spare the strikers some of
        # the time (test_sead.TestStandoff says so).
        campaign = Campaign(
            theater=slice_with(
                **{PATRIOT: {"kill_probability": 1.0, "engagement_radius": launch_range("Kh-58U")}}
            )
        )
        frames = fly_until(campaign, first_red_raid_released, limit=5_000.0)
        package = packages_of(campaign, "red")[0]

        lost = element_losses(campaign, "red", "strike")
        self.assertEqual(len(lost), 2, "the Patriot shot nothing down")
        self.assertTrue(all(x.cause == CAUSE_UNOBSERVED and x.entity_id == package.id for x in lost))
        # A Pk of one leaves nothing of the SEAD element in front of it
        # either, and then there is nobody left to suppress anything.
        self.assertEqual(len(element_losses(campaign, "red", "sead")), 2)
        self.assertEqual(package.state, DESTROYED)
        squadron = strike_squadron(campaign, "red")
        self.assertEqual(squadron.airframes_lost, 2)
        # Shot down inbound, so the bombs went into the ground, not onto Incirlik.
        self.assertEqual(sum(squadron.munitions_lost.values()), 4)
        self.assertEqual(sum(squadron.munitions_expended.values()), 0)
        storage = campaign.theater.targets[STORAGE]
        self.assertEqual(storage.units_alive, storage.units_initial)
        squadron.check_invariant()
        self.assertIn("Incirlik Patriot engaged: 2 enemy aircraft down.", texts(frames))

    def test_a_route_clear_of_the_patriot_takes_no_losses_and_no_dice(self):
        # Far enough that the Patriot's published 160 km envelope misses red's
        # route: the 100 km this test used when the radius was a trimmed 40 km
        # would now put Bassel al-Assad itself inside it.
        far = slice_with(**{PATRIOT: {"kill_probability": 1.0, "pos": (-250_000.0, 0.0, 300_000.0)}})
        missed = Campaign(theater=far)
        fly_until(missed, first_red_raid_released, limit=5_000.0)
        self.assertEqual(flight_losses(missed, "red"), [])

        bare_theater = build_slice_theater()
        del bare_theater.threats[PATRIOT]
        bare = Campaign(theater=bare_theater)
        fly_until(bare, first_red_raid_released, limit=5_000.0)
        self.assertEqual(missed.rng.getstate(), bare.rng.getstate())

    def test_a_red_flight_dcs_is_holding_is_never_paper_rolled(self):
        """The authority rule, applied to red.

        The observer sits on blue's storage area, so DCS holds both the target
        and the raid when it arrives. The Patriot kills whatever it rolls
        against, so a single roll would be a loss -- and not one die may be
        thrown at red's TOT at all: the target is held too, and blue's own
        strike reaches its TOT some fifteen seconds later.
        """
        campaign = Campaign(theater=slice_with(**{PATRIOT: {"kill_probability": 1.0}}))
        storage = campaign.theater.targets[STORAGE]
        at_storage = [(storage.pos[0], 100.0, storage.pos[2])]
        seen: dict[str, object] = {}

        def watch(c: Campaign, _t: int) -> None:
            red = packages_of(c, "red")
            if not red:
                return
            package = red[0]
            if not package.weapons_released:
                seen["before"] = c.rng.getstate()
                seen["held_at_tot"] = c.tracker.is_instantiated(package.strike.spawn_id)
            elif "after" not in seen:
                seen["after"] = c.rng.getstate()

        drive(
            campaign,
            observer_positions=at_storage,
            damages=[],
            deliver_events=False,
            start=0,
            duration=1_800,
            hook=watch,
        )
        self.assertTrue(seen.get("held_at_tot"), "the raid was not instantiated; vacuous")
        self.assertIn("after", seen, "the raid never reached its TOT")
        self.assertEqual(seen["after"], seen["before"], "a red flight DCS held was rolled on paper")
        self.assertEqual(flight_losses(campaign, "red"), [])
        self.assertEqual(
            [x for x in campaign.tracker.losses if x.entity_id == STORAGE], [],
            "a target DCS held was resolved on paper",
        )


# ---------------------------------------------------------------------------
# Conservation, both sides, a long war
# ---------------------------------------------------------------------------


class TestConservationOnBothSides(unittest.TestCase):
    def test_both_sides_books_balance_through_a_long_war(self):
        """Sturdier targets and sites, deadlier sites, so the war lasts and bleeds.

        Forty units a target keeps either side from winning in a few sorties;
        a Pk of 0.3 at both sites makes losses common on both sides. Forty
        units a site too, now that SEAD missiles can destroy one: a five-unit
        battery is gone within a few escorted sorties, and after that nobody
        loses anything and the books balance trivially. And sites that reach
        as far as the missiles fired at them: a SEAD element that out-ranges
        its site is never shot at on paper, and the SEAD squadrons' books
        would balance trivially too. Every squadron and every reservation is
        checked after every paper step.
        """
        theater = slice_with(
            **{
                DEPOT: {"units_initial": 40, "units_alive": 40},
                STORAGE: {"units_initial": 40, "units_alive": 40},
                SA6: {"kill_probability": 0.3, "units_initial": 40, "units_alive": 40,
                      "engagement_radius": launch_range("AGM-88C")},
                PATRIOT: {"kill_probability": 0.3, "units_initial": 40, "units_alive": 40,
                          "engagement_radius": launch_range("Kh-58U")},
            }
        )
        campaign = Campaign(theater=theater)
        for _ in range(int(100_000 / PAPER_STEP)):
            campaign.advance(PAPER_STEP)
            conserved(self, campaign)

        for coalition in ("blue", "red"):
            with self.subTest(coalition=coalition):
                # The strike squadron and the SEAD squadron each answer for
                # their own elements' losses, and nothing else.
                for role, squadron in (
                    ("strike", strike_squadron(campaign, coalition)),
                    ("sead", sead_squadron(campaign, coalition)),
                ):
                    lost = element_losses(campaign, coalition, role)
                    self.assertGreaterEqual(len(lost), 4, "too few losses to prove anything")
                    self.assertEqual(squadron.airframes_lost, len(lost), role)
                    # Every loss is on paper, at the TOT, before release: each
                    # took its two bombs, or two missiles, down with it.
                    self.assertEqual(sum(squadron.munitions_lost.values()), 2 * len(lost))
                    self.assertEqual(
                        squadron.airframes_available + squadron.airframes_reserved
                        + squadron.airframes_lost,
                        squadron.airframes_total,
                    )
                self.assertEqual(
                    len(flight_losses(campaign, coalition)),
                    len(element_losses(campaign, coalition, "strike"))
                    + len(element_losses(campaign, coalition, "sead")),
                )


# ---------------------------------------------------------------------------
# The end of the war
# ---------------------------------------------------------------------------


class TestTheWarEnds(unittest.TestCase):
    def _fight_to_the_end(self, seed: int, player: str = "blue") -> tuple[Campaign, list]:
        campaign = Campaign(seed=seed, player_coalition=player)
        frames = run(campaign, 86_400.0)
        self.assertIsNotNone(getattr(campaign, "war_result", None), "the war never ended")
        return campaign, frames

    def _ended_cleanly(self, campaign: Campaign) -> None:
        result = campaign.war_result
        # Nothing was planned after the end, and nothing is left open.
        self.assertTrue(all(p.t_created <= result["t"] for p in campaign.packages.values()))
        self.assertEqual(open_by_side(campaign), {})
        for coalition in ("blue", "red"):
            for squadron in squadrons_of(campaign, coalition):
                self.assertEqual(squadron.open_reservations, {}, squadron.id)
                squadron.check_invariant()
        # Anything still on the ground when it ended was stood down, not
        # flown -- element by element, since a SEAD element leaves first.
        for package in campaign.packages.values():
            for element in package.elements:
                if element.t_takeoff > result["t"]:
                    self.assertEqual(element.state, ABORTED, element.reservation_id)
            if package.t_takeoff > result["t"]:
                self.assertEqual(package.state, ABORTED, package.id)
        # An ended war stays ended through a save.
        reloaded = Campaign.from_dict(campaign.to_dict())
        self.assertEqual(reloaded.war_result, result)
        before = len(reloaded.packages)
        run(reloaded, 20_000.0)
        self.assertEqual(len(reloaded.packages), before, "a reloaded, finished war planned again")

    def test_blue_wins_and_the_blue_humans_are_told(self):
        campaign, frames = self._fight_to_the_end(SEED_BLUE_WINS)
        self.assertEqual(campaign.war_result["defeated"], ["red"])
        self.assertTrue(campaign.theater.targets[DEPOT].destroyed)
        self.assertFalse(campaign.theater.targets[STORAGE].destroyed)
        self.assertEqual(texts(frames).count(WON), 1)
        self.assertNotIn(LOST, texts(frames))
        self._ended_cleanly(campaign)

    def test_red_wins_and_the_blue_humans_are_told_they_lost(self):
        campaign, frames = self._fight_to_the_end(SEED_RED_WINS)
        self.assertEqual(campaign.war_result["defeated"], ["blue"])
        self.assertTrue(campaign.theater.targets[STORAGE].destroyed)
        self.assertFalse(campaign.theater.targets[DEPOT].destroyed)
        self.assertEqual(texts(frames).count(LOST), 1)
        self.assertNotIn(WON, texts(frames))
        # The last of it is said before the end is: the humans learn their
        # storage area is gone, then that the war is.
        said = texts(frames)
        self.assertLess(said.index("Incirlik Munitions Storage destroyed."), said.index(LOST))
        self._ended_cleanly(campaign)

    def test_the_same_red_victory_is_a_win_for_red_humans(self):
        campaign, frames = self._fight_to_the_end(SEED_RED_WINS, player="red")
        self.assertEqual(campaign.war_result["defeated"], ["blue"])
        self.assertEqual(texts(frames).count(WON), 1)
        self.assertNotIn(LOST, texts(frames))
        self.assertEqual({f.to for f in frames if isinstance(f, Message)}, {"red"})

    def test_both_sides_losing_at_once_is_a_war_without_a_victor(self):
        theater = slice_with(
            **{DEPOT: {"units_alive": 0}, STORAGE: {"units_alive": 0}}
        )
        campaign = Campaign(theater=theater)
        frames = run(campaign, PAPER_STEP)
        result = getattr(campaign, "war_result", None)
        self.assertIsNotNone(result, "the war did not end")
        self.assertEqual(result["defeated"], ["blue", "red"])
        self.assertEqual(texts(frames), [DRAWN])
        self.assertEqual(campaign.packages, {})

    def test_a_package_still_on_the_ground_is_stood_down(self):
        """Seed-free: end the war by hand while both sides' first packages wait."""
        campaign = Campaign()
        run(campaign, PAPER_STEP)
        waiting = [p for p in campaign.packages.values() if p.state == PLANNED]
        self.assertEqual(len(waiting), 2, "both sides should be waiting to take off")
        # Flattened on paper, the way a strike nobody watched would do it.
        depot = campaign.theater.targets[DEPOT]
        for loss in campaign.tracker.record_unobserved(
            depot.spawn_id, depot.units_alive, campaign.clock
        ):
            campaign._apply_loss(loss)
        frames = campaign.advance(PAPER_STEP)
        result = getattr(campaign, "war_result", None)
        self.assertIsNotNone(result, "the war did not end")
        self.assertEqual(result["defeated"], ["red"])
        for package in waiting:
            self.assertEqual(package.state, ABORTED, package.id)
            # Every element was on the ground, so every element stood down.
            self.assertEqual(
                [e.state for e in package.elements], [ABORTED] * len(package.elements)
            )
            for element in package.elements:
                squadron = campaign.inventories[side_of(campaign, package)].squadrons[
                    element.squadron_id
                ]
                self.assertEqual(squadron.open_reservations, {})
                self.assertEqual(squadron.airframes_available, squadron.airframes_total)
        said = texts(frames)
        self.assertIn(WON, said)
        blue = packages_of(campaign, "blue")[0]
        self.assertIn(f"{blue.callsign} stood down: the war is over.", said)
        # Red's was stood down too, and nobody told the humans red's callsign:
        # the humans heard of blue's elements standing down and nothing else.
        self.assertEqual(
            [t for t in said if "stood down" in t],
            [f"{blue.name_of(e)} stood down: the war is over." for e in blue.elements],
        )


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


class TestBothSidesFight(unittest.TestCase):
    def test_left_alone_for_a_day_both_sides_bleed_and_both_do_damage(self):
        """A day of war with DCS closed, on the slice as shipped.

        Before section 4 the same day ended with red having lost nothing and
        destroyed nothing, whatever the seed: red flew nothing.
        """
        campaign = Campaign(seed=SEED_BOTH_BLEED)
        run(campaign, 86_400.0)

        for coalition, enemy_target in (("blue", DEPOT), ("red", STORAGE)):
            with self.subTest(side=coalition):
                lost = element_losses(campaign, coalition, "strike")
                self.assertGreaterEqual(len(lost), 1, f"{coalition} lost no aircraft")
                squadron = strike_squadron(campaign, coalition)
                self.assertEqual(squadron.airframes_lost, len(lost))
                squadron.check_invariant()
                sead = sead_squadron(campaign, coalition)
                self.assertEqual(
                    sead.airframes_lost, len(element_losses(campaign, coalition, "sead"))
                )
                sead.check_invariant()
                hits = [
                    x for x in campaign.tracker.losses
                    if x.entity_kind == KIND_TARGET and x.entity_id == enemy_target
                ]
                self.assertGreaterEqual(len(hits), 1, f"{coalition} destroyed nothing")
        # Nobody was watching any of it.
        self.assertTrue(all(x.cause == CAUSE_UNOBSERVED for x in campaign.tracker.losses))


if __name__ == "__main__":
    unittest.main()
