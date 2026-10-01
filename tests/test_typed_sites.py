"""Air defences: typed units, blinded sites, and repair.

docs/design.md, section 6. A site's units are a radar and launchers. An
anti-radiation missile homes on the radar, so a paper ARM kill removes it,
and a site whose radar is gone cannot engage until it is repaired. While DCS
is not holding a site the engine repairs it at its theater's rates; while
DCS holds it, it changes only by snapshot -- and since protocol v4 the
snapshot says which units are left, so the engine never has to guess which
one the sim destroyed.

What is pinned here:

  * an ARM kill takes the radar, and only the radar, and blinds the site;
  * a blinded site throws no dice at a strike, and no ARM is fired at it;
  * repair puts the radar back after its period and the site engages again;
  * repair never runs while DCS holds the site, or is about to, or has not
    yet let it go;
  * an observed site's typed losses come from the snapshot, never the paper;
  * a respawned site carries its repaired composition, and the harness
    builds exactly that;
  * a site with nothing left stays gone, and the slice repairs nothing;
  * protocol v4: the round trip, a v3 peer refused, a malformed field;
  * determinism, a save mid-repair, and every squadron's books on a Syria
    war that repairs.

Scripted dice are tests/test_sead.py's `Dice`: an unscripted draw fails.
"""

from __future__ import annotations

import dataclasses
import json
import random
import unittest

from campaign.api import PAPER_STEP
from campaign.attrition import CAUSE_UNOBSERVED, KIND_THREAT
from campaign.campaign import Campaign
from campaign.oob import build_syria_oob
from campaign.protocol import (
    PROTOCOL_VERSION,
    GroupSnapshot,
    Hello,
    Message,
    ProtocolError,
    Spawn,
    StateReport,
    decode_uplink,
    encode,
)
from campaign.theater import (
    REPAIR_OFF,
    SYRIA_REPAIR,
    SiteRepair,
    Theater,
    build_slice_theater,
    build_syria_theater,
)
from tests.test_domain import Damage, FakeDCS, SilentDespawnDCS
from tests.test_sead import (
    HITS,
    SA6,
    SURVIVES,
    blue_package,
    conserved_everywhere,
    resolve_blue_tot,
    standing,
    texts,
    to_the_brink,
)
from tests.test_single_element import strike_only_oob

RADAR, LAUNCHER = "Kub 1S91 str", "Kub 2P25 ln"
#: Hundreds of kilometres from anything on the slice.
FAR = (400_000.0, 100.0, 400_000.0)


def slice_where_the_sa6(*, units: dict[str, int] | None = None,
                        repair: SiteRepair = REPAIR_OFF, **fields) -> Theater:
    theater = build_slice_theater()
    site = theater.threats[SA6]
    for key, value in fields.items():
        setattr(site, key, value)
    if units is not None:
        site.units_by_type = dict(units)
    theater.repair = repair
    return theater


def threat_losses(campaign: Campaign) -> list:
    """The SA-6's ledger entries. Red's raids may take units off the Patriot."""
    return [x for x in campaign.tracker.losses
            if x.entity_kind == KIND_THREAT and x.entity_id == SA6]


def snapshot(site, *, units: int, unit_types, alive: bool = True) -> StateReport:
    return StateReport(seq=7, t=0.0, groups=[GroupSnapshot(
        spawn_id=site.spawn_id, alive=alive, units=units, units_initial=5,
        unit_types=unit_types)])


def hold_the_site(campaign: Campaign) -> FakeDCS:
    """DCS connects with its observer on the SA-6, and builds it."""
    site = campaign.theater.threats[SA6]
    dcs = FakeDCS(campaign=campaign, deliver_events=False)
    dcs.connect(0.0)
    dcs.observers(0.0, [site.pos])
    assert campaign.tracker.is_instantiated(site.spawn_id), "the site was not built"
    return dcs


# ---------------------------------------------------------------------------
# The typed rule
# ---------------------------------------------------------------------------


class TestAnArmKillsTheRadar(unittest.TestCase):
    def test_an_arm_kill_removes_the_radar_and_blinds_the_site(self):
        """The slice as shipped: blue's HARMs from standoff, no SEAD exposure.

        Four missiles are rolled whatever they do. A hit takes the radar; a
        later hit has nothing to home on and takes nothing. The battery is
        then blind, so the strikers behind it throw no exposure dice.
        """
        for label, missiles in (
            ("the first hits", [HITS, SURVIVES, SURVIVES, SURVIVES]),
            ("every one hits", [HITS] * 4),
            ("the last hits", [SURVIVES, SURVIVES, SURVIVES, HITS]),
        ):
            with self.subTest(label):
                campaign = Campaign()
                to_the_brink(campaign)
                site = campaign.theater.threats[SA6]
                self.assertEqual(site.units_by_type, {RADAR: 1, LAUNCHER: 4})
                _, dice = resolve_blue_tot(campaign, missiles + [SURVIVES] * 4)
                self.assertEqual(dice.script, [], "a blind battery fired, or a missile went unrolled")
                self.assertEqual(site.units_by_type, {LAUNCHER: 4})
                self.assertFalse(site.can_engage)
                self.assertFalse(site.destroyed)
                self.assertEqual(campaign.tracker.units_by_type(site.spawn_id), {LAUNCHER: 4})
                (loss,) = threat_losses(campaign)
                self.assertEqual((loss.entity_id, loss.cause), (SA6, CAUSE_UNOBSERVED))
                conserved_everywhere(self, campaign)

    def test_the_owner_is_told_the_battery_is_blind(self):
        campaign = Campaign(player_coalition="red")
        to_the_brink(campaign)
        frames, _ = resolve_blue_tot(campaign, [HITS, SURVIVES, SURVIVES, SURVIVES]
                                     + [SURVIVES] * 4)
        self.assertIn(
            "Latakia North SA-6 hit: 1 unit(s) destroyed, 4 of 5 remaining. "
            "Its radar is out: it cannot engage.",
            texts(frames),
        )


class TestABlindSiteDoesNothing(unittest.TestCase):
    def test_a_blinded_site_rolls_nothing_against_a_strike(self):
        """A battery that kills whatever it rolls against, and no SEAD.

        With its radar, the strikers' two exposure dice kill both and nothing
        is bombed. Without it, the only dice at blue's TOT are the four bombs;
        a single die thrown at the strikers fails the script.
        """
        for label, units, script, lost in (
            ("radar", None, [SURVIVES, SURVIVES], 2),
            ("no radar", {LAUNCHER: 4}, [SURVIVES] * 4, 0),
        ):
            with self.subTest(label):
                campaign = Campaign(
                    theater=slice_where_the_sa6(units=units, kill_probability=1.0),
                    inventories=strike_only_oob(),
                )
                to_the_brink(campaign)
                _, dice = resolve_blue_tot(campaign, script)
                self.assertEqual(dice.script, [])
                package = blue_package(campaign)
                self.assertEqual(
                    len(campaign.tracker.losses_for(package.strike.spawn_id)), lost
                )

    def test_a_route_past_a_blind_battery_is_not_exposed(self):
        """Nothing to home on, and nothing that can fire: no SEAD is sent,
        no missile is spent, and the only dice at the TOT are the bombs.

        Before this the planner escorted a strike past a blind battery and
        the SEAD element spent its four missiles at nothing: in the first
        Syria measurement both sides' main SEAD squadrons were dry by the
        last quarter of the war.
        """
        campaign = Campaign(theater=slice_where_the_sa6(units={LAUNCHER: 4}))
        route = (campaign.theater.airbases["incirlik"].pos,
                 campaign.theater.targets["latakia_fuel_depot"].pos)
        self.assertEqual(campaign.theater.live_threats_along("red", *route), [])
        to_the_brink(campaign)
        package = blue_package(campaign)
        self.assertIsNone(package.sead)
        start = standing(campaign)
        _, dice = resolve_blue_tot(campaign, [SURVIVES] * 4)  # the bombs alone
        self.assertEqual(dice.script, [])
        self.assertEqual(threat_losses(campaign), [])
        sead = campaign.inventories["blue"].squadron("vfa_incirlik_f16_sead")
        self.assertEqual(sead.munitions_expended.get("AGM-88C", 0), 0)
        conserved_everywhere(self, campaign, start)

    def test_the_missiles_go_to_the_battery_that_can_still_be_seen(self):
        """A blind battery and a seeing one on the same route.

        The blind one, first in id order, gets none of the four missiles:
        all four are rolled at the one still emitting, and it, not the blind
        one, is what the strikers then meet. Shared out over both, two
        missiles would have gone nowhere.
        """
        theater = slice_where_the_sa6(units={LAUNCHER: 4})
        twin = dataclasses.replace(theater.threats[SA6], id="latakia_south_sa6",
                                   name="Latakia South SA-6", spawn_id="",
                                   units_alive=5)
        theater.threats[twin.id] = twin
        campaign = Campaign(theater=theater)
        to_the_brink(campaign)
        self.assertIsNotNone(blue_package(campaign).sead, "the planner sent no SEAD; vacuous")
        _, dice = resolve_blue_tot(
            campaign,
            [HITS, SURVIVES, SURVIVES, SURVIVES]   # four missiles at the twin
            + [SURVIVES] * 4,                     # both blind now: no exposure; bombs
        )
        self.assertEqual(dice.script, [])
        self.assertEqual(campaign.theater.threats[twin.id].units_by_type, {LAUNCHER: 4})
        self.assertEqual(campaign.theater.threats[SA6].units_by_type, {LAUNCHER: 4})

    def test_a_battery_of_emitters_loses_one_a_hit_and_engages_to_the_last(self):
        """A Tor is its own radar: every unit is a target, and the battery
        engages until the last one goes."""
        campaign = Campaign(theater=slice_where_the_sa6(template="SA-15_Tor_site",
                                                        units_alive=5))
        to_the_brink(campaign)
        site = campaign.theater.threats[SA6]
        # Three hits, then the strikers' two exposure dice: it still engages.
        _, dice = resolve_blue_tot(campaign, [HITS, HITS, HITS, SURVIVES]
                                   + [SURVIVES] * 2 + [SURVIVES] * 4)
        self.assertEqual(dice.script, [])
        self.assertEqual(site.units_by_type, {"Tor 9A331": 2})
        self.assertTrue(site.can_engage)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


class TestRepair(unittest.TestCase):
    PERIOD = 600.0

    def test_repair_restores_the_radar_after_its_period_and_the_site_engages_again(self):
        for label, repair, script in (
            ("repaired", SiteRepair(radar_time=self.PERIOD), [SURVIVES] * 2 + [SURVIVES] * 4),
            ("not repaired", REPAIR_OFF, [SURVIVES] * 4),
        ):
            with self.subTest(label):
                campaign = Campaign(
                    theater=slice_where_the_sa6(units={LAUNCHER: 4}, repair=repair),
                    inventories=strike_only_oob(),
                )
                site = campaign.theater.threats[SA6]
                while campaign.clock < self.PERIOD:
                    self.assertFalse(site.can_engage, campaign.clock)
                    campaign.advance(PAPER_STEP)
                self.assertEqual(site.can_engage, repair is not REPAIR_OFF)
                if site.can_engage:
                    self.assertEqual(site.units_by_type, {RADAR: 1, LAUNCHER: 4})
                    self.assertEqual(
                        campaign.tracker.units_by_type(site.spawn_id), site.units_by_type
                    )
                    self.assertEqual(site.radar_repair, 0.0)
                # Repair is not a loss, and books nothing.
                self.assertEqual(threat_losses(campaign), [])
                # Blue's first TOT, 1471 s: the repaired battery throws its two
                # exposure dice at the strikers again; the blind one none.
                to_the_brink(campaign)
                _, dice = resolve_blue_tot(campaign, script)
                self.assertEqual(dice.script, [])

    def test_the_owner_hears_its_radar_is_back(self):
        campaign = Campaign(
            theater=slice_where_the_sa6(units={LAUNCHER: 4},
                                        repair=SiteRepair(radar_time=self.PERIOD)),
            inventories={}, player_coalition="red",
        )
        said = texts(campaign.advance(self.PERIOD))
        self.assertEqual(said, ["Latakia North SA-6 repaired: 1 unit(s) restored, 5 of 5. "
                                "Radar back in action."])

    def test_launchers_are_repaired_one_a_period_beside_the_radar(self):
        campaign = Campaign(
            theater=slice_where_the_sa6(
                units={LAUNCHER: 2},
                repair=SiteRepair(radar_time=self.PERIOD, launcher_time=2 * self.PERIOD),
            ),
            inventories={},
        )
        site = campaign.theater.threats[SA6]
        seen = []
        for _ in range(int(4 * self.PERIOD / PAPER_STEP)):
            campaign.advance(PAPER_STEP)
            if not seen or seen[-1][1] != site.units_by_type:
                seen.append((campaign.clock, dict(site.units_by_type)))
        self.assertEqual(seen, [
            (5.0, {LAUNCHER: 2}),
            (600.0, {RADAR: 1, LAUNCHER: 2}),
            (1200.0, {RADAR: 1, LAUNCHER: 3}),
            (2400.0, {RADAR: 1, LAUNCHER: 4}),
        ])
        self.assertEqual((site.radar_repair, site.launcher_repair), (0.0, 0.0))

    def test_repair_never_runs_while_dcs_holds_the_site(self):
        campaign = Campaign(
            theater=slice_where_the_sa6(units={LAUNCHER: 4},
                                        repair=SiteRepair(radar_time=self.PERIOD)),
            inventories={},
        )
        site = campaign.theater.threats[SA6]
        dcs = hold_the_site(campaign)
        # Three periods with DCS holding it: not a minute of repair.
        for t in range(5, int(3 * self.PERIOD) + 1, 5):
            dcs.observers(float(t), [site.pos])
            if t % 30 == 0:
                dcs.state(t)
            self.assertTrue(campaign.tracker.is_instantiated(site.spawn_id))
        self.assertFalse(site.can_engage)
        self.assertEqual(site.radar_repair, 0.0)
        self.assertEqual(dcs.groups[site.spawn_id].types, [LAUNCHER] * 4)
        # Released: the work starts then, and takes the whole period.
        t = 3 * self.PERIOD + 5
        dcs.observers(t, [FAR])
        self.assertNotIn(site.spawn_id, dcs.groups, "the site was not despawned")
        released = campaign.clock
        while campaign.clock < released + self.PERIOD - PAPER_STEP:
            t += 5
            dcs.observers(t, [FAR])
            self.assertFalse(site.can_engage, campaign.clock - released)
        dcs.observers(t + 5, [FAR])
        self.assertTrue(site.can_engage)

    def test_a_despawn_dcs_has_not_acknowledged_still_holds_the_site(self):
        """Its last census may still be on its way; repairing under it would
        read the repaired radar as one DCS destroyed."""
        campaign = Campaign(
            theater=slice_where_the_sa6(units={LAUNCHER: 4},
                                        repair=SiteRepair(radar_time=self.PERIOD)),
            inventories={},
        )
        site = campaign.theater.threats[SA6]
        dcs = SilentDespawnDCS(campaign=campaign, deliver_events=False)
        dcs.connect(0.0)
        dcs.observers(0.0, [site.pos])
        dcs.observers(5.0, [FAR])
        self.assertFalse(campaign.tracker.is_instantiated(site.spawn_id))
        for t in range(10, int(2 * self.PERIOD), 5):
            dcs.observers(float(t), [FAR])
        self.assertFalse(site.can_engage, "repaired under an unanswered despawn")
        # DCS goes away; nothing is pending any more, and the war moves on.
        campaign.on_disconnect()
        campaign.advance(self.PERIOD + PAPER_STEP)
        self.assertTrue(site.can_engage)

    def test_a_site_with_nothing_left_stays_gone(self):
        campaign = Campaign(
            theater=slice_where_the_sa6(units={LAUNCHER: 4}, repair=SYRIA_REPAIR),
            inventories={},
        )
        site = campaign.theater.threats[SA6]
        dcs = hold_the_site(campaign)
        dcs.damages = [Damage(t=30, category="ground", remove=4)]
        dcs.state(30)
        self.assertTrue(site.destroyed)
        campaign.on_disconnect()
        campaign.advance(3 * 86_400.0)
        self.assertTrue(site.destroyed)
        self.assertEqual(site.units_by_type, {})
        self.assertNotIn(site.spawn_id, campaign.tracker.groups)
        self.assertNotIn(site.spawn_id, campaign._candidates())

    def test_the_slice_repairs_nothing(self):
        self.assertIs(build_slice_theater().repair, REPAIR_OFF)
        campaign = Campaign(theater=slice_where_the_sa6(units={LAUNCHER: 4}), inventories={})
        campaign.advance(86_400.0)
        self.assertEqual(campaign.theater.threats[SA6].units_by_type, {LAUNCHER: 4})


# ---------------------------------------------------------------------------
# Authority: which units DCS destroyed is the snapshot's to say
# ---------------------------------------------------------------------------


class TestTheSnapshotSaysWhichUnitsWent(unittest.TestCase):
    def test_an_observed_sites_typed_losses_come_from_the_snapshot(self):
        """The same one-unit loss, read two ways: the census decides which."""
        for label, unit_type, left, engages in (
            ("the radar", RADAR, {LAUNCHER: 4}, False),
            ("a launcher", LAUNCHER, {RADAR: 1, LAUNCHER: 3}, True),
        ):
            with self.subTest(label):
                campaign = Campaign(inventories={})
                site = campaign.theater.threats[SA6]
                dcs = hold_the_site(campaign)
                dcs.damages = [Damage(t=30, category="ground", remove=1, unit_type=unit_type)]
                dcs.state(30)
                self.assertEqual(site.units_by_type, left)
                self.assertEqual(site.can_engage, engages)
                (loss,) = threat_losses(campaign)
                self.assertNotEqual(loss.cause, CAUSE_UNOBSERVED)

    def test_the_paper_cannot_take_a_unit_off_a_site_dcs_holds(self):
        campaign = Campaign(inventories={})
        site = campaign.theater.threats[SA6]
        hold_the_site(campaign)
        self.assertEqual(
            campaign.tracker.record_unobserved(site.spawn_id, 1, 0.0, unit_type=RADAR), []
        )
        self.assertEqual(site.units_by_type, {RADAR: 1, LAUNCHER: 4})

    def test_a_census_that_cannot_name_the_units_books_nothing_until_one_can(self):
        """A count fell, but nobody could say which unit went: no guess."""
        campaign = Campaign(inventories={})
        site = campaign.theater.threats[SA6]
        hold_the_site(campaign)
        campaign.on_state(snapshot(site, units=4, unit_types=None))
        self.assertEqual(threat_losses(campaign), [])
        self.assertEqual(site.units_by_type, {RADAR: 1, LAUNCHER: 4})
        campaign.on_state(snapshot(site, units=4, unit_types={LAUNCHER: 4}))
        self.assertEqual(len(threat_losses(campaign)), 1)
        self.assertEqual(site.units_by_type, {LAUNCHER: 4})

    def test_a_census_cannot_resurrect_a_radar(self):
        campaign = Campaign(theater=slice_where_the_sa6(units={LAUNCHER: 4}), inventories={})
        site = campaign.theater.threats[SA6]
        hold_the_site(campaign)
        campaign.on_state(snapshot(site, units=5, unit_types={RADAR: 1, LAUNCHER: 4}))
        self.assertEqual(site.units_by_type, {LAUNCHER: 4})
        self.assertEqual(threat_losses(campaign), [])

    def test_a_dead_battery_needs_no_types(self):
        campaign = Campaign(inventories={})
        site = campaign.theater.threats[SA6]
        hold_the_site(campaign)
        campaign.on_state(snapshot(site, units=0, unit_types=None, alive=False))
        self.assertTrue(site.destroyed)
        self.assertEqual(len(threat_losses(campaign)), 5)


# ---------------------------------------------------------------------------
# The spawn
# ---------------------------------------------------------------------------


class TestARespawnedSiteIsWhatTheWarLeft(unittest.TestCase):
    def _spawn_of(self, campaign: Campaign) -> Spawn:
        site = campaign.theater.threats[SA6]
        dcs = hold_the_site(campaign)
        (frame,) = [f for f in dcs.downlink if isinstance(f, Spawn) and f.spawn_id == site.spawn_id]
        # The stand-in built what the frame said, and its census agrees:
        # nothing is booked, which a battery built differently would be.
        losses = len(campaign.tracker.losses)
        dcs.state(30)
        self.assertEqual(len(campaign.tracker.losses), losses)
        campaign.on_disconnect()
        return frame

    def test_a_respawned_site_carries_its_repaired_composition(self):
        campaign = Campaign(
            theater=slice_where_the_sa6(
                units={LAUNCHER: 2},
                repair=SiteRepair(radar_time=600.0, launcher_time=1200.0),
            ),
            inventories={},
        )
        first = self._spawn_of(campaign)
        self.assertEqual((first.units, first.tasking),
                         (2, {"kind": "air_defence", "composition": {LAUNCHER: 2}}))
        # The radar is back after a period off paper; the battery it makes
        # is the one a radar-first build gives, so nothing more is said.
        campaign.advance(600.0 + PAPER_STEP)
        second = self._spawn_of(campaign)
        self.assertEqual((second.units, second.tasking), (3, {"kind": "air_defence"}))
        campaign.advance(1200.0 + PAPER_STEP)
        third = self._spawn_of(campaign)
        self.assertEqual((third.units, third.tasking), (4, {"kind": "air_defence"}))

    def test_a_site_blinded_on_paper_comes_back_into_dcs_blind(self):
        campaign = Campaign()
        to_the_brink(campaign)
        resolve_blue_tot(campaign, [HITS, SURVIVES, SURVIVES, SURVIVES] + [SURVIVES] * 4)
        # The scripted dice are spent; the rest of the war rolls real ones.
        campaign.rng = random.Random(1)
        frame = self._spawn_of(campaign)
        self.assertEqual((frame.units, frame.tasking),
                         (4, {"kind": "air_defence", "composition": {LAUNCHER: 4}}))


# ---------------------------------------------------------------------------
# Protocol v4
# ---------------------------------------------------------------------------


class TestProtocolV4(unittest.TestCase):
    def test_the_engine_speaks_4(self):
        self.assertEqual(PROTOCOL_VERSION, 4)

    def test_unit_types_round_trip(self):
        report = StateReport(seq=3, t=30.0, groups=[
            GroupSnapshot(spawn_id="0004", alive=True, units=4, units_initial=5,
                          unit_types={LAUNCHER: 4}),
            GroupSnapshot(spawn_id="0005", alive=True, units=2, units_initial=2),
        ])
        decoded = decode_uplink(encode(report))
        self.assertEqual(decoded, report)
        self.assertEqual(decoded.groups[0].unit_types, {LAUNCHER: 4})
        self.assertIsNone(decoded.groups[1].unit_types)

    def test_a_malformed_unit_types_is_a_protocol_error(self):
        for label, units, unit_types in (
            ("not an object", 4, [LAUNCHER]),
            ("an empty type name", 4, {"": 4}),
            ("a fractional count", 4, {LAUNCHER: 4.0}),
            ("a negative count", 4, {RADAR: -1, LAUNCHER: 5}),
            ("a boolean count", 1, {RADAR: True}),
            ("not adding up to units", 5, {LAUNCHER: 4}),
        ):
            with self.subTest(label):
                raw = json.dumps({"type": "state", "seq": 1, "t": 0.0, "groups": [
                    {"spawn_id": "0004", "alive": True, "units": units, "units_initial": 5,
                     "unit_types": unit_types}]})
                with self.assertRaises(ProtocolError):
                    decode_uplink(raw)

    def test_a_version_3_hello_is_refused_and_changes_nothing(self):
        campaign = Campaign()
        campaign.advance(PAPER_STEP)
        before = campaign.to_dict()
        with self.assertRaises(ProtocolError) as caught:
            campaign.on_hello(Hello(seq=1, t=0.0, protocol=3, theater="Syria"))
        self.assertIn("client protocol 3 != engine 4", str(caught.exception))
        self.assertFalse(campaign.connected)
        self.assertEqual(campaign.to_dict(), before)


# ---------------------------------------------------------------------------
# The Syria war, which repairs
# ---------------------------------------------------------------------------


def new_syria(seed: int) -> Campaign:
    blue, red = build_syria_oob()
    return Campaign(theater=build_syria_theater(),
                    inventories={blue.coalition: blue, red.coalition: red}, seed=seed)


class TestTheSyriaWarRepairs(unittest.TestCase):
    def test_syria_repairs_at_its_published_placeholders(self):
        self.assertIs(build_syria_theater().repair, SYRIA_REPAIR)
        self.assertEqual(SYRIA_REPAIR, SiteRepair(radar_time=12 * 3600.0,
                                                  launcher_time=24 * 3600.0))

    def test_every_squadron_balances_through_a_war_that_repairs(self):
        for seed in (1, 2):
            with self.subTest(seed=seed):
                campaign = new_syria(seed)
                repaired = 0
                for _ in range(int(3 * 86_400.0 / PAPER_STEP)):
                    for frame in campaign.advance(PAPER_STEP):
                        if isinstance(frame, Message) and "repaired" in frame.text:
                            repaired += 1
                    for inventory in campaign.inventories.values():
                        inventory.check_invariant()
                    if campaign.war_result is not None and not any(
                        p.is_open for p in campaign.packages.values()
                    ):
                        break
                self.assertIsNotNone(campaign.war_result, "the war never ended")
                for inventory in campaign.inventories.values():
                    for squadron in inventory.squadrons.values():
                        self.assertEqual(squadron.open_reservations, {}, squadron.id)
                        self.assertEqual(
                            squadron.airframes_available + squadron.airframes_lost,
                            squadron.airframes_total, squadron.id,
                        )
                # Sites and tracker agree on every surviving battery.
                for site in campaign.theater.threats.values():
                    if not site.destroyed:
                        self.assertEqual(
                            campaign.tracker.units_by_type(site.spawn_id), site.units_by_type
                        )
                # Not vacuous: the humans' side heard of a repair.
                self.assertGreater(repaired, 0, "nothing was repaired; vacuous")

    def test_a_save_mid_repair_continues_the_same_war(self):
        campaign = new_syria(1)
        while not any(s.radar_repair > 0 for s in campaign.theater.threats.values()):
            campaign.advance(PAPER_STEP)
            self.assertLess(campaign.clock, 3 * 86_400.0, "no repair ever started")
        raw = json.loads(json.dumps(campaign.to_dict()))
        reloaded = Campaign.from_dict(raw)
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())
        self.assertEqual(reloaded.repaired_to, campaign.repaired_to)
        for _ in range(int(86_400.0 / PAPER_STEP)):
            campaign.advance(PAPER_STEP)
            reloaded.advance(PAPER_STEP)
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())

    def test_a_seed_is_a_war(self):
        a, b = new_syria(3), new_syria(3)
        for _ in range(int(86_400.0 / PAPER_STEP)):
            self.assertEqual(
                [encode(f) for f in a.advance(PAPER_STEP)],
                [encode(f) for f in b.advance(PAPER_STEP)],
            )
        self.assertEqual(a.to_dict(), b.to_dict())


if __name__ == "__main__":
    unittest.main()
