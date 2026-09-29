"""The unobserved resolver, and the campaign bankruptcy it exists to stop.

Driven with one observer far from the war -- an idle dedicated server, or a
player flying somewhere else, which is the normal case in a dynamic campaign --
the engine used to frag package after package, spend the squadron's entire
munitions stock, destroy nothing, record no losses, and stall permanently
holding a save that passed every invariant check.

Two rules are under test here:

  * an entity DCS never instantiated is resolved on paper, by the seeded RNG;
  * an entity DCS *is* holding is never resolved on paper, because a snapshot
    is its sole authority and a target killed twice is worse than one killed
    late.
"""

from __future__ import annotations

import random
import unittest

from campaign.attrition import CAUSE_UNOBSERVED, KIND_TARGET
from campaign.campaign import Campaign
from campaign.resolver import StrikeOutcome, resolve_strike
from tests.test_domain import (
    OBSERVER_AT_TARGET,
    OBSERVER_FAR_AWAY,
    FakeDCS,
    drive,
)

DEPOT = "latakia_fuel_depot"
SQUADRON = "vfa_incirlik_f16"


class TestResolveStrike(unittest.TestCase):
    def test_the_same_seed_gives_the_same_answer(self):
        a = resolve_strike(rounds=8, target_units_alive=4, rng=random.Random(5))
        b = resolve_strike(rounds=8, target_units_alive=4, rng=random.Random(5))
        self.assertEqual(a, b)

    def test_draws_depend_only_on_rounds_never_on_the_target(self):
        """RNG consumption must not vary with the outcome.

        Every round is rolled even after the target has nothing left to lose.
        If the resolver stopped early on an over-killed target it would consume
        fewer draws, and every subsequent roll in the campaign would shift --
        so a replay would diverge from the first flattened target onward, which
        is exactly the kind of bug a seeded campaign exists to make impossible.
        """
        drained = []
        for alive in (1, 4, 99):
            rng = random.Random(11)
            resolve_strike(rounds=6, target_units_alive=alive, rng=rng)
            drained.append(rng.random())
        self.assertEqual(len(set(drained)), 1, "draw count varied with the target")

    def test_it_cannot_kill_more_than_is_there(self):
        outcome = resolve_strike(
            rounds=50, target_units_alive=3, rng=random.Random(2), weapon_pk=1.0
        )
        self.assertEqual(outcome.units_killed, 3)

    def test_nothing_released_achieves_nothing(self):
        self.assertEqual(
            resolve_strike(rounds=0, target_units_alive=4, rng=random.Random(1)),
            StrikeOutcome(units_killed=0, rounds_rolled=0),
        )

    def test_a_dead_target_absorbs_nothing_further(self):
        self.assertTrue(
            resolve_strike(
                rounds=8, target_units_alive=0, rng=random.Random(1)
            ).missed
        )


class TestTheWarProgressesWithNobodyWatching(unittest.TestCase):
    """The bankruptcy regression.

    Before the resolver this test's campaign spent every bomb it had and left
    the depot at full strength.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.campaign = Campaign()
        dcs = FakeDCS(campaign=cls.campaign, deliver_events=False)
        drive(
            cls.campaign,
            observer_positions=OBSERVER_FAR_AWAY,
            damages=[],
            deliver_events=False,
            start=0,
            duration=12_000,
            dcs=dcs,
        )
        cls.dcs = dcs

    def test_the_target_actually_took_damage(self):
        depot = self.campaign.theater.targets[DEPOT]
        self.assertLess(
            depot.units_alive,
            depot.units_initial,
            "twelve thousand seconds of unobserved strikes achieved nothing",
        )

    def test_the_damage_was_booked_as_unobserved_not_as_a_sighting(self):
        losses = [
            loss
            for loss in self.campaign.tracker.losses
            if loss.entity_kind == KIND_TARGET
        ]
        self.assertTrue(losses, "no target losses reached the ledger")
        self.assertTrue(
            all(loss.cause == CAUSE_UNOBSERVED for loss in losses),
            "a loss nobody observed was booked as though a snapshot saw it",
        )

    def test_the_ordnance_bought_something(self):
        sqn = self.campaign.inventories["blue"].squadron(SQUADRON)
        spent = sqn.munitions_expended.get("GBU-38", 0) + sqn.munitions_lost.get(
            "GBU-38", 0
        )
        depot = self.campaign.theater.targets[DEPOT]
        killed = depot.units_initial - depot.units_alive
        self.assertGreater(spent, 0, "nothing was expended, so this proves nothing")
        self.assertGreater(killed / spent, 0.0)
        sqn.check_invariant()


class TestNoDoubleJeopardy(unittest.TestCase):
    def test_a_target_dcs_is_holding_is_not_also_resolved_on_paper(self):
        """The sim and the resolver must never both kill the same thing.

        With the observer sitting on the target, the depot is instantiated, so
        every unit it loses must come from a snapshot. A paper loss appearing
        here would mean the depot could be destroyed twice over -- once by the
        dice and once by DCS -- and the campaign would write off units that
        never existed.
        """
        campaign = Campaign()
        dcs = FakeDCS(campaign=campaign, deliver_events=True)
        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[],
            deliver_events=True,
            start=0,
            duration=3000,
            dcs=dcs,
        )
        self.assertIn(
            campaign.theater.targets[DEPOT].spawn_id,
            campaign.live,
            "the depot was never instantiated, so this proves nothing",
        )
        # Blue's strike on the depot is what the observer is watching. Red's
        # raid on Incirlik, 160 km away, is resolved on paper in the same war,
        # and rightly: nothing there is instantiated.
        watched = {campaign.theater.targets[DEPOT].spawn_id} | {
            p.spawn_id for p in campaign.packages.values() if p.coalition == "blue"
        }
        paper = [
            loss
            for loss in campaign.tracker.losses
            if loss.cause == CAUSE_UNOBSERVED and loss.spawn_id in watched
        ]
        self.assertEqual(paper, [], "an observed target was resolved on paper too")
        self.assertTrue(
            [p for p in campaign.packages.values()
             if p.coalition == "blue" and p.weapons_released],
            "blue's strike never reached its TOT, so this proves nothing",
        )


class TestTheStallIsAnnounced(unittest.TestCase):
    def test_a_campaign_that_cannot_task_says_so(self):
        """A silent stall is indistinguishable from a finished war."""
        campaign = Campaign()
        sqn = campaign.inventories["blue"].squadron(SQUADRON)
        sqn.airframes_available = 0
        dcs = FakeDCS(campaign=campaign, deliver_events=False)
        drive(
            campaign,
            observer_positions=OBSERVER_FAR_AWAY,
            damages=[],
            deliver_events=False,
            start=0,
            duration=300,
            dcs=dcs,
        )
        said = [
            f.text for f in dcs.downlink if getattr(f, "text", "").startswith("Cannot task")
        ]
        self.assertTrue(said, "the campaign stalled without telling anyone")
        self.assertEqual(len(said), 1, "the stall message repeats every tick")


if __name__ == "__main__":
    unittest.main()
