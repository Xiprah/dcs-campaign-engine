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

from campaign.api import PAPER_STEP
from campaign.attrition import CAUSE_UNOBSERVED
from campaign.audit import attributions, strip_event_derived
from campaign.campaign import Campaign
from campaign.planner import COMPLETE
from campaign.server import CampaignServer
from campaign.protocol import (
    PROTOCOL_VERSION,
    Ack,
    Hello,
    Message,
    Spawn,
    Waypoint,
    encode,
)
from tools.fake_dcs import INCIRLIK_XZ, Config, FakeDCS

#: Long enough for the slice's own strike to take off, hit and land again.
#: Shorter than this and the loop looks closed while the flight is still out.
MISSION_SECONDS = 2800.0

#: Fast enough that the transport's wall-clock tick fires a handful of times
#: across the whole mission, which is the point: the campaign must advance on
#: mission time from frames, not on the tick.
TICK_PERIOD = 0.05

SQUADRON = "vfa_incirlik_f16"
DEPOT = "latakia_fuel_depot"
SA6 = "latakia_north_sa6"


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
    campaign: Campaign,
    idle_first: float = 0.0,
    time_compression: float = 0.0,
    **overrides: object,
) -> FakeDCS:
    """Serve `campaign` on an ephemeral port and run one fake mission at it.

    `idle_first` leaves the engine running with nothing connected, which is
    the ordinary case of starting the engine before launching DCS.

    `time_compression` defaults to 0 -- the war waits for DCS -- because
    these runs are about the connected regime. At any other value the war
    moves while the harness is still connecting, by however much wall time
    that took, and two identical runs would differ by the host's scheduling.
    """
    server = CampaignServer(
        campaign,
        host="127.0.0.1",
        port=0,
        tick_period=TICK_PERIOD,
        time_compression=time_compression,
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


def blue_packages(campaign: Campaign) -> list:
    """Blue's packages, in planning order.

    Red plans too (docs/design.md, section 4), from Bassel al-Assad against
    Incirlik. The harness's observer chases blue's flight, so red's raid is
    almost always resolved on paper; what these tests pin is blue's sortie.
    """
    return [p for p in campaign.packages.values() if p.coalition == "blue"]


def blue_sortie_losses(campaign: Campaign) -> list:
    """The ledger restricted to blue's first sortie: its flight and the depot."""
    package = blue_packages(campaign)[0]
    depot = campaign.theater.targets[DEPOT]
    return [
        x for x in campaign.tracker.losses
        if x.spawn_id in (package.strike.spawn_id, depot.spawn_id)
    ]


def run_loop(
    campaign: Campaign | None = None,
    idle_first: float = 0.0,
    time_compression: float = 0.0,
    **overrides: object,
):
    """One end-to-end mission. Returns (campaign, fake DCS)."""
    campaign = Campaign() if campaign is None else campaign
    sim = asyncio.run(_drive(campaign, idle_first, time_compression, **overrides))
    return campaign, sim


class TestTheLoopCloses(unittest.TestCase):
    """Step by step, the thing this whole slice exists to prove."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.campaign, cls.sim = run_loop()

    def test_a_package_was_planned_against_the_strategic_target(self):
        packages = blue_packages(self.campaign)
        self.assertEqual(len(packages), 1, "expected exactly one blue strike package")
        package = packages[0]
        self.assertEqual(package.target_id, DEPOT)
        self.assertEqual(package.strike.flight_size, 2)
        self.assertLess(package.t_takeoff, package.t_tot)
        self.assertLess(package.t_tot, package.t_rtb)

    def test_the_flight_and_the_target_were_instantiated_in_the_sim(self):
        package = blue_packages(self.campaign)[0]
        self.assertTrue(self.sim.saw_spawn, "the client was never told to spawn")
        self.assertGreaterEqual(
            self.sim.received["spawn"], 2, "flight and target were not both spawned"
        )
        self.assertIn(
            package.strike.spawn_id,
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
        # Every loss of the sortie DCS watched. Red's raid is flown on paper,
        # out of the bubble, so its losses are the resolver's and not these.
        losses = blue_sortie_losses(self.campaign)
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
        self.assertNotIn(CAUSE_UNOBSERVED, {x.cause for x in losses})

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
        package = blue_packages(self.campaign)[0]
        self.assertEqual(package.state, COMPLETE)
        sqn = self.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(
            sqn.open_reservations,
            {},
            "a finished package is still holding inventory",
        )

    def test_the_sa6_site_on_the_route_was_instantiated_and_reported(self):
        """The threat site is bubble-instantiable like any other entity.

        The observer chases the strike flight, which passes within a few km of
        the SA-6, so the site must have been spawned, accepted by the harness
        and reported in a snapshot -- not refused and blocked for the rest of
        the war, which is what a harness that did not know the template's
        size would do.
        """
        site = self.campaign.theater.threats[SA6]
        self.assertNotIn(site.spawn_id, self.campaign.blocked)
        self.assertTrue(
            self.campaign.tracker.groups[site.spawn_id].ever_seen,
            "no snapshot ever reported the SA-6 site",
        )
        self.assertEqual(site.units_alive, site.units_initial)

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
            self.assertEqual(raw["save_version"], 9)
        reloaded = Campaign.load(path)
        self.assertEqual(reloaded.to_dict(), self.campaign.to_dict())
        # And it is an engine rather than a deserialised blob. `tick` cannot
        # show that here: the war is over, so [] is both the healthy answer and
        # the answer a stub would give. A `hello` can -- the client that
        # connects now has to be told where the campaign already is, and has to
        # be issued nothing from the war that finished, the depot being rubble
        # and the flight home. What it is issued is blue's own storage area and
        # Patriot battery: the observer brought the flight home to Incirlik,
        # so they are in the bubble, and a restarted DCS has to get them back.
        frames = reloaded.on_hello(
            Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        at_incirlik = {
            reloaded.theater.targets["incirlik_munitions_storage"].spawn_id,
            reloaded.theater.threats["incirlik_patriot"].spawn_id,
        }
        self.assertEqual(frames[0].type, "sync")
        self.assertEqual(frames[0].campaign_time, self.campaign.clock)
        self.assertEqual([f.type for f in frames[1:]], ["spawn"] * len(at_incirlik))
        self.assertEqual({f.spawn_id for f in frames[1:]}, at_incirlik)


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
        # Every observed loss is "unknown" without events. Red's raid is flown
        # on paper and its losses say "unobserved" in both runs: not an event
        # label, so dropping events must not touch it.
        causes = [x.cause for x in self.without_events.tracker.losses]
        self.assertIn(CAUSE_UNOBSERVED, causes, "no paper loss; the split is vacuous")
        self.assertEqual(
            quiet,
            [
                "unobserved" if cause == CAUSE_UNOBSERVED else "unknown"
                for cause in causes
            ],
        )
        self.assertNotEqual(
            loud,
            quiet,
            "events changed nothing at all, so this test would pass even if "
            "the engine ignored them entirely",
        )


class TestEventsAreAttributionOnlyWhenRedIsWatched(unittest.TestCase):
    """The reconciliation rule again, with red's raid inside the bubble.

    The runs above chase blue's flight, so red is only ever resolved on paper
    and the rule is tested on blue's losses alone. Here the observer never
    leaves Incirlik: DCS holds the storage area and the Patriot, and the red
    raid when it arrives, and the harness bombs the target and shoots down a
    Su-24M. Every one of those losses has to come from a snapshot, and the
    campaign has to end identical with every event dropped.
    """

    @classmethod
    def setUpClass(cls) -> None:
        parked = {"chase": False, "observer_to": INCIRLIK_XZ}
        cls.with_events, cls.loud = run_loop(**parked)
        cls.without_events, cls.quiet = run_loop(drop_events=True, **parked)

    def _red_observed(self, campaign: Campaign) -> list:
        return [
            x for x in campaign.tracker.losses
            if x.cause != CAUSE_UNOBSERVED
            and (x.coalition == "red" or x.entity_id == "incirlik_munitions_storage")
        ]

    def test_red_losses_and_red_damage_were_observed_not_rolled(self):
        observed = self._red_observed(self.with_events)
        kinds = {(x.coalition, x.entity_kind) for x in observed}
        self.assertIn(("red", "flight"), kinds, "no red aircraft was lost in DCS; vacuous")
        self.assertIn(("blue", "target"), kinds, "red damaged nothing in DCS; vacuous")

    def test_the_campaign_state_is_identical(self):
        self.assertEqual(self.quiet.sent.get("event", 0), 0)
        self.assertTrue(
            self._red_observed(self.without_events),
            "red was never watched, so this is the chase run again",
        )
        self.assertEqual(
            strip_event_derived(self.without_events.to_dict()),
            strip_event_derived(self.with_events.to_dict()),
        )

    def test_only_attribution_differs_and_it_really_does(self):
        loud = [x.attribution for x in self._red_observed(self.with_events)]
        quiet = [x.attribution for x in self._red_observed(self.without_events)]
        self.assertEqual(len(loud), len(quiet))
        self.assertEqual(quiet, ["unknown"] * len(quiet))
        self.assertNotEqual(loud, quiet, "events changed nothing; this proves nothing")


class TestAWatchedSeadElementIsTheSimsToResolve(unittest.TestCase):
    """docs/design.md, section 5: an element DCS holds is the sim's.

    Blue's SEAD element flies two minutes ahead of the strike the observer
    chases, inside the bubble, past the SA-6 the bubble also holds; the
    harness has it fire its four missiles and destroy one unit of the
    battery (`--sead-kills 1`). That loss may arrive only by snapshot, the
    missiles only by the ammunition the snapshots carry, the paper may not
    fire them again at the TOT, and the whole war must come out the same
    with every event dropped.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.with_events, cls.loud = run_loop(sead_kills=1)
        cls.without_events, cls.quiet = run_loop(sead_kills=1, drop_events=True)

    def _site_losses(self, campaign: Campaign) -> list:
        return [x for x in campaign.tracker.losses if x.entity_id == SA6]

    def test_the_harness_resolved_the_sead_element_against_the_sa6(self):
        package = blue_packages(self.with_events)[0]
        engaged = [o for o in self.loud.sead_outcomes
                   if o["flight"] == f"cmp_{package.sead.spawn_id}"]
        self.assertEqual(len(engaged), 1, self.loud.sead_outcomes)
        self.assertEqual(engaged[0]["site_units_killed"], 1)

    def test_the_site_unit_came_back_by_snapshot_and_only_once(self):
        losses = self._site_losses(self.with_events)
        self.assertEqual(len(losses), 1)
        self.assertNotEqual(losses[0].cause, CAUSE_UNOBSERVED)
        self.assertEqual(losses[0].attribution.split("/")[-1], "AGM-88C")
        site = self.with_events.theater.threats[SA6]
        self.assertEqual(site.units_alive, site.units_initial - 1)
        package = blue_packages(self.with_events)[0]
        # It fired all four in the sim, so the paper had none left to fire
        # at the battery at the TOT, whoever held what by then.
        self.assertEqual(package.sead.sim_spent, 4)
        self.assertTrue(package.weapons_released)
        engaged = [o for o in self.loud.sead_outcomes
                   if o["flight"] == f"cmp_{package.sead.spawn_id}"]
        self.assertEqual(sum(o["missiles"] for o in engaged), 4)

    def test_the_campaign_state_is_identical_without_events(self):
        self.assertEqual(self.quiet.sent.get("event", 0), 0)
        self.assertEqual(
            strip_event_derived(self.without_events.to_dict()),
            strip_event_derived(self.with_events.to_dict()),
        )
        self.assertEqual(
            [x.attribution for x in self._site_losses(self.without_events)], ["unknown"]
        )


class TestASeadElementThatFiredPartOfItsLoad(unittest.TestCase):
    """The reconciliation rule with the ammunition count doing work.

    The harness's SEAD pass fires one missile of four (`--sead-shots 1`).
    What the engine learns of it comes from the snapshots' ammunition alone,
    so the war -- the missile booked when it left the sim included -- must
    be identical with every event frame dropped.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.with_events, cls.loud = run_loop(sead_shots=1, sead_kills=1)
        cls.without_events, cls.quiet = run_loop(
            sead_shots=1, sead_kills=1, drop_events=True
        )

    def test_the_sim_fired_one_and_the_engine_counted_one(self):
        package = blue_packages(self.with_events)[0]
        engaged = [o for o in self.loud.sead_outcomes
                   if o["flight"] == f"cmp_{package.sead.spawn_id}"]
        self.assertEqual(sum(o["missiles"] for o in engaged), 1, self.loud.sead_outcomes)
        self.assertEqual(package.sead.sim_spent, 1)
        sead = self.with_events.inventories["blue"].squadron("vfa_incirlik_f16_sead")
        self.assertEqual(
            sead.munitions_expended.get("AGM-88C", 0) + sead.munitions_lost.get("AGM-88C", 0),
            4,
            "the four missiles the two-ship carried are not accounted for",
        )
        sead.check_invariant()

    def test_the_campaign_state_is_identical_without_events(self):
        self.assertEqual(self.quiet.sent.get("event", 0), 0)
        self.assertEqual(
            strip_event_derived(self.without_events.to_dict()),
            strip_event_derived(self.with_events.to_dict()),
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

        package = blue_packages(campaign)[0]
        self.assertEqual(package.state, COMPLETE)
        self.assertTrue(campaign.theater.targets[DEPOT].destroyed)

        # Inventory, against the run that was never interrupted. On its own
        # check_invariant says only that the books balance, and they balance
        # just as well if the reconnect wrote off the jet that came home: two
        # lost, ten available, and the package still completes.
        uninterrupted, _ = run_loop()
        sqn = campaign.inventories["blue"].squadron(SQUADRON)
        clean = uninterrupted.inventories["blue"].squadron(SQUADRON)
        sqn.check_invariant()
        self.assertEqual(sqn.airframes_lost, 1)
        self.assertEqual(sqn.airframes_lost, clean.airframes_lost)
        self.assertEqual(sqn.airframes_available, clean.airframes_available)
        self.assertEqual(sqn.munitions_expended, clean.munitions_expended)
        self.assertEqual(sqn.munitions_lost, clean.munitions_lost)
        self.assertEqual(
            sqn.munitions_expended["GBU-38"] + sqn.munitions_lost["GBU-38"],
            4,
            "the four bombs the two-ship carried are not accounted for",
        )

        # And the ledger itself: a restart may neither invent a casualty nor
        # forget one, whatever the totals say.
        #
        # Blue's sortie's ledger, which is what the restart can be compared
        # on. Red's raid cannot be: the restarted mission puts its player back
        # at Incirlik, inside red's target area, so in this run DCS watches
        # the raid arrive and in the uninterrupted one nobody does and it is
        # rolled on paper. Two different authorities, each correct, for what
        # is not the same observation -- not the restart costing anything.
        ledger = [(x.cause, x.entity_kind) for x in blue_sortie_losses(campaign)]
        self.assertEqual(
            ledger,
            [(x.cause, x.entity_kind) for x in blue_sortie_losses(uninterrupted)],
        )
        self.assertEqual(len(ledger), 5, "the reference run's ledger changed shape")


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
        package = blue_packages(campaign)[0]
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


class TestTheHarnessRefusesWhatTheClientRefuses(unittest.TestCase):
    """tools/fake_dcs.py may be no more forgiving than the Lua client.

    A spawn the harness accepts and mission/campaign_client.lua refuses is a
    failure the whole offline loop is blind to. That gap is exactly how a
    strike flight once spawned with no attack task while every test here
    stayed green. The error text is the client's, word for word, so the two
    refusals are checked against the same strings.
    """

    def _spawn(self, config: dict | None = None, **overrides: object):
        fields: dict = dict(
            seq=5,
            t=0.0,
            ref=5,
            spawn_id="beef",
            coalition="blue",
            category="plane",
            template="F-16C_strike_jdam",
            units=2,
            position=(1000.0, 5000.0, 2000.0),
        )
        fields.update(overrides)
        frame = Spawn(**fields)

        async def go():
            sim = FakeDCS(Config(**(config or {})))
            sent: list = []

            async def record(out):
                sent.append(out)

            sim._send = record
            await sim._on_spawn(frame)
            acks = [f for f in sent if isinstance(f, Ack) and f.ref == frame.ref]
            return sim, acks

        # The harness logs every spawn and every refusal; assertLogs keeps it
        # off the test output and proves it said something either way.
        with self.assertLogs("fake_dcs", level="INFO"):
            return asyncio.run(go())

    def test_more_units_than_the_template_holds_is_refused_not_clamped(self):
        sim, acks = self._spawn(units=3)
        self.assertEqual(len(acks), 1)
        self.assertFalse(acks[0].ok, "the harness built a three-ship from a two-ship")
        self.assertIn(
            "units 3 exceeds template F-16C_strike_jdam capacity of 2", acks[0].error
        )
        self.assertEqual(sim.groups, {})

    def test_a_count_that_is_not_a_positive_integer_is_refused(self):
        for units in (0, -1, 1.5, True):
            with self.subTest(units=units):
                sim, acks = self._spawn(units=units)
                self.assertFalse(acks[0].ok)
                self.assertIn("bad units", acks[0].error)
                self.assertEqual(sim.groups, {})

    def test_exactly_the_count_asked_for_is_built(self):
        sim, acks = self._spawn(units=1)
        self.assertTrue(acks[0].ok)
        self.assertEqual(sim.groups["beef"].units, 1)
        self.assertEqual(sim.groups["beef"].units_initial, 1)

    def test_template_units_sets_the_capacity(self):
        sim, acks = self._spawn(
            config={"template_units": {"F-16C_strike_jdam": 4}}, units=4
        )
        self.assertTrue(acks[0].ok)
        self.assertEqual(sim.groups["beef"].units, 4)

    def test_the_sa6_battery_is_accepted_at_its_full_size_and_no_larger(self):
        sam = dict(
            spawn_id="5a60",
            coalition="red",
            category="ground",
            template="SA-6_Kub_site",
            position=(16000.0, 0.0, 25000.0),
            tasking={"kind": "air_defence"},
        )
        sim, acks = self._spawn(units=5, **sam)
        self.assertTrue(acks[0].ok, acks[0].error)
        self.assertEqual(sim.groups["5a60"].units, 5)
        sim, acks = self._spawn(units=6, **sam)
        self.assertFalse(acks[0].ok)
        self.assertIn("units 6 exceeds template SA-6_Kub_site capacity of 5", acks[0].error)

    def test_a_malformed_airdrome_id_is_refused(self):
        sim, acks = self._spawn(
            route=[Waypoint(pos=(0.0, 0.0, 0.0), airdrome_id="Incirlik")]
        )
        self.assertFalse(acks[0].ok)
        self.assertIn("malformed airdrome_id", acks[0].error)

    def test_a_sead_element_is_accepted_as_the_client_accepts_it(self):
        """Both sides' SEAD templates, at the client's capacity and no more."""
        for coalition, template in (("blue", "F-16C_sead_harm"), ("red", "Su-24M_sead_kh58")):
            with self.subTest(template=template):
                tasking = {"kind": "sead", "targets": ["cmp_0004"], "tot": 900.0,
                           "callsign": "VIPER SEAD"}
                sim, acks = self._spawn(coalition=coalition, template=template,
                                        tasking=tasking)
                self.assertTrue(acks[0].ok, acks[0].error)
                self.assertEqual(sim.groups["beef"].tasking, tasking)
                sim, acks = self._spawn(coalition=coalition, template=template,
                                        tasking=tasking, units=3)
                self.assertFalse(acks[0].ok)
                self.assertIn(f"units 3 exceeds template {template} capacity of 2",
                              acks[0].error)


class TestTheHarnessFiresOnlyWhatItCarries(unittest.TestCase):
    """tools/fake_dcs.py may be no more forgiving about ammunition either.

    The Lua client builds every flight with empty pylons today; a harness
    whose SEAD pass destroyed a radar with no missiles aboard would prove
    things about a sim that does not exist. And what it fires has to show in
    the next snapshot, because that is all the engine may learn it from.
    """

    def _sim(self, **config):
        sim = FakeDCS(Config(**config))
        sent: list = []

        async def record(out):
            sent.append(out)

        sim._send = record
        return sim, sent

    def _setup(self, sim, **site_tasking):
        site = dict(seq=1, t=0.0, ref=1, spawn_id="5a60", coalition="red",
                    category="ground", template="SA-6_Kub_site", units=5,
                    position=(16000.0, 0.0, 25000.0),
                    tasking={"kind": "air_defence", **site_tasking})
        sead = dict(seq=2, t=0.0, ref=2, spawn_id="5ead", coalition="blue",
                    category="plane", template="F-16C_sead_harm", units=2,
                    position=(17000.0, 6000.0, 26000.0),
                    tasking={"kind": "sead", "targets": ["cmp_5a60"], "tot": 0.0})
        asyncio.run(sim._on_spawn(Spawn(**site)))
        asyncio.run(sim._on_spawn(Spawn(**sead)))
        return sim.groups["5ead"], sim.groups["5a60"]

    def _pass(self, **config):
        sim, sent = self._sim(**config)
        with self.assertLogs("fake_dcs", level="INFO"):
            flight, site = self._setup(sim)
            asyncio.run(sim._resolve_strikes())
            asyncio.run(sim._send_state())
        state = [f for f in sent if f.type == "state"][-1]
        reported = {g.spawn_id: g for g in state.groups}
        return flight, site, reported

    def test_a_battery_off_the_air_gives_the_missiles_nothing_to_home_on(self):
        """As DCS under emission control would: a spawn carrying
        `emission_off_until` is dark until then, and a SEAD pass in that
        window destroys nothing (docs/design.md, section 7)."""
        sim, _ = self._sim(loadout=2, sead_kills=1, sead_shots=1)
        with self.assertLogs("fake_dcs", level="INFO"):
            flight, site = self._setup(sim, emission_off_until=1_000.0)
            asyncio.run(sim._resolve_strikes())
        self.assertTrue(flight.resolved, "the pass was never made")
        self.assertEqual(site.units, 5)

    def test_with_empty_pylons_a_sead_pass_fires_and_destroys_nothing(self):
        flight, site, reported = self._pass(loadout=0, sead_kills=1)
        self.assertTrue(flight.resolved, "the pass was never made")
        self.assertEqual(site.units, 5)
        self.assertEqual(reported["5ead"].ammo, {})
        self.assertEqual(reported["5ead"].ammo_initial, {})

    def test_what_a_sead_pass_fires_comes_off_the_next_snapshot(self):
        flight, site, reported = self._pass(loadout=2, sead_kills=1, sead_shots=1)
        self.assertEqual(site.units, 4)
        self.assertEqual(reported["5ead"].ammo, {"AGM-88C": 3})
        self.assertEqual(reported["5ead"].ammo_initial, {"AGM-88C": 4})
        # Only aircraft report ammunition, as only the Lua client's AIRBORNE
        # categories do.
        self.assertIsNone(reported["5a60"].ammo)
        self.assertIsNone(reported["5a60"].ammo_initial)

    def test_an_unarmed_strike_destroys_nothing(self):
        sim, _ = self._sim(loadout=0)
        target = dict(seq=1, t=0.0, ref=1, spawn_id="d090", coalition="red",
                      category="structure", template="fuel_depot_medium", units=4,
                      position=(0.0, 0.0, 0.0), tasking={"kind": "static"})
        strike = dict(seq=2, t=0.0, ref=2, spawn_id="57e1", coalition="blue",
                      category="plane", template="F-16C_strike_jdam", units=2,
                      position=(100.0, 6000.0, 100.0),
                      tasking={"kind": "strike", "target": "cmp_d090", "tot": 0.0})
        with self.assertLogs("fake_dcs", level="INFO"):
            asyncio.run(sim._on_spawn(Spawn(**target)))
            asyncio.run(sim._on_spawn(Spawn(**strike)))
            asyncio.run(sim._resolve_strikes())
        self.assertTrue(sim.groups["57e1"].resolved)
        self.assertEqual(sim.groups["d090"].units, 4)


class TestAnOlderClientIsRefused(unittest.TestCase):
    """The real engine behind the real transport closes on an old hello.

    Nothing may be written first: not a sync, and not a spawn the v1 client
    would build from its template regardless of the unit count it cannot
    read, nor one a v2 client would fly without ever reporting what it fired.
    """

    def _refused(self, version: int) -> tuple[Campaign, bytes, bool, list[str]]:
        async def go():
            campaign = Campaign()
            server = CampaignServer(
                campaign, host="127.0.0.1", port=0, tick_period=TICK_PERIOD
            )
            await server.start()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                writer.write(encode(Hello(seq=1, t=0.0, protocol=version, theater="Syria")))
                await writer.drain()
                received, closed = b"", False
                try:
                    while chunk := await asyncio.wait_for(reader.read(65536), 2.0):
                        received += chunk
                    closed = True
                except TimeoutError:
                    pass
                writer.close()
                return campaign, received, closed
            finally:
                await server.close()

        with self.assertLogs("campaign.server", level="ERROR") as logs:
            campaign, received, closed = asyncio.run(go())
        return campaign, received, closed, logs.output

    def test_the_connection_closes_and_nothing_is_written(self):
        campaign, received, closed, output = self._refused(1)
        self.assertEqual(received, b"", "the engine answered a v1 client")
        self.assertTrue(closed, "the engine left a v1 client connected")
        self.assertFalse(campaign.connected)
        self.assertTrue(
            any("client protocol 1 != engine 4" in line for line in output),
            output,
        )

    def test_a_version_2_client_is_refused_the_same_way(self):
        """A v2 client reports no ammunition, and the engine would read every
        SEAD element it held as having fired its whole load."""
        campaign, received, closed, output = self._refused(2)
        self.assertEqual(received, b"", "the engine answered a v2 client")
        self.assertTrue(closed, "the engine left a v2 client connected")
        self.assertFalse(campaign.connected)
        self.assertTrue(
            any("client protocol 2 != engine 4" in line for line in output),
            output,
        )

    def test_a_version_3_client_is_refused_the_same_way(self):
        """A v3 client reports a site's surviving units as a bare count, and
        the engine could not tell a radar the sim destroyed from a launcher;
        it builds a battery without a radar back with one."""
        campaign, received, closed, output = self._refused(3)
        self.assertEqual(received, b"", "the engine answered a v3 client")
        self.assertTrue(closed, "the engine left a v3 client connected")
        self.assertFalse(campaign.connected)
        self.assertTrue(
            any("client protocol 3 != engine 4" in line for line in output),
            output,
        )


class TestTheWarMovesBeforeDCSArrives(unittest.TestCase):
    """The offline clock, through the real transport and a real hello.

    The engine runs at high time compression with nothing connected, so the
    war is some hundreds of paper seconds old when the harness connects at
    mission time zero. How many is up to the host's scheduling; that it is a
    whole number of paper steps, and that the client is rebased onto it
    rather than rewinding it, is not.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.campaign, cls.sim = run_loop(idle_first=0.3, time_compression=2000.0)

    def test_the_war_had_moved_in_whole_paper_steps_before_the_hello(self):
        epoch = self.campaign.mission_epoch
        self.assertGreater(epoch, 0.0, "the war did not move while DCS was away")
        self.assertEqual(epoch % PAPER_STEP, 0.0, "a partial step reached the campaign")

    def test_the_client_joined_the_war_where_it_was(self):
        # At least the whole mission on top of the offline stretch, never
        # less: the hello rebased mission time zero onto the war's clock
        # instead of rewinding it. (More, usually -- the war carries on in
        # paper steps once the harness hangs up, until the server closes.)
        self.assertGreaterEqual(
            self.campaign.clock,
            self.campaign.mission_epoch + MISSION_SECONDS - 5.0,
        )

    def test_the_loop_still_closes(self):
        self.assertTrue(self.campaign.theater.targets[DEPOT].destroyed)
        package = blue_packages(self.campaign)[0]
        self.assertEqual(package.state, COMPLETE)
        self.campaign.inventories["blue"].check_invariant()


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

        Run at time compression 0, so the only way wall time could reach the
        campaign is through `tick(now)`. The war moving offline on purpose is
        TestTheWarMovesBeforeDCSArrives's business.
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
