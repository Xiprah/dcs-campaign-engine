"""A flight that lands must not be written off as a flight that died.

DCS deletes AI aircraft a short while after they touch down, so their handles
vanish exactly the way a destroyed unit's does. The client reported a missing
handle as `alive=false, units=0`, the engine obeyed the reconciliation rule
correctly, and a package that made it home was booked as a total loss -- one
airframe at a time, silently, forever.

This is the sharpest edge of the rule that makes the engine trustworthy: the
campaign believes its census absolutely, so the census has to be right. A
sensor that cannot tell a parked aircraft from a smoking hole will destroy a
squadron on paper while every aircraft sits safely on the ramp.

The offline harness could never catch this, because it only lands when the
engine's own route says to and the engine despawns the flight first.
"""

from __future__ import annotations

import unittest

from campaign.protocol import group_name
from tests.test_mission_client import (
    requires_lua,
    sortie_with_observer_over_the_target,
)

SQUADRON = "vfa_incirlik_f16"


def _flight_in_the_air(test: unittest.TestCase):
    """A sortie run far enough that the two-ship exists inside DCS."""
    mission = sortie_with_observer_over_the_target()
    test.addCleanup(mission.close)
    mission.run_until(
        lambda m: bool(
            m.campaign.packages
            and m.mock.group(group_name(m.package.spawn_id)) is not None
        ),
        limit=1400.0,
    )
    flight = mission.flight_group()
    units = sorted(u for u in mission.mock.unit_names() if u.startswith(flight))
    test.assertEqual(len(units), 2, "the two-ship was never instantiated")
    return mission, flight, units


def _land(mission, units) -> None:
    for name in units:
        mission.mock.fire_event(
            mission.mock.event_id("S_EVENT_LAND"), initiator=mission.mock.unit(name)
        )


@requires_lua
class TestALandedFlightIsNotALostFlight(unittest.TestCase):
    def test_dcs_deleting_a_landed_flight_costs_the_squadron_nothing(self):
        mission, flight, units = _flight_in_the_air(self)

        _land(mission, units)
        # DCS reaps the aircraft now that they are down. To the client this is
        # indistinguishable from the group being shot out of the sky, which is
        # precisely the problem.
        mission.mock.kill_group(flight)
        mission.step(20)

        squadron = mission.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(
            squadron.airframes_lost,
            0,
            "a flight that landed was booked as a combat loss",
        )
        self.assertEqual(
            mission.campaign.tracker.units_alive(mission.package.spawn_id),
            2,
            "the campaign lost aircraft that were parked on the ramp",
        )
        self.assertEqual(
            [loss.to_dict() for loss in mission.campaign.tracker.losses],
            [],
            "the ledger recorded losses for a flight that came home",
        )
        mission.assert_lua_was_clean(self)

    def test_a_flight_that_never_landed_is_still_written_off(self):
        """The control. Without this the fix is just "stop reporting deaths".

        Same disappearance, no landing beforehand: the campaign must take the
        full loss. A census that cannot report a death is worse than one that
        reports too many, because the war then cannot be lost.
        """
        mission, flight, _units = _flight_in_the_air(self)

        mission.mock.kill_group(flight)
        mission.step(20)

        squadron = mission.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(
            squadron.airframes_lost, 2, "a flight shot down cost the squadron nothing"
        )
        self.assertEqual(
            mission.campaign.tracker.units_alive(mission.package.spawn_id), 0
        )
        mission.assert_lua_was_clean(self)

    def test_one_lands_and_one_does_not(self):
        """The mixed case, which is the ordinary one for a two-ship."""
        mission, flight, units = _flight_in_the_air(self)

        _land(mission, [units[0]])
        mission.mock.kill_group(flight)
        mission.step(20)

        squadron = mission.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(squadron.airframes_lost, 1)
        self.assertEqual(
            mission.campaign.tracker.units_alive(mission.package.spawn_id), 1
        )
        mission.assert_lua_was_clean(self)

    def test_an_aircraft_killed_after_landing_is_a_loss_again(self):
        """Caught on the ramp. Landing is not permanent immunity."""
        mission, flight, units = _flight_in_the_air(self)

        _land(mission, units)
        for name in units:
            mission.mock.fire_event(
                mission.mock.event_id("S_EVENT_DEAD"),
                initiator=mission.mock.unit(name),
            )
        mission.mock.kill_group(flight)
        mission.step(20)

        squadron = mission.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(
            squadron.airframes_lost,
            2,
            "aircraft destroyed on the ground were counted as recovered",
        )
        mission.assert_lua_was_clean(self)


if __name__ == "__main__":
    unittest.main()
