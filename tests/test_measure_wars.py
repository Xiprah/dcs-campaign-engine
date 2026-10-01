"""tools/measure_wars.py counts what the campaign's own books say.

docs/design.md quotes its tables, so a war it miscounts is a design decision
taken on a wrong number. What is pinned: every airframe a squadron lost is
charged to exactly one package that reached its TOT, in the right element,
and the depth and quarter tables account for every package and every loss.
It reads the campaign and never writes: the war it measures is the war
`Campaign.advance` flies on its own.

The war is tests/test_both_sides.py's bleeding slice: sites as far-reaching
as the missiles fired at them, so both sides' SEAD elements are shot at too,
and batteries of forty emitters that no number of hits can blind for good.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from campaign.api import PAPER_STEP
from campaign.campaign import Campaign
from campaign.oob import launch_range
from tests.test_both_sides import DEPOT, PATRIOT, SA6, STORAGE, slice_with

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import measure_wars  # noqa: E402  (tools/ is not a package)


def bleeding_slice() -> Campaign:
    theater = slice_with(
        **{
            DEPOT: {"units_initial": 40, "units_alive": 40},
            STORAGE: {"units_initial": 40, "units_alive": 40},
            SA6: {"template": "SA-15_Tor_site", "kill_probability": 0.3,
                  "units_initial": 40, "units_alive": 40,
                  "engagement_radius": launch_range("AGM-88C")},
            PATRIOT: {"template": "SA-15_Tor_site", "kill_probability": 0.3,
                      "units_initial": 40, "units_alive": 40,
                      "engagement_radius": launch_range("Kh-58U")},
        }
    )
    return Campaign(theater=theater, seed=4)


class TestTheCountsAreTheBooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.campaign = bleeding_slice()
        cls.war = measure_wars.fly(cls.campaign, max_days=100_000 / 86_400.0)

    def test_every_lost_airframe_is_charged_to_one_package_element(self):
        for side in measure_wars.SIDES:
            with self.subTest(side=side):
                figures = self.war["sides"][side]
                books = sum(s.airframes_lost
                            for s in self.campaign.inventories[side].squadrons.values())
                self.assertEqual(figures["airframes_lost"], books)
                self.assertEqual(figures["strike_lost"] + figures["sead_lost"], books)
                # Not vacuous: both elements bled.
                self.assertGreater(figures["strike_lost"], 0)
                self.assertGreater(figures["sead_lost"], 0)

    def test_quarters_and_escorts_account_for_every_package_and_loss(self):
        for side in measure_wars.SIDES:
            with self.subTest(side=side):
                figures = self.war["sides"][side]
                quarters = figures["by_quarter"]
                self.assertEqual(sum(n for n, _ in quarters), figures["packages"])
                self.assertEqual(sum(n for _, n in quarters), figures["airframes_lost"])
                escort = figures["by_escort"]
                self.assertEqual(sum(n for n, _ in escort), figures["packages"])
                self.assertEqual(escort[1][0], figures["escorted"])
                self.assertEqual(sum(n for _, n in escort), figures["airframes_lost"])

    def test_a_war_measured_is_the_war_flown_unmeasured(self):
        unmeasured = bleeding_slice()
        for _ in range(int(round(self.campaign.clock / PAPER_STEP))):
            unmeasured.advance(PAPER_STEP)
        self.assertEqual(unmeasured.to_dict(), self.campaign.to_dict())


class TestTheDepthTable(unittest.TestCase):
    def test_syria_targets_are_all_of_a_known_depth(self):
        """A target of a size the table does not know would drop out of it."""
        war = measure_wars.new_syria_war(0)
        sizes = {t.units_initial for t in war.theater.targets.values()}
        self.assertEqual(sizes, set(measure_wars.DEPTH_OF_SIZE))


if __name__ == "__main__":
    unittest.main()
