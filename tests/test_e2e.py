"""The whole system, over a real socket, with no DCS installed.

Everything else in `tests/` exercises one layer. This file runs the three
layers as one process pair: the real `Campaign`, the real asyncio transport on
a real loopback socket, and `tools/fake_dcs.py` speaking the mission-client
half of the protocol from the other end. It is the only test that can fail
because two layers disagreed rather than because one of them is wrong.

It exists to hold three things down:

* the loop closes -- a package is planned, instantiated, resolved from state
  snapshots, charged to the squadron, and written to disk in a form that
  reloads and keeps going;
* dropping every `event` frame changes nothing but attribution;
* a DCS restart mid-sortie costs the campaign nothing.

The last two are the ones that would otherwise regress silently, because a
campaign that has quietly started counting kills off the event stream looks
exactly like a working one until the day DCS swallows an event.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campaign.audit import attributions, strip_event_derived
from campaign.campaign import Campaign
from campaign.planner import COMPLETE
from campaign.server import CampaignServer
from campaign.protocol import Message, encode
from tools.fake_dcs import Config, FakeDCS

#: Long enough for the slice's own strike to take off, hit and land again.
#: Shorter than this and the loop looks closed while the flight is still out.
MISSION_SECONDS = 2800.0

#: Fast enough that the transport's wall-clock tick fires a handful of times
#: across the whole mission, which is the point: the campaign must advance on
#: mission time from frames, not on the tick.
TICK_PERIOD = 0.05

SQUADRON = "vfa_incirlik_f16"
DEPOT = "latakia_fuel_depot"


def _config(port: int, **overrides: object) -> Config:
    base = {
        "host": "127.0.0.1",
        "port": port,
        "seed": 1,
        "speed": 0.0,  # run flat out; nothing here is paced by a wall clock
        "duration": MISSION_SECONDS,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


async def _drive(
    campaign: Campaign, idle_first: float = 0.0, **overrides: object
) -> FakeDCS:
    """Serve `campaign` on an ephemeral port and run one fake mission at it.

    `idle_first` leaves the engine running with nothing connected, which is
    the ordinary case of starting the engine before launching DCS.
    """
    server = CampaignServer(
        campaign, host="127.0.0.1", port=0, tick_period=TICK_PERIOD
    )
    await server.start()
    try:
        await asyncio.sleep(idle_first)
        sim = FakeDCS(_config(server.port, **overrides))
        exit_code = await sim.run()
        assert exit_code == 0, f"fake_dcs gave up with exit {exit_code}"
        # The client has closed its socket; let the server notice, so
        # on_disconnect has run before anyone inspects the campaign.
        await asyncio.sleep(0.05)
    finally:
        await server.close()
    return sim


def run_loop(
    campaign: Campaign | None = None, idle_first: float = 0.0, **overrides: object
):
    """One end-to-end mission. Returns (campaign, fake DCS)."""
    campaign = Campaign() if campaign is None else campaign
    sim = asyncio.run(_drive(campaign, idle_first, **overrides))
    return campaign, sim


class TestTheLoopCloses(unittest.TestCase):
    """Step by step, the thing this whole slice exists to prove."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.campaign, cls.sim = run_loop()

    def test_a_package_was_planned_against_the_strategic_target(self):
        packages = list(self.campaign.packages.values())
        self.assertEqual(len(packages), 1, "expected exactly one strike package")
        package = packages[0]
        self.assertEqual(package.target_id, DEPOT)
        self.assertEqual(package.flight_size, 2)
        self.assertLess(package.t_takeoff, package.t_tot)
        self.assertLess(package.t_tot, package.t_rtb)

    def test_the_flight_and_the_target_were_instantiated_in_the_sim(self):
        package = next(iter(self.campaign.packages.values()))
        self.assertTrue(self.sim.saw_spawn, "the client was never told to spawn")
        self.assertGreaterEqual(
            self.sim.received["spawn"], 2, "flight and target were not both spawned"
        )
        self.assertIn(
            package.spawn_id,
            self.sim.struck,
            "the flight never reached its target inside the bubble",
        )
        outcome = self.sim.outcomes[0]
        self.assertEqual(outcome["target_units_killed"], 4)
        self.assertEqual(outcome["flight_losses"], 1)

    def test_the_outcome_came_back_and_the_target_is_rubble(self):
        depot = self.campaign.theater.targets[DEPOT]
        self.assertTrue(depot.destroyed)
        self.assertEqual(depot.units_alive, 0)
        self.assertEqual(depot.damage_fraction, 1.0)

    def test_every_loss_was_recorded_by_a_snapshot(self):
        losses = self.campaign.tracker.losses
        self.assertTrue(losses, "the campaign recorded no losses at all")
        self.assertEqual(
            len([x for x in losses if x.entity_kind == "target"]),
            4,
            "the depot's four units were not all written off",
        )
        self.assertEqual(
            len([x for x in losses if x.entity_kind == "flight"]),
            1,
            "the airframe the harness shot down was not written off",
        )

    def test_airframes_and_munitions_came_off_the_squadron(self):
        sqn = self.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(sqn.airframes_lost, 1)
        self.assertEqual(sqn.airframes_available, sqn.airframes_total - 1)
        self.assertEqual(
            sqn.munitions_expended["GBU-38"] + sqn.munitions_lost["GBU-38"],
            4,
            "the four bombs the two-ship carried are not accounted for",
        )
        sqn.check_invariant()

    def test_the_package_landed_and_gave_its_reservation_back(self):
        package = next(iter(self.campaign.packages.values()))
        self.assertEqual(package.state, COMPLETE)
        sqn = self.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(
            sqn.open_reservations,
            {},
            "a finished package is still holding inventory",
        )

    def test_the_war_is_over_and_the_client_was_told(self):
        self.assertIn(
            "All assigned strategic targets destroyed.",
            self.sim.messages,
        )

    def test_the_save_round_trips_into_a_campaign_that_can_carry_on(self):
        with self.subTest("written as JSON"):
            path = Path(self.enterContext(_tempdir())) / "campaign.json"
            self.campaign.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["save_version"], 1)
        reloaded = Campaign.load(path)
        self.assertEqual(reloaded.to_dict(), self.campaign.to_dict())
        # And it keeps running rather than merely deserialising.
        self.assertEqual(reloaded.tick(0.0), [])


class TestEventsAreAttributionOnly(unittest.TestCase):
    """The reconciliation rule, end to end and over the wire."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.with_events, _ = run_loop()
        cls.without_events, cls.silent = run_loop(drop_events=True)

    def test_not_one_event_frame_was_sent_in_the_silent_run(self):
        self.assertEqual(self.silent.sent.get("event", 0), 0)
        self.assertGreater(
            self.silent.sent.get("state", 0), 0, "no snapshots either: vacuous"
        )

    def test_the_campaign_state_is_identical(self):
        self.assertEqual(
            strip_event_derived(self.without_events.to_dict()),
            strip_event_derived(self.with_events.to_dict()),
        )

    def test_the_ledger_has_the_same_losses_in_the_same_order(self):
        loud = [
            (x.t, x.spawn_id, x.entity_id, x.cause)
            for x in self.with_events.tracker.losses
        ]
        quiet = [
            (x.t, x.spawn_id, x.entity_id, x.cause)
            for x in self.without_events.tracker.losses
        ]
        self.assertEqual(loud, quiet)
        self.assertTrue(loud, "no losses at all; the comparison proves nothing")

    def test_only_attribution_differs_and_it_really_does(self):
        loud = attributions(self.with_events.to_dict())
        quiet = attributions(self.without_events.to_dict())
        self.assertEqual(quiet, ["unknown"] * len(quiet))
        self.assertNotEqual(
            loud,
            quiet,
            "events changed nothing at all, so this test would pass even if "
            "the engine ignored them entirely",
        )


class TestReconnect(unittest.TestCase):
    def test_a_sim_restart_costs_the_campaign_no_time_and_no_inventory(self):
        restart_at = 900.0
        campaign, sim = run_loop(restart_at=restart_at)

        self.assertTrue(sim.restarted)
        self.assertEqual(sim.received["sync"], 2, "the engine did not re-sync")
        self.assertGreaterEqual(
            sim.received["spawn"], 3, "nothing was re-issued after the restart"
        )

        # The client's mission clock went back to zero; the campaign's did not.
        self.assertAlmostEqual(campaign.mission_epoch, restart_at, delta=5.0)
        self.assertAlmostEqual(
            campaign.clock, restart_at + MISSION_SECONDS, delta=5.0
        )

        package = next(iter(campaign.packages.values()))
        self.assertEqual(package.state, COMPLETE)
        self.assertTrue(campaign.theater.targets[DEPOT].destroyed)
        campaign.inventories["blue"].squadron(SQUADRON).check_invariant()


class TestTheHarnessDoesNotHideItsOwnFailures(unittest.TestCase):
    """A bug in the stand-in must not read as a dropped connection.

    `_sim_loop` treats a finished reader task as "the engine hung up" and
    `run()` reconnects, so an exception inside the reader produced a run that
    silently lost frames and still exited 0 -- which is the one thing a harness
    whose whole job is to be the reference implementation may not do.

    The case that triggered it is ordinary: a flight that dies while the engine
    is despawning it, with the destroyed group lingering in the census for more
    than one report.
    """

    def test_despawning_an_already_expired_group_leaves_the_reader_alive(self):
        with self.assertNoLogs("fake_dcs", level="ERROR"):
            campaign, sim = run_loop(dead_linger=3, flight_losses=2)
        self.assertEqual(sim.sent["hello"], 1, "the harness reconnected mid-run")
        self.assertEqual(
            sim.sent["ack"],
            sim.received["spawn"] + sim.received["despawn"],
            "the client stopped acking part-way through",
        )
        package = next(iter(campaign.packages.values()))
        self.assertIn(package.state, ("destroyed", "complete"))
        self.assertTrue(campaign.tracker.losses, "no losses reached the ledger")


class TestTheReaderNeverActsOnItsOwn(unittest.TestCase):
    """Inbound frames are handled at one point in the tick, never off the socket.

    The bug this guards: `_reader_loop` used to call `_handle` directly, and
    `_on_despawn` sends a state snapshot. That snapshot therefore raced the sim
    tick, and its `t` and contents depended on which coroutine the event loop
    resumed first. Every `_emit_event` is an await, so runs with events
    serialised themselves by accident and looked deterministic; --drop-events
    removed those awaits and exposed the race. Two identical silent runs then
    disagreed and diff_saves reported RECONCILIATION BROKEN -- blaming the
    event stream for a fault that had nothing to do with reconciliation, on the
    one command the README gives you to validate the core invariant.

    It is also a fidelity bug. The real client cannot do this: DCS Lua drains
    its socket inside timer.scheduleFunction and has no concurrent reader.
    """

    @staticmethod
    def _read(frames, then_drain: bool = False):
        """Feed `frames` through the reader; optionally drain afterwards."""

        async def go():
            sim = FakeDCS(Config())
            handled = []

            async def record(frame):
                handled.append(frame)

            sim._handle = record
            reader = asyncio.StreamReader()
            for frame in frames:
                reader.feed_data(encode(frame))
            reader.feed_eof()
            await sim._reader_loop(reader)
            queued = len(sim._inbox)
            if then_drain:
                await sim._drain_inbox()
            return queued, handled, len(sim._inbox)

        return asyncio.run(go())

    def test_reading_a_frame_queues_it_and_handles_nothing(self):
        queued, handled, _ = self._read(
            [
                Message(seq=1, t=0.0, to="blue", text="one"),
                Message(seq=2, t=0.0, to="blue", text="two"),
            ]
        )
        self.assertEqual(handled, [], "the reader handled a frame off the socket")
        self.assertEqual(queued, 2, "frames did not reach the inbox")

    def test_draining_is_what_hands_frames_over(self):
        _, handled, left = self._read(
            [Message(seq=1, t=0.0, to="blue", text="one")], then_drain=True
        )
        self.assertEqual(len(handled), 1)
        self.assertEqual(left, 0)


class TestDeterminism(unittest.TestCase):
    def test_two_identical_runs_produce_the_same_campaign(self):
        a, _ = run_loop()
        b, _ = run_loop()
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_the_silent_run_is_deterministic_too(self):
        """Determinism has to hold on the no-event path, not just the busy one.

        Weaker than it looks, and deliberately kept anyway: in-process the
        event loop schedules the same way every time, so this passed even with
        the socket race described in TestTheReaderNeverActsOnItsOwn below. The
        race only showed cross-process. This asserts the invariant; the test
        below is the one that catches the regression.
        """
        a, _ = run_loop(drop_events=True)
        b, _ = run_loop(drop_events=True)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_wall_clock_time_before_the_sim_connects_changes_nothing(self):
        """Starting the engine early must not schedule the war in the past.

        The transport ticks on a wall clock. If any of that leaked into the
        campaign clock, an engine launched before DCS would come out of the
        same mission in a different state -- and no log could reproduce it.
        """
        prompt, prompt_sim = run_loop()
        early, early_sim = run_loop(idle_first=1.0)
        self.assertEqual(early.to_dict(), prompt.to_dict())
        # And the late-joining client is still told what is already in the air.
        self.assertEqual(len(early_sim.messages), len(prompt_sim.messages))


def _tempdir():
    import tempfile

    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
