"""Two tiers of anti-radiation effect: a radar destroyed, or forced off the air.

docs/design.md, section 7. A missile that homes on a site's radar seldom
destroys it; mostly it forces the battery off the air. A shutdown blinds the
site for `resolver.ARM_SHUTDOWN_TIME` and then the radar is simply back --
nothing was broken, so nothing is repaired. A destruction blinds it until
the radar is replaced, through the repair the theater carries.

What is pinned here:

  * one die a missile carries hit, destroy and shutdown, so the draws are
    the missiles fired, whatever the dice say;
  * a shutdown blinds the site at the TOT and ends on time, with no unit
    lost and no repair involved; a destruction goes through repair;
  * nobody plans SEAD against a battery that will still be off the air at
    the TOT, and nobody double-counts two shutdowns;
  * a battery spawned into DCS mid-shutdown is sent with its emitters off
    until the paper has them back;
  * the shutdown survives a save.

Scripted dice are tests/test_sead.py's `Dice`: an unscripted draw fails.
"""

from __future__ import annotations

import json
import random
import unittest

from campaign.api import PAPER_STEP
from campaign.attrition import KIND_THREAT
from campaign.campaign import Campaign
from campaign.protocol import Hello, Observer, ObserverReport, Spawn
from campaign.resolver import (
    ARM_DESTROY_FRACTION,
    ARM_PK,
    ARM_SHUTDOWN_TIME,
    resolve_arm,
)
from campaign.theater import REPAIR_OFF, SiteRepair, build_slice_theater
from tests.test_sead import (
    HITS,
    SA6,
    SURVIVES,
    Dice,
    blue_package,
    conserved_everywhere,
    resolve_blue_tot,
    standing,
    texts,
    to_the_brink,
)
from tests.test_single_element import strike_only_oob
from tests.test_validation import HAVE_LUPA

if HAVE_LUPA:
    from tests.test_validation import ValidatorRun

requires_lua = unittest.skipUnless(
    HAVE_LUPA, "lupa is not installed; the Lua validator cannot be executed"
)

RADAR, LAUNCHER = "Kub 1S91 str", "Kub 2P25 ln"
#: A die that hits home but does not destroy: above ARM_PK times the destroy
#: fraction, below ARM_PK.
SHUTS = 0.1
#: A die that destroys the radar it hits: below ARM_PK times the fraction.
DESTROYS = HITS


def slice_with_the_sa6(*, repair=REPAIR_OFF, **fields):
    theater = build_slice_theater()
    for key, value in fields.items():
        setattr(theater.threats[SA6], key, value)
    theater.repair = repair
    return theater


def sa6_losses(campaign: Campaign) -> list:
    return [x for x in campaign.tracker.losses
            if x.entity_kind == KIND_THREAT and x.entity_id == SA6]


class TestTheRoll(unittest.TestCase):
    def test_the_dice_partition_into_destroy_shutdown_and_miss(self):
        self.assertLess(ARM_PK * ARM_DESTROY_FRACTION, SHUTS)
        self.assertLess(SHUTS, ARM_PK)
        for label, dice, radars, want in (
            ("a destroy", [DESTROYS], 1, (1, False)),
            ("a shutdown", [SHUTS], 1, (0, True)),
            ("a miss", [SURVIVES], 1, (0, False)),
            ("nothing after a shutdown", [SHUTS, DESTROYS], 1, (0, True)),
            ("nothing after the last radar", [DESTROYS, SHUTS], 1, (1, False)),
            ("all-emitter battery, then dark", [DESTROYS, SHUTS, DESTROYS], 3, (1, True)),
        ):
            with self.subTest(label):
                rng = Dice(dice)
                out = resolve_arm(rounds=len(dice), radars_alive=radars, rng=rng)
                self.assertEqual((out.radars_destroyed, out.shut_down), want)
                self.assertEqual(rng.script, [], "a missile went unrolled")

    def test_draws_are_the_missiles_fired_whatever_the_dice(self):
        after = []
        for seed in range(30):
            rng = random.Random(seed)
            out = resolve_arm(rounds=4, radars_alive=1, rng=rng)
            self.assertEqual(out.rounds_rolled, 4)
            replay = random.Random(seed)
            for _ in range(4):
                replay.random()
            self.assertEqual(rng.getstate(), replay.getstate(), seed)
            after.append(out)
        self.assertGreater(len({(o.radars_destroyed, o.shut_down) for o in after}), 1,
                           "every seed gave one outcome; vacuous")


class TestAShutdownBlindsAndEnds(unittest.TestCase):
    def test_a_shutdown_blinds_the_site_and_ends_on_time_with_no_repair(self):
        """Repair on, at Syria's rates: a shutdown must not touch it."""
        campaign = Campaign(theater=slice_with_the_sa6(repair=SiteRepair(
            radar_time=12 * 3600.0, launcher_time=24 * 3600.0)))
        to_the_brink(campaign)
        site = campaign.theater.threats[SA6]
        start = standing(campaign)
        _, dice = resolve_blue_tot(
            campaign,
            [SHUTS, SURVIVES, SURVIVES, SURVIVES]   # the first forces it down
            + [SURVIVES] * 4,                       # off the air: no exposure; bombs
        )
        self.assertEqual(dice.script, [], "a dark battery fired at the strikers")
        now = campaign.clock
        self.assertEqual(site.units_by_type, {RADAR: 1, LAUNCHER: 4})
        self.assertEqual(sa6_losses(campaign), [])
        self.assertEqual(site.dark_until, now + ARM_SHUTDOWN_TIME)
        self.assertFalse(site.engages_at(now))
        self.assertTrue(site.engages_at(now + ARM_SHUTDOWN_TIME))
        # It comes back on by itself: no unit restored, no repair work done.
        campaign.rng = random.Random(1)
        while campaign.clock < now + ARM_SHUTDOWN_TIME:
            self.assertFalse(site.engages_at(campaign.clock))
            campaign.advance(PAPER_STEP)
        self.assertTrue(site.engages_at(campaign.clock))
        self.assertEqual((site.radar_repair, site.launcher_repair), (0.0, 0.0))
        self.assertEqual(site.units_by_type, {RADAR: 1, LAUNCHER: 4})
        conserved_everywhere(self, campaign, start)

    def test_a_destruction_goes_through_repair(self):
        campaign = Campaign(theater=slice_with_the_sa6(repair=SiteRepair(radar_time=600.0)))
        to_the_brink(campaign)
        site = campaign.theater.threats[SA6]
        resolve_blue_tot(campaign, [DESTROYS, SURVIVES, SURVIVES, SURVIVES] + [SURVIVES] * 4)
        now = campaign.clock
        self.assertEqual(site.units_by_type, {LAUNCHER: 4})
        self.assertIsNone(site.dark_until)
        self.assertEqual(len(sa6_losses(campaign)), 1)
        campaign.rng = random.Random(1)
        while campaign.clock < now + 600.0 - PAPER_STEP:
            campaign.advance(PAPER_STEP)
            self.assertFalse(site.can_engage, campaign.clock - now)
        campaign.advance(2 * PAPER_STEP)
        self.assertEqual(site.units_by_type, {RADAR: 1, LAUNCHER: 4})

    def test_the_owner_hears_its_radar_is_off_the_air(self):
        campaign = Campaign(player_coalition="red")
        to_the_brink(campaign)
        frames, _ = resolve_blue_tot(campaign, [SHUTS] + [SURVIVES] * 3 + [SURVIVES] * 4)
        site = campaign.theater.threats[SA6]
        self.assertIn(
            f"Latakia North SA-6 shut down its radar under anti-radiation attack; "
            f"back on the air at {campaign.mission_time(site.dark_until):.0f}.",
            texts(frames),
        )

    def test_a_second_shutdown_moves_the_end_and_never_adds(self):
        campaign = Campaign(inventories={})
        site = campaign.theater.threats[SA6]
        campaign.clock = 1000.0
        campaign._shut_down(site)
        campaign.clock = 1300.0
        campaign._shut_down(site)
        self.assertEqual(site.dark_until, 1300.0 + ARM_SHUTDOWN_TIME)
        campaign.clock = 1100.0
        campaign._shut_down(site)
        self.assertEqual(site.dark_until, 1300.0 + ARM_SHUTDOWN_TIME)


class TestAtTheTot(unittest.TestCase):
    def test_no_sead_against_a_battery_off_the_air_at_the_tot(self):
        """Blue's first package is planned in the first step, TOT 1471 s."""
        for label, dark_until, escorted in (
            ("off the air through the TOT", 10_000.0, False),
            ("off now, back before the TOT", 100.0, True),
        ):
            with self.subTest(label):
                campaign = Campaign(theater=slice_with_the_sa6(dark_until=dark_until))
                campaign.advance(PAPER_STEP)
                package = blue_package(campaign)
                self.assertLess(campaign.clock, dark_until)
                self.assertGreater(package.t_tot, 100.0)
                self.assertEqual(package.sead is not None, escorted)

    def test_a_battery_off_the_air_at_the_tot_throws_nothing(self):
        campaign = Campaign(theater=slice_with_the_sa6(dark_until=10_000.0,
                                                       kill_probability=1.0),
                            inventories=strike_only_oob())
        to_the_brink(campaign)
        _, dice = resolve_blue_tot(campaign, [SURVIVES] * 4)  # bombs alone
        self.assertEqual(dice.script, [])
        self.assertEqual(campaign.tracker.losses_for(blue_package(campaign).strike.spawn_id), [])


class TestADarkBatteryInDcs(unittest.TestCase):
    def _spawn(self, campaign: Campaign) -> Spawn:
        site = campaign.theater.threats[SA6]
        campaign.on_hello(Hello(seq=1, t=0.0, protocol=4, theater="Syria"))
        frames = campaign.on_observer(
            ObserverReport(seq=2, t=5.0, observers=[Observer(id="p", pos=site.pos)])
        )
        return next(f for f in frames if isinstance(f, Spawn) and f.spawn_id == site.spawn_id)

    def test_a_battery_spawned_mid_shutdown_comes_with_its_emitters_off(self):
        campaign = Campaign(inventories={})
        campaign.advance(1000.0)
        site = campaign.theater.threats[SA6]
        campaign._shut_down(site)
        frame = self._spawn(campaign)
        self.assertEqual(frame.tasking, {
            "kind": "air_defence",
            "emission_off_until": campaign.mission_time(site.dark_until),
        })

    def test_once_the_radar_is_back_the_spawn_says_nothing_of_it(self):
        campaign = Campaign(inventories={})
        site = campaign.theater.threats[SA6]
        campaign._shut_down(site)
        campaign.advance(ARM_SHUTDOWN_TIME)
        self.assertEqual(self._spawn(campaign).tasking, {"kind": "air_defence"})


class TestTheShutdownIsSaved(unittest.TestCase):
    def test_a_save_mid_shutdown_continues_the_same_war(self):
        campaign = Campaign()
        to_the_brink(campaign)
        resolve_blue_tot(campaign, [SHUTS] + [SURVIVES] * 3 + [SURVIVES] * 4)
        campaign.rng = random.Random(5)
        self.assertIsNotNone(campaign.theater.threats[SA6].dark_until)
        reloaded = Campaign.from_dict(json.loads(json.dumps(campaign.to_dict())))
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())
        self.assertEqual(reloaded.theater.threats[SA6].dark_until,
                         campaign.theater.threats[SA6].dark_until)
        for _ in range(400):
            campaign.advance(PAPER_STEP)
            reloaded.advance(PAPER_STEP)
        self.assertEqual(reloaded.to_dict(), campaign.to_dict())


@requires_lua
class TestTheValidatorAsksDcs(unittest.TestCase):
    """Emission control is unverified DCS content: the in-sim run settles it."""

    def _case(self, *, fail: bool) -> tuple[dict, list[bool]]:
        run = ValidatorRun()
        self.addCleanup(run.close)
        if fail:
            run.mock.fail_emission()
        results = {r["id"]: r for r in run.run_now()["results"]}
        return (results["emission.SA-6_Kub_site"],
                run.mock.emission_history("cmpval_emission_sa6"))

    def test_it_switches_a_sam_group_off_and_on(self):
        case, calls = self._case(fail=False)
        self.assertEqual(case["status"], "OK", case.get("error"))
        self.assertFalse(case["required"])
        # It asked DCS both ways, rather than reporting calls it never made.
        self.assertEqual(calls, [False, True])

    def test_a_dcs_that_refuses_it_is_reported_not_raised(self):
        case, _ = self._case(fail=True)
        self.assertEqual(case["status"], "REJECTED")
        self.assertIn("enableEmission", case["error"])


if __name__ == "__main__":
    unittest.main()
