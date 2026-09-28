"""The real Lua mission client, executed.

Everything else in `tests/` exercises Python. `mission/campaign_client.lua` is
the half of this system DCS runs, and until this file existed it had only ever
been *compiled* -- which is why an adversarial review could find a spawn that
attached no attack task at all and the whole offline suite stayed green:
`tools/fake_dcs.py` resolves a strike geometrically, so it cannot notice that
the flight was never tasked to drop anything.

So this file runs the actual Lua, in a real Lua 5.1 interpreter, inside a mock
DCS environment (`tests/dcsmock/`), talking to a real `campaign.Campaign` over
a real loopback TCP socket. The only things faked are DCS itself and the
passage of mission time, which the mock's `timer` owns outright: no wall clock
and no unseeded randomness appear anywhere below.

The tests are ordered by what they would catch:

* `TestFullSortie` -- the loop closes with the real client driving it.
* `TestStrikeIsActuallyTasked` -- the regression. A spawned strike flight must
  carry an attack task aimed at the right thing, whether or not its target has
  been instantiated yet. An untasked flight flies to the target and does
  nothing, and nothing else in the suite can see it.
* `TestReconnect` -- a dropped socket must not leave duplicated or resurrected
  groups behind.
* `TestFramesCarryMissionTimeNotCampaignTime` -- after a restart the two
  clocks differ by a whole session, and the wire carries the client's.
* `TestARejectedSpawnIsNeverRetried`,
  `TestAFlightLostEarlyIsClosedOutAtOnce`,
  `TestACampaignThatCannotTaskStandsStill` -- campaign guards whose failure
  mode is a frame storm, a blocked planner or a runaway counter rather than a
  wrong answer, driven through the real client because that is where the cost
  actually lands.
* `TestTheEventHotPathIsCheapBeforeItIsThorough`,
  `TestSceneryDoesNotPutANumberOnTheWire`, `TestTheHostMustBeAnAddress` --
  the sim thread: what the client may do per event, what it may put on the
  wire, and the last call that could block it.
* `TestJsonEmptyTables` / `TestJsonStrings` -- `{}` versus `[]`, and the
  strings entity names are made of, round-tripped against
  `campaign/protocol.py`'s own decoder.
* `TestAnAttritedEntityComesBackAttrited` -- re-instantiation must not hand
  the campaign back an airframe it has already written off.
* `TestClientRefusesBadSpawns`, `TestSpawnShapes`, `TestFramingAndBackpressure`,
  `TestNonBlockingDiscipline`, `TestEventsAndObservers`,
  `TestReloadingTheScript`, `TestHelloCarriesTheMissionEpoch`,
  `TestDeterminism` -- the rest of the client's contract.

Requires `lupa` (which embeds Lua 5.1). The whole module skips without it, so
the main suite still passes on a machine that has never heard of it.
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campaign.campaign import Campaign
from campaign.planner import COMPLETE, CRUISE_ALTITUDE, DESTROYED
from campaign.protocol import (
    Ack,
    Despawn,
    Event,
    FrameBuffer,
    Hello,
    Message,
    ObserverReport,
    Spawn,
    StateReport,
    Sync,
    Waypoint,
    decode_downlink,
    decode_uplink,
    encode,
    group_name,
)
from campaign.theater import ground_distance

try:
    import lupa  # noqa: F401

    HAVE_LUPA = True
except ImportError:  # pragma: no cover - exercised on hosts without lupa
    HAVE_LUPA = False

if HAVE_LUPA:
    from tests.dcsmock import DCSMock

requires_lua = unittest.skipUnless(
    HAVE_LUPA, "lupa is not installed; the Lua client cannot be executed"
)

SQUADRON = "vfa_incirlik_f16"
DEPOT = "latakia_fuel_depot"

#: One mission second per client tick. The client's own default is 0.1 s; a
#: whole sortie at that rate is 28,000 round trips for no extra coverage,
#: and every cadence that matters (observer 5 s, state 30 s) is far slower.
TICK = 1.0

#: Long enough for the slice's strike to launch, hit and recover.
SORTIE_SECONDS = 2700.0

#: Far outside the bubble from everything in the slice's theater, so the
#: engine instantiates nothing at all and a test can own the downlink.
NOWHERE = (900_000.0, 5_000.0, 900_000.0)


# --------------------------------------------------------------------------
# The engine end of the wire
# --------------------------------------------------------------------------


class EngineHarness:
    """A real listening socket in front of a real `Campaign`.

    Deliberately not `campaign.server.CampaignServer`: that runs on asyncio
    and a wall-clock tick, and this file's whole point is that the test owns
    the clock. The frame handling is the same handful of lines, decoded and
    encoded through `campaign.protocol` exactly as the server does it.
    """

    def __init__(self, campaign: Campaign) -> None:
        self.campaign = campaign
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]

        self.conn: socket.socket | None = None
        self.frames = FrameBuffer()
        self.outbox = b""

        self.uplink: list[object] = []
        self.downlink: list[object] = []
        self.connections = 0
        self.disconnects = 0
        #: Set when the harness should stop answering, so a test can watch
        #: the client's sync timeout.
        self.mute = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.drop()
        self.listener.close()

    def drop(self) -> None:
        """Hang up on the client, as a DCS-side network failure would."""
        if self.conn is None:
            return
        self.conn.close()
        self.conn = None
        self.frames = FrameBuffer()
        self.outbox = b""
        self.disconnects += 1
        self.campaign.on_disconnect()

    # -- pump --------------------------------------------------------------

    def pump(self, rounds: int = 2) -> None:
        for _ in range(rounds):
            self._accept()
            self._read()
            self._write()

    def _accept(self) -> None:
        if self.conn is not None:
            return
        try:
            conn, _ = self.listener.accept()
        except BlockingIOError:
            return
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.conn = conn
        self.frames = FrameBuffer()
        self.outbox = b""
        self.connections += 1

    def _read(self) -> None:
        if self.conn is None:
            return
        while True:
            try:
                chunk = self.conn.recv(65536)
            except BlockingIOError:
                return
            except OSError:
                self.drop()
                return
            if chunk == b"":
                self.drop()
                return
            for raw in self.frames.feed(chunk):
                self._handle(raw)

    def _handle(self, raw: bytes) -> None:
        frame = decode_uplink(raw)
        self.uplink.append(frame)
        if self.mute:
            return
        handler = {
            Hello: self.campaign.on_hello,
            ObserverReport: self.campaign.on_observer,
            Event: self.campaign.on_event,
            StateReport: self.campaign.on_state,
            Ack: self.campaign.on_ack,
        }[type(frame)]
        self.send(handler(frame))

    def send(self, frames) -> None:
        for frame in frames:
            self.downlink.append(frame)
            self.outbox += encode(frame)

    def _write(self) -> None:
        if self.conn is None or not self.outbox:
            return
        try:
            sent = self.conn.send(self.outbox)
        except BlockingIOError:
            return
        except OSError:
            self.drop()
            return
        self.outbox = self.outbox[sent:]

    # -- queries -----------------------------------------------------------

    def uplink_of(self, kind: type) -> list:
        return [f for f in self.uplink if isinstance(f, kind)]

    def downlink_of(self, kind: type) -> list:
        return [f for f in self.downlink if isinstance(f, kind)]

    def messages(self) -> list[str]:
        return [f.text for f in self.downlink_of(Message)]


# --------------------------------------------------------------------------
# One mission
# --------------------------------------------------------------------------


class Mission:
    """A mock DCS, a campaign, and the socket between them."""

    def __init__(
        self,
        *,
        observer_pos: tuple[float, float, float],
        campaign: Campaign | None = None,
        tick: float = TICK,
        config: dict | None = None,
    ) -> None:
        self.campaign = campaign if campaign is not None else Campaign()
        self.engine = EngineHarness(self.campaign)
        self.tick = tick
        self.mock = DCSMock(port=self.engine.port, tick=tick, config=config)
        self.observer = "player_hornet_1"
        self.mock.add_player(self.observer, "Jakem", side=2, pos=observer_pos)

    def close(self) -> None:
        self.mock.stop()
        self.engine.close()

    def step(self, steps: int = 1, hook=None) -> None:
        for _ in range(steps):
            self.mock.advance(self.tick)
            self.engine.pump()
            if hook is not None:
                hook(self)

    def run_to(self, mission_time: float, hook=None) -> None:
        while self.mock.time < mission_time:
            self.step(hook=hook)

    def run_until(self, predicate, limit: float, hook=None) -> bool:
        while self.mock.time < limit:
            if predicate(self):
                return True
            self.step(hook=hook)
        return predicate(self)

    # -- convenience -------------------------------------------------------

    @property
    def package(self):
        return next(iter(self.campaign.packages.values()))

    @property
    def depot(self):
        return self.campaign.theater.targets[DEPOT]

    def flight_group(self) -> str:
        return group_name(self.package.spawn_id)

    def assert_lua_was_clean(self, test: unittest.TestCase) -> None:
        test.assertEqual(
            self.mock.scheduler_errors(),
            [],
            "the client's scheduled tick raised out of pcall",
        )
        test.assertEqual(
            self.mock.logs("error"), [], "the client logged an error"
        )


def sortie_with_observer_over_the_target(campaign: Campaign | None = None) -> Mission:
    """The ordinary case: a human orbiting the target the engine tasks.

    Parking the observer on the depot puts the target in the bubble from the
    first observer frame and pulls the flight in as it runs in, which is the
    sequence the campaign was designed around.
    """
    depot_pos = (-3_000.0, CRUISE_ALTITUDE, 41_000.0)
    return Mission(observer_pos=depot_pos, campaign=campaign)


# --------------------------------------------------------------------------
# 1. The whole sortie, driven by the real client
# --------------------------------------------------------------------------


@requires_lua
class TestFullSortie(unittest.TestCase):
    """Connect, sync, spawn, report, resolve, despawn -- through the Lua."""

    @classmethod
    def setUpClass(cls) -> None:
        mission = sortie_with_observer_over_the_target()
        cls.mission = mission
        cls.addClassCleanup(mission.close)

        cls.saw_flight_group = False
        cls.flight_units_at_spawn = 0
        cls.killed_wingman = False
        cls.killed_depot = False

        def resolve(m: Mission) -> None:
            """Stand in for weapons and damage; nothing else here does."""
            package = next(iter(m.campaign.packages.values()), None)
            if package is None:
                return
            flight = group_name(package.spawn_id)
            if m.mock.group(flight) is not None and not cls.saw_flight_group:
                cls.saw_flight_group = True
                data = m.mock.group_data(flight)
                cls.flight_units_at_spawn = len(data["units"])

            # At the planned time on target, the depot comes apart and one
            # jet does not come home. Both are facts only a snapshot may
            # carry back, which is the point of doing it this way.
            if m.campaign.clock >= package.t_tot and not cls.killed_depot:
                cls.killed_depot = True
                for name in list(m.mock.static_names()):
                    m.mock.kill_static(name)
                units = [u for u in m.mock.unit_names() if u.startswith(flight)]
                if units:
                    cls.killed_wingman = True
                    m.mock.kill_unit(sorted(units)[-1])

        mission.run_to(SORTIE_SECONDS, hook=resolve)
        # Let the last despawn ack and the final messages land.
        mission.step(5)

    def test_the_client_connected_and_synced_once(self):
        hellos = self.mission.engine.uplink_of(Hello)
        self.assertEqual(len(hellos), 1, "the client did not say hello exactly once")
        self.assertEqual(hellos[0].protocol, 1)
        self.assertEqual(hellos[0].seq, 1, "hello must be seq 1 on a connection")
        self.assertEqual(hellos[0].theater, "Syria")
        self.assertGreater(
            hellos[0].mission_start_epoch, 0, "mission_start_epoch was never derived"
        )
        self.assertEqual(len(self.mission.engine.downlink_of(Sync)), 1)
        self.assertTrue(self.mission.mock.status()["synced"])

    def test_the_lua_never_raised_and_never_logged_an_error(self):
        self.mission.assert_lua_was_clean(self)

    def test_the_target_and_the_flight_were_both_instantiated(self):
        spawns = self.mission.engine.downlink_of(Spawn)
        self.assertGreaterEqual(len(spawns), 2, "flight and target were not both sent")
        self.assertTrue(
            self.saw_flight_group, "the strike flight never existed inside DCS"
        )
        self.assertEqual(
            self.flight_units_at_spawn, 2, "the two-ship did not spawn as a two-ship"
        )
        kinds = {call["kind"] for call in self.mission.mock.spawn_calls()}
        self.assertEqual(kinds, {"group", "static"})
        statics = [c for c in self.mission.mock.spawn_calls() if c["kind"] == "static"]
        self.assertEqual(
            len(statics), 4, "the four-object depot was not built as four objects"
        )

    def test_every_spawn_was_acked_ok(self):
        acks = {a.ref: a for a in self.mission.engine.uplink_of(Ack)}
        refs = [s.ref for s in self.mission.engine.downlink_of(Spawn)]
        self.assertTrue(refs)
        for ref in refs:
            self.assertIn(ref, acks, f"spawn ref {ref} was never acked")
            self.assertTrue(acks[ref].ok, f"spawn ref {ref} was refused: {acks[ref].error}")

    def test_state_snapshots_carried_the_ground_truth(self):
        states = self.mission.engine.uplink_of(StateReport)
        self.assertGreater(len(states), 5, "hardly any snapshots were sent")
        seen = {g.spawn_id for s in states for g in s.groups}
        self.assertIn(self.mission.package.spawn_id, seen)
        self.assertIn(self.mission.depot.spawn_id, seen)
        # The depot's four objects were counted as four, not as one.
        depot_counts = {
            g.units_initial
            for s in states
            for g in s.groups
            if g.spawn_id == self.mission.depot.spawn_id
        }
        self.assertEqual(depot_counts, {4})

    def test_the_target_is_rubble_and_the_campaign_knows_it(self):
        self.assertTrue(self.killed_depot, "the strike was never resolved")
        self.assertTrue(self.mission.depot.destroyed)
        self.assertEqual(self.mission.depot.units_alive, 0)
        target_losses = [
            x for x in self.mission.campaign.tracker.losses if x.entity_kind == "target"
        ]
        self.assertEqual(len(target_losses), 4)

    def test_the_lost_airframe_came_off_the_squadron(self):
        self.assertTrue(self.killed_wingman, "no aircraft was ever shot down")
        sqn = self.mission.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(sqn.airframes_lost, 1)
        self.assertEqual(sqn.airframes_available, sqn.airframes_total - 1)
        sqn.check_invariant()

    def test_the_package_closed_and_released_its_reservation(self):
        self.assertEqual(self.mission.package.state, COMPLETE)
        sqn = self.mission.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertEqual(sqn.open_reservations, {})

    def test_everything_the_engine_owned_was_despawned_from_dcs(self):
        self.assertTrue(self.mission.engine.downlink_of(Despawn))
        leftovers = [
            n
            for n in self.mission.mock.group_names() + self.mission.mock.static_names()
            if n.startswith("cmp_")
        ]
        self.assertEqual(leftovers, [], "engine-owned objects outlived their despawn")
        self.assertEqual(self.mission.mock.status()["live_spawns"], 0)

    def test_a_despawn_reports_ground_truth_before_it_destroys_anything(self):
        """The ordering the protocol calls out: state, then Group.destroy.

        Backwards, a flight recalled from the bubble reads as dead in the
        snapshot that follows, and the engine books it as a combat loss.
        """
        stream = self.mission.engine.uplink
        despawn_refs = {d.ref for d in self.mission.engine.downlink_of(Despawn)}
        checked = 0
        for i, frame in enumerate(stream):
            if not isinstance(frame, Ack) or frame.ref not in despawn_refs:
                continue
            previous = stream[i - 1]
            self.assertIsInstance(
                previous,
                StateReport,
                "a despawn was acked without a snapshot immediately before it",
            )
            checked += 1
        self.assertGreater(checked, 0, "no despawn was acked at all")

    def test_the_player_was_reported_as_an_observer_throughout(self):
        reports = self.mission.engine.uplink_of(ObserverReport)
        self.assertGreater(len(reports), 10)
        self.assertTrue(all(len(r.observers) == 1 for r in reports))
        self.assertEqual(reports[0].observers[0].id, "player:Jakem")

    def test_the_pilot_facing_messages_reached_the_cockpit(self):
        shown = [m["text"] for m in self.mission.mock.messages()]
        self.assertTrue(shown, "not one message was displayed in DCS")
        self.assertTrue(
            any("fragged" in text for text in shown),
            f"the tasking message never reached the player: {shown}",
        )
        self.assertTrue(any("destroyed" in text for text in shown), shown)
        self.assertTrue(
            all(m["to"] == "blue" for m in self.mission.mock.messages()),
            "a message went to the wrong coalition",
        )

    def test_the_campaign_clock_tracked_mission_time(self):
        """The campaign advances on frame `t`, so it trails by one cadence.

        Anything more than an observer period behind means the campaign has
        stopped being driven by mission time, which is the failure that makes
        a time-compressed run produce a different war.
        """
        self.assertAlmostEqual(
            self.mission.campaign.clock,
            self.mission.mock.time,
            delta=self.mission.campaign.observer_period + 2.0 * TICK,
        )


# --------------------------------------------------------------------------
# 2. The regression: a strike that is not tasked to strike anything
# --------------------------------------------------------------------------


@requires_lua
class TestStrikeIsActuallyTasked(unittest.TestCase):
    """A spawned strike flight must carry an attack task, aimed at the target.

    This is the bug class the adversarial review found: the engine spawns the
    package before its target is instantiated, the client's target lookup
    resolves nothing, and the flight goes out with an empty ComboTask. In DCS
    it flies the route and drops nothing. `tools/fake_dcs.py` cannot see it,
    because it resolves a strike from geometry rather than from tasking.

    Both orders are tested, because only one of them was broken and the fix
    has to hold for both.
    """

    def _spawn_the_flight_only(self) -> tuple[Mission, str, dict]:
        """Drive a campaign until it spawns a flight with no target in DCS.

        The observer sits on the departure airbase, 165 km from the depot, so
        the bubble instantiates the flight at takeoff and never instantiates
        the target -- which is the ordinary case, not a contrived one.
        """
        incirlik = (142_000.0, CRUISE_ALTITUDE, -38_000.0)
        mission = Mission(observer_pos=incirlik)
        self.addCleanup(mission.close)
        found = mission.run_until(
            lambda m: bool(
                m.campaign.packages
                and m.mock.group(group_name(m.package.spawn_id)) is not None
            ),
            limit=1200.0,
        )
        self.assertTrue(found, "the flight was never instantiated at its own airbase")
        name = mission.flight_group()
        self.assertEqual(
            [n for n in mission.mock.static_names() if n.startswith("cmp_")],
            [],
            "the target was instantiated after all; this test proves nothing",
        )
        return mission, name, mission.mock.group_data(name)

    def test_a_flight_spawned_before_its_target_is_still_tasked_to_attack(self):
        mission, name, _data = self._spawn_the_flight_only()
        tasks = mission.mock.attack_tasks(name)

        self.assertTrue(
            tasks,
            "the strike flight spawned with an empty task list: in DCS it "
            "would fly its route and drop nothing",
        )
        attacks = [t for t in tasks if t["id"] in ("Bombing", "AttackGroup", "AttackUnit")]
        self.assertEqual(
            len(attacks), 1, f"expected exactly one attack task, got {tasks}"
        )
        task = attacks[0]
        self.assertTrue(task["enabled"])
        self.assertEqual(task["id"], "Bombing")

        depot = mission.depot
        point = task["params"]["point"]
        # DCS map coordinates: the vec3's x and z.
        self.assertAlmostEqual(point["x"], depot.pos[0], delta=1.0)
        self.assertAlmostEqual(point["y"], depot.pos[2], delta=1.0)

    def test_the_attack_task_hangs_on_the_target_waypoint_not_the_last_one(self):
        """Tasked on the landing waypoint, the flight attacks after it lands."""
        mission, name, data = self._spawn_the_flight_only()
        per_waypoint = mission.mock.waypoint_tasks(name)
        self.assertGreaterEqual(len(per_waypoint), 3, data["route"])
        tasked = [i for i, tasks in enumerate(per_waypoint) if tasks]
        self.assertEqual(
            tasked, [1], "the attack task is not on the engine's attack waypoint"
        )
        points = data["route"]["points"]
        self.assertEqual(points[-1]["type"], "Land")
        self.assertEqual(points[-1]["task"]["params"]["tasks"], {})

        depot = mission.depot
        attack_wp = points[1]
        self.assertAlmostEqual(attack_wp["x"], depot.pos[0], delta=1.0)
        self.assertAlmostEqual(attack_wp["y"], depot.pos[2], delta=1.0)

    def test_a_flight_spawned_after_its_target_aims_at_the_real_object(self):
        """With the depot already built, the aim point is the object itself."""
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        found = mission.run_until(
            lambda m: bool(
                m.campaign.packages
                and m.mock.group(group_name(m.package.spawn_id)) is not None
            ),
            limit=1400.0,
        )
        self.assertTrue(found, "the flight never entered the bubble")
        statics = [n for n in mission.mock.static_names() if n.startswith("cmp_")]
        self.assertEqual(len(statics), 4, "the depot was not up before the flight")

        tasks = mission.mock.attack_tasks(mission.flight_group())
        attacks = [t for t in tasks if t["id"] in ("Bombing", "AttackGroup")]
        self.assertEqual(len(attacks), 1, f"expected one attack task, got {tasks}")
        point = attacks[0]["params"]["point"]
        aim = mission.mock.static_point(sorted(statics)[0])
        self.assertAlmostEqual(point["x"], aim[0], delta=0.5)
        self.assertAlmostEqual(point["y"], aim[2], delta=0.5)
        self.assertLess(
            ground_distance(
                (point["x"], 0.0, point["y"]), mission.depot.pos
            ),
            200.0,
            "the aim point is not on the depot",
        )


# --------------------------------------------------------------------------
# 3. Reconnect
# --------------------------------------------------------------------------


@requires_lua
class TestReconnect(unittest.TestCase):
    """A dropped socket mid-sortie, and what the client owes afterwards."""

    def test_the_client_reconnects_and_does_not_duplicate_or_resurrect_groups(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)

        mission.run_until(
            lambda m: bool(
                m.campaign.packages
                and m.mock.group(group_name(m.package.spawn_id)) is not None
            ),
            limit=1400.0,
        )
        before = sorted(
            n
            for n in mission.mock.group_names() + mission.mock.static_names()
            if n.startswith("cmp_")
        )
        self.assertTrue(before, "nothing was live when the socket was dropped")
        flight = mission.flight_group()
        group_id_before = mission.mock.group_id(flight)

        mission.engine.drop()
        # One tick for the client to notice, then let it back off and retry.
        mission.step(3)
        self.assertEqual(
            [
                n
                for n in mission.mock.group_names() + mission.mock.static_names()
                if n.startswith("cmp_")
            ],
            [],
            "the client kept engine-owned objects after losing the engine; "
            "the re-issued spawns would collide with them",
        )
        self.assertEqual(mission.mock.status()["live_spawns"], 0)

        reconnected = mission.run_until(
            lambda m: m.engine.connections >= 2 and m.mock.status()["synced"],
            limit=mission.mock.time + 120.0,
        )
        self.assertTrue(reconnected, "the client never reconnected")

        hellos = mission.engine.uplink_of(Hello)
        self.assertEqual(len(hellos), 2, "the client did not re-send hello")
        self.assertEqual(hellos[1].seq, 1, "seq did not restart on the new connection")
        self.assertEqual(len(mission.engine.downlink_of(Sync)), 2)

        # The engine re-issues everything that should be live; the client
        # must rebuild it, once each, with no duplicate-spawn refusals.
        mission.run_until(
            lambda m: sorted(
                n
                for n in m.mock.group_names() + m.mock.static_names()
                if n.startswith("cmp_")
            )
            == before,
            limit=mission.mock.time + 120.0,
        )
        after = sorted(
            n
            for n in mission.mock.group_names() + mission.mock.static_names()
            if n.startswith("cmp_")
        )
        self.assertEqual(after, before, "the world did not come back as it was")

        refused = [a for a in mission.engine.uplink_of(Ack) if not a.ok]
        self.assertEqual(
            [a.error for a in refused], [], "a re-issued spawn was refused"
        )

        # Rebuilt, not resurrected: the old handles are gone and the new
        # group is a different DCS object under the same name.
        self.assertNotEqual(
            mission.mock.group_id(flight),
            group_id_before,
            "the client handed back the pre-drop group object",
        )
        units = [n for n in mission.mock.unit_names() if n.startswith(flight)]
        self.assertEqual(
            len(units), 2, f"the flight came back with the wrong unit list: {units}"
        )
        mission.assert_lua_was_clean(self)

    def test_the_sortie_still_completes_after_a_mid_air_reconnect(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        state = {"dropped": False, "resolved": False}

        def hook(m: Mission) -> None:
            package = next(iter(m.campaign.packages.values()), None)
            if package is None:
                return
            flight = group_name(package.spawn_id)
            if not state["dropped"] and m.mock.group(flight) is not None:
                state["dropped"] = True
                m.engine.drop()
                return
            if m.campaign.clock >= package.t_tot and not state["resolved"]:
                if not m.mock.static_names():
                    return
                state["resolved"] = True
                for name in list(m.mock.static_names()):
                    m.mock.kill_static(name)

        mission.run_to(SORTIE_SECONDS, hook=hook)
        mission.step(5)

        self.assertTrue(state["dropped"] and state["resolved"])
        self.assertEqual(mission.engine.connections, 2)
        self.assertTrue(mission.depot.destroyed, "the strike did not land")
        self.assertEqual(mission.package.state, COMPLETE)
        mission.campaign.inventories["blue"].squadron(SQUADRON).check_invariant()
        mission.assert_lua_was_clean(self)


# --------------------------------------------------------------------------
# 4. The empty-table trap
# --------------------------------------------------------------------------


@requires_lua
class TestFramesCarryMissionTimeNotCampaignTime(unittest.TestCase):
    """Every envelope `t` is `timer.getTime()`, per docs/protocol.md.

    The campaign runs on its own clock and rebases the mission clock onto it
    at each hello, so the two diverge by exactly one restart's worth of war.
    A client that has just restarted knows nothing about campaign time: the
    engine converts back on the way out, and if it stopped doing so every
    spawn, despawn and message would be stamped ahead of the client's clock
    and the player would be briefed a time on target that is not the one the
    flight is flying to.
    """

    def test_a_restarted_mission_is_told_its_own_clock(self):
        first = sortie_with_observer_over_the_target()
        self.addCleanup(first.close)
        first.run_until(
            lambda m: bool(
                m.campaign.packages
                and m.mock.group(group_name(m.package.spawn_id)) is not None
            ),
            limit=1400.0,
        )
        campaign = first.campaign
        package = first.package
        self.assertGreater(campaign.clock, 600.0, "the campaign clock never moved")
        first.engine.drop()
        first.step(3)
        first.close()

        # DCS restarted: a new mission whose clock starts at zero, the same
        # campaign, everything that should be live re-issued.
        second = Mission(
            observer_pos=(-3_000.0, CRUISE_ALTITUDE, 41_000.0), campaign=campaign
        )
        self.addCleanup(second.close)
        second.run_until(
            lambda m: bool(
                m.mock.status()["synced"]
                and [
                    s
                    for s in m.engine.downlink_of(Spawn)
                    if s.spawn_id == package.spawn_id
                ]
            ),
            limit=120.0,
        )

        sync = second.engine.downlink_of(Sync)[-1]
        self.assertAlmostEqual(
            sync.campaign_time,
            campaign.clock,
            delta=second.tick,
            msg="sync did not carry the campaign clock",
        )
        self.assertLess(
            sync.t,
            60.0,
            "the engine stamped the new mission's sync with campaign time: "
            "t=%r against a mission clock of %r" % (sync.t, second.mock.time),
        )

        spawn = [
            s for s in second.engine.downlink_of(Spawn) if s.spawn_id == package.spawn_id
        ][-1]
        self.assertLessEqual(
            abs(spawn.t - second.mock.time),
            5 * second.tick,
            "a re-issued spawn was stamped %r against a mission clock of %r"
            % (spawn.t, second.mock.time),
        )
        self.assertAlmostEqual(
            spawn.tasking["tot"],
            package.t_tot - campaign.mission_epoch,
            places=6,
            msg="the flight was briefed a TOT on the campaign's clock; the "
            "cockpit reads the mission's",
        )
        second.assert_lua_was_clean(self)


@requires_lua
class TestJsonEmptyTables(unittest.TestCase):
    """`{}` versus `[]`, checked against campaign/protocol.py's own decoder.

    Lua has one table type, so an empty `route` and an empty `tasking` are
    the same value until something says otherwise. Encode them the same way
    and the engine either iterates a dict as a list or reads a list as a
    mapping -- silently, and only when the payload happens to be empty,
    which is the steady state of a quiet server.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mock = DCSMock(port=1, autostart=False)
        cls.json = cls.mock.json

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mock.stop()

    def round_trip(self, frame_bytes: bytes) -> str:
        """Decode with mission/json.lua, re-encode, return the Lua's text."""
        decoded = self.json.decode(frame_bytes.decode("utf-8").rstrip("\n"))
        self.assertIsNotNone(decoded, "mission/json.lua could not decode the frame")
        text = self.json.encode(decoded)
        self.assertIsNotNone(text, "mission/json.lua could not re-encode the frame")
        return text

    def test_an_empty_tasking_stays_an_object_and_an_empty_route_stays_a_list(self):
        frame = Spawn(
            seq=7,
            t=1.0,
            ref=7,
            spawn_id="a91f",
            coalition="red",
            category="structure",
            template="fuel_depot_medium",
            position=(1.0, 2.0, 3.0),
            route=[],
            tasking={},
        )
        text = self.round_trip(encode(frame))
        self.assertIn('"route":[]', text)
        self.assertIn('"tasking":{}', text)

        # And the engine's own decoder must still recognise what came back.
        again = decode_downlink(text)
        self.assertEqual(again.route, [])
        self.assertEqual(again.tasking, {})
        self.assertEqual(again, frame)

    def test_a_populated_route_and_tasking_round_trip_unchanged(self):
        frame = Spawn(
            seq=9,
            t=2.0,
            ref=9,
            spawn_id="0002",
            coalition="blue",
            category="plane",
            template="F-16C_strike_jdam",
            position=(1.0, 2.0, 3.0),
            heading=1.25,
            route=[Waypoint(pos=(4.0, 5.0, 6.0), alt=6000.0, speed=220.0, action="attack")],
            tasking={"kind": "strike", "target": "cmp_0001", "units": 2},
        )
        self.assertEqual(decode_downlink(self.round_trip(encode(frame))), frame)

    def test_the_client_sends_an_empty_state_report_as_a_list_not_a_map(self):
        """The case that actually bites: a quiet server, and no live spawns."""
        mission = Mission(observer_pos=NOWHERE)
        self.addCleanup(mission.close)
        # Nothing within a bubble radius of the observer, so the client has
        # no spawns at all: the steady state of a quiet server.
        mission.run_until(
            lambda m: len(m.engine.uplink_of(StateReport)) >= 2, limit=200.0
        )
        states = mission.engine.uplink_of(StateReport)
        self.assertTrue(states, "no state report was ever sent")
        self.assertEqual(mission.mock.status()["live_spawns"], 0)
        empty = [s for s in states if not s.groups]
        self.assertEqual(
            len(empty), len(states), "something was instantiated after all"
        )
        self.assertIsInstance(empty[0].groups, list)

        observers = mission.engine.uplink_of(ObserverReport)
        self.assertTrue(observers)
        self.assertIsInstance(observers[0].observers, list)

    def test_an_empty_lua_table_without_a_tag_encodes_as_an_object(self):
        """The documented fallback, pinned so a rewrite cannot flip it."""
        self.assertEqual(self.json.encode(self.mock.lua.eval("{}")), "{}")
        self.assertEqual(self.json.encode(self.json.array(self.mock.lua.eval("{}"))), "[]")
        self.assertEqual(
            self.json.encode(self.json.object(self.mock.lua.eval("{}"))), "{}"
        )

    def test_a_decoded_null_survives_as_a_null_rather_than_erasing_its_key(self):
        decoded = self.json.decode('{"error":null,"ok":true}')
        self.assertTrue(self.json.isnull(decoded["error"]))
        self.assertEqual(self.json.encode(decoded), '{"error":null,"ok":true}')


# --------------------------------------------------------------------------
# 5. The rest of the client's contract
# --------------------------------------------------------------------------


@requires_lua
class TestClientRefusesBadSpawns(unittest.TestCase):
    """Every refusal path, since each one is a campaign-corruption guard."""

    def setUp(self) -> None:
        # Out of the bubble, so the engine instantiates nothing of its own
        # and every group in DCS got there from a frame this test injected.
        self.campaign = Campaign()
        self.mission = Mission(observer_pos=NOWHERE, campaign=self.campaign)
        self.addCleanup(self.mission.close)
        self.mission.run_until(
            lambda m: m.mock.status()["synced"], limit=60.0
        )
        self.assertTrue(self.mission.mock.status()["synced"], "never synced")
        self.assertEqual(
            self.mission.mock.status()["live_spawns"], 0, "the bubble was not empty"
        )
        self.ref = 9000

    def inject(self, frame) -> None:
        """Put a frame on the wire behind the campaign's back."""
        self.mission.engine.send([frame])
        self.mission.step(3)

    def spawn(self, **overrides):
        self.ref += 1
        fields = dict(
            seq=self.ref,
            t=self.mission.mock.time,
            ref=self.ref,
            spawn_id="beef",
            coalition="blue",
            category="plane",
            template="F-16C_strike_jdam",
            position=(1000.0, 5000.0, 2000.0),
            heading=0.0,
            route=[],
            tasking={},
        )
        fields.update(overrides)
        return Spawn(**fields)

    def ack_for(self, ref: int) -> Ack:
        acks = [a for a in self.mission.engine.uplink_of(Ack) if a.ref == ref]
        self.assertTrue(acks, f"no ack for ref {ref}")
        return acks[-1]

    def test_an_unknown_template_is_refused_and_nothing_is_created(self):
        frame = self.spawn(template="Su-34_strike")
        self.inject(frame)
        ack = self.ack_for(frame.ref)
        self.assertFalse(ack.ok)
        self.assertIn("unknown template", ack.error or "")
        self.assertEqual(self.mission.mock.group_names(), [])

    def test_an_unknown_coalition_is_refused(self):
        frame = self.spawn(coalition="orange")
        self.inject(frame)
        self.assertIn("unknown coalition", self.ack_for(frame.ref).error or "")

    def test_an_unknown_category_is_refused(self):
        frame = self.spawn(category="submarine")
        self.inject(frame)
        self.assertIn("unknown category", self.ack_for(frame.ref).error or "")

    def test_a_duplicate_spawn_id_is_refused_and_the_first_group_survives(self):
        first = self.spawn(spawn_id="cafe")
        self.inject(first)
        self.assertTrue(self.ack_for(first.ref).ok)
        group_id = self.mission.mock.group_id("cmp_cafe")

        second = self.spawn(spawn_id="cafe")
        self.inject(second)
        ack = self.ack_for(second.ref)
        self.assertFalse(ack.ok)
        self.assertIn("duplicate spawn_id", ack.error or "")
        self.assertEqual(
            self.mission.mock.group_id("cmp_cafe"),
            group_id,
            "the duplicate replaced the original group",
        )

    def test_a_group_dcs_silently_failed_to_create_is_refused_not_registered(self):
        """The corruption case: ack ok for a group that does not exist.

        DCS returns nil from addGroup for a table it dislikes rather than
        raising. Registering the spawn anyway makes the next census report
        zero units, and the engine writes off a flight nobody flew.
        """
        self.mission.mock.fail_next_group_spawn(silently=True)
        frame = self.spawn(spawn_id="dead")
        self.inject(frame)
        ack = self.ack_for(frame.ref)
        self.assertFalse(ack.ok, "the client acked a group DCS never created")
        self.assertIn("created nothing", ack.error or "")
        self.assertEqual(
            self.mission.mock.status()["live_spawns"],
            0,
            "the client registered a spawn_id for a group DCS never built; "
            "the next census would write the whole flight off as a loss",
        )
        self.mission.mock.allow_group_spawns()

    def test_a_despawn_for_something_unknown_is_a_successful_no_op(self):
        frame = Despawn(
            seq=8000, t=self.mission.mock.time, ref=8000, spawn_id="ffff", reason="x"
        )
        self.inject(frame)
        self.assertTrue(self.ack_for(8000).ok)

    def test_an_unknown_frame_type_with_a_ref_is_refused_rather_than_ignored(self):
        self.mission.engine.outbox += b'{"type":"nonsense","seq":1,"t":1.0,"ref":7654}\n'
        self.mission.step(3)
        self.assertIn("unknown frame type", self.ack_for(7654).error or "")

    def test_a_garbage_frame_is_logged_and_the_connection_survives(self):
        self.mission.engine.outbox += b"{not json at all\n"
        self.mission.step(3)
        self.assertTrue(
            any("decode" in msg for msg in self.mission.mock.logs("error")),
            self.mission.mock.logs(),
        )
        self.assertEqual(self.mission.mock.status()["phase"], "open")
        self.assertEqual(self.mission.engine.connections, 1)


@requires_lua
class TestNonBlockingDiscipline(unittest.TestCase):
    """The rule the whole client is shaped around, checked mechanically."""

    def test_the_client_only_ever_asks_for_a_zero_timeout(self):
        # LuaSocketBridge.settimeout raises on anything but 0; a blocking
        # call would therefore fail the sortie rather than pass quietly.
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        mission.run_until(lambda m: m.mock.status()["synced"], limit=60.0)
        self.assertTrue(mission.mock.status()["synced"])
        mission.assert_lua_was_clean(self)

    def test_a_socket_that_only_takes_part_of_a_frame_still_sends_it_intact(self):
        """Short writes are ordinary, and the client does index arithmetic.

        `flush_outbox` tracks its position with the absolute byte index
        LuaSocket reports, which is the kind of off-by-one that produces a
        corrupt frame rather than a crash -- and a corrupt frame is a lost
        state report, which is a loss the campaign never books.
        """
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        mission.mock.sockets.send_limit = 24  # a fraction of any real frame

        mission.run_until(lambda m: m.mock.status()["synced"], limit=200.0)
        self.assertTrue(
            mission.mock.status()["synced"],
            "the client never got a whole hello out in 24-byte pieces",
        )
        mission.run_until(
            lambda m: len(m.engine.uplink_of(StateReport)) >= 3, limit=600.0
        )
        # Every frame that arrived parsed, which is what decode_uplink in the
        # harness already guaranteed; what matters is that none went missing.
        self.assertGreaterEqual(len(mission.engine.uplink_of(StateReport)), 3)
        self.assertGreaterEqual(len(mission.engine.uplink_of(ObserverReport)), 5)
        self.assertEqual(mission.engine.connections, 1, "the client gave up")
        seqs = [f.seq for f in mission.engine.uplink]
        self.assertEqual(
            seqs, list(range(1, len(seqs) + 1)), "the uplink stream has a gap"
        )
        # The backlog a throttled socket builds is bounded, and drains the
        # moment the socket stops throttling.
        self.assertLess(
            mission.mock.status()["outbox_bytes"],
            8 * 1024,
            "a throttled socket built an unbounded backlog",
        )
        mission.mock.sockets.send_limit = None
        mission.step(5)
        self.assertEqual(mission.mock.status()["outbox_bytes"], 0)
        mission.assert_lua_was_clean(self)

    def test_the_client_gives_up_on_an_engine_that_never_answers_hello(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        mission.engine.mute = True
        mission.run_until(lambda m: bool(m.engine.uplink_of(Hello)), limit=60.0)
        self.assertTrue(mission.engine.uplink_of(Hello))
        mission.run_to(mission.mock.time + 40.0)
        self.assertFalse(
            mission.mock.status()["synced"], "the client claims to be synced"
        )
        self.assertTrue(
            any("did not answer hello" in msg for msg in mission.mock.logs("warning")),
            mission.mock.logs(),
        )

    def test_a_protocol_mismatch_tears_the_connection_down(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        mission.run_until(lambda m: bool(m.engine.uplink_of(Hello)), limit=60.0)
        mission.engine.mute = True
        mission.engine.send(
            [Sync(seq=1, t=0.0, campaign_time=0.0, protocol=99)]
        )
        mission.step(3)
        self.assertFalse(mission.mock.status()["synced"])
        self.assertTrue(
            any("protocol" in msg for msg in mission.mock.logs("error")),
            mission.mock.logs(),
        )


@requires_lua
class TestEventsAndObservers(unittest.TestCase):
    """Events are attribution-only, and the client filters them hard."""

    def setUp(self) -> None:
        self.mission = sortie_with_observer_over_the_target()
        self.addCleanup(self.mission.close)
        self.mission.run_until(
            lambda m: bool(m.mock.static_names()) and m.mock.status()["synced"],
            limit=200.0,
        )
        self.assertTrue(self.mission.mock.static_names(), "nothing was spawned")

    def test_an_event_about_an_engine_owned_object_is_forwarded(self):
        static = sorted(self.mission.mock.static_names())[0]
        self.mission.mock.fire_event(
            self.mission.mock.event_id("S_EVENT_DEAD"),
            initiator=self.mission.mock.static(static),
        )
        self.mission.step(3)
        events = self.mission.engine.uplink_of(Event)
        self.assertTrue(events, "no event reached the engine")
        self.assertEqual(events[-1].kind, "dead")
        self.assertEqual(
            events[-1].initiator,
            static.rsplit("_", 1)[0],
            "the object's numbering was not stripped back to the spawn id",
        )

    def test_an_event_about_somebody_elses_unit_is_dropped(self):
        before = len(self.mission.engine.uplink_of(Event))
        self.mission.mock.fire_event(
            self.mission.mock.event_id("S_EVENT_DEAD"),
            initiator=self.mission.mock.unit(self.mission.observer),
        )
        self.mission.step(3)
        self.assertEqual(
            len(self.mission.engine.uplink_of(Event)),
            before,
            "an event about a non-engine object was forwarded",
        )

    def test_a_dead_player_drops_straight_out_of_the_observer_report(self):
        self.mission.run_until(
            lambda m: bool(m.engine.uplink_of(ObserverReport)), limit=60.0
        )
        self.mission.mock.kill_unit(self.mission.observer)
        self.mission.mock.fire_event(
            self.mission.mock.event_id("S_EVENT_PILOT_DEAD")
        )
        self.mission.step(8)
        latest = self.mission.engine.uplink_of(ObserverReport)[-1]
        self.assertEqual(
            latest.observers, [], "a dead player is still reported as an observer"
        )

    def test_an_event_handler_that_is_handed_rubbish_does_not_die(self):
        """A DCS event handler that raises is removed for the whole mission."""
        self.mission.mock.fire_event(self.mission.mock.event_id("S_EVENT_DEAD"))
        self.mission.mock.fire_event(999999)
        self.mission.step(2)
        before = len(self.mission.engine.uplink_of(Event))
        static = sorted(self.mission.mock.static_names())[0]
        self.mission.mock.fire_event(
            self.mission.mock.event_id("S_EVENT_DEAD"),
            initiator=self.mission.mock.static(static),
        )
        self.mission.step(3)
        self.assertGreater(
            len(self.mission.engine.uplink_of(Event)),
            before,
            "the event handler stopped working after a malformed event",
        )


@requires_lua
class TestTheEventHotPathIsCheapBeforeItIsThorough(unittest.TestCase):
    """Every S_EVENT in the mission runs through this, on the sim thread.

    `object_name` costs up to three pcall'd round trips into the DCS object
    model per object, and a mission with AI ground combat alongside the slice
    fires `shot` and `hit` in the thousands per second -- nearly all of them
    about units the engine does not own. The ownership filter is correct; what
    matters is that it runs before the expensive part rather than after it.
    """

    def setUp(self) -> None:
        self.mission = Mission(observer_pos=NOWHERE)
        self.addCleanup(self.mission.close)
        self.mission.run_until(lambda m: m.mock.status()["synced"], limit=60.0)
        self.assertTrue(self.mission.mock.status()["synced"], "never synced")

    def _probe(self, name: str):
        """A DCS object that counts the group lookups made against it."""
        return self.mission.mock.lua.eval(
            """
            (function(name)
                local o = {group_lookups = 0}
                function o:getName() return name end
                function o:getGroup()
                    o.group_lookups = o.group_lookups + 1
                    return nil
                end
                return o
            end)(%r)
            """
            % name
        )

    def test_someone_elses_shot_is_dropped_without_resolving_its_group(self):
        shooter = self._probe("red_sa6_bassel_1")
        victim = self._probe("blue_farp_truck_3")
        self.mission.mock.fire_event(
            self.mission.mock.event_id("S_EVENT_SHOT"),
            initiator=shooter,
            target=victim,
        )
        self.mission.step(2)
        self.assertEqual(
            (int(shooter.group_lookups), int(victim.group_lookups)),
            (0, 0),
            "the client walked into the DCS object model for an event it "
            "discarded a line later",
        )
        self.assertEqual(
            self.mission.engine.uplink_of(Event), [], "the event was forwarded"
        )

    def test_an_owned_units_shot_is_still_forwarded_under_its_group_name(self):
        """The control: the cheap filter must not drop what the engine wants."""
        self.mission.engine.send(
            [
                Spawn(
                    seq=9100,
                    t=self.mission.mock.time,
                    ref=9100,
                    spawn_id="beef",
                    coalition="blue",
                    category="plane",
                    template="F-16C_strike_jdam",
                    position=(1000.0, 5000.0, 2000.0),
                    heading=0.0,
                    route=[],
                    tasking={},
                )
            ]
        )
        self.mission.step(3)
        self.assertIn("cmp_beef", self.mission.mock.group_names())
        self.mission.mock.fire_event(
            self.mission.mock.event_id("S_EVENT_SHOT"),
            initiator=self.mission.mock.unit("cmp_beef_1"),
        )
        self.mission.step(3)
        events = self.mission.engine.uplink_of(Event)
        self.assertTrue(events, "an event about an engine-owned unit was dropped")
        self.assertEqual(events[-1].initiator, "cmp_beef")


@requires_lua
class TestSceneryDoesNotPutANumberOnTheWire(unittest.TestCase):
    """`initiator` and `target` are strings, per docs/protocol.md.

    DCS scenery answers `getName` with its numeric object id, and an
    engine-owned jet clipping a building is an ordinary occurrence. The number
    survives JSON, lands in `Event.target` -- which nothing validates -- and
    the engine's attribution path tests it with a string prefix.
    """

    def setUp(self) -> None:
        self.mission = Mission(observer_pos=NOWHERE)
        self.addCleanup(self.mission.close)
        self.mission.run_until(lambda m: m.mock.status()["synced"], limit=60.0)
        self.mission.engine.send(
            [
                Spawn(
                    seq=9200,
                    t=self.mission.mock.time,
                    ref=9200,
                    spawn_id="beef",
                    coalition="blue",
                    category="plane",
                    template="F-16C_strike_jdam",
                    position=(1000.0, 5000.0, 2000.0),
                    heading=0.0,
                    route=[],
                    tasking={},
                )
            ]
        )
        self.mission.step(3)
        self.assertIn("cmp_beef", self.mission.mock.group_names())

    def test_a_hit_on_a_building_carries_no_name_rather_than_a_number(self):
        scenery = self.mission.mock.lua.eval(
            "(function() local o = {} function o:getName() return 140521 end "
            "return o end)()"
        )
        self.mission.mock.fire_event(
            self.mission.mock.event_id("S_EVENT_HIT"),
            initiator=self.mission.mock.unit("cmp_beef_1"),
            target=scenery,
        )
        self.mission.step(3)
        events = self.mission.engine.uplink_of(Event)
        self.assertTrue(events, "the hit never reached the engine")
        self.assertEqual(events[-1].initiator, "cmp_beef")
        self.assertIsNone(
            events[-1].target,
            "a scenery object id went out where the wire says string",
        )
        self.mission.assert_lua_was_clean(self)


@requires_lua
class TestTheHostMustBeAnAddress(unittest.TestCase):
    """`settimeout(0)` bounds the handshake, not the name resolution.

    LuaSocket's `connect` calls getaddrinfo synchronously before any socket
    timeout applies, so a `host` that does not resolve costs Windows a DNS
    round trip, then LLMNR, then NetBIOS -- one to three seconds of frozen sim
    per reconnect attempt, for as long as the engine is unreachable. `host` is
    documented as user-overridable, which makes it the last path by which this
    client can block the simulation thread.
    """

    def test_a_hostname_is_refused_instead_of_being_resolved(self):
        mission = Mission(
            observer_pos=NOWHERE, config={"host": "campaign-engine.invalid"}
        )
        self.addCleanup(mission.close)
        mission.step(10)
        self.assertEqual(
            mission.mock.sockets.opened,
            0,
            "the client handed a name to connect(), which resolves it on the "
            "simulation thread",
        )
        self.assertEqual(mission.engine.connections, 0)
        self.assertEqual(mission.mock.status()["phase"], "idle")
        complaints = [m for m in mission.mock.logs("error") if "IPv4" in m]
        self.assertEqual(
            len(complaints),
            1,
            "the refusal was not logged exactly once: %r" % (complaints,),
        )
        self.assertIn("campaign-engine.invalid", complaints[0])

    def test_localhost_connects_without_asking_a_resolver(self):
        """The one name worth honouring, because it needs no resolver.

        Refusing it would be friction bought with no safety: everyone writes
        localhost, and the answer is known here. What must not happen is the
        client handing the string itself to connect(), which would resolve it
        on the simulation thread like any other name.
        """
        mission = Mission(observer_pos=NOWHERE, config={"host": "localhost"})
        self.addCleanup(mission.close)
        mission.step(10)
        self.assertEqual(mission.engine.connections, 1, "localhost was refused")
        self.assertEqual(
            [m for m in mission.mock.logs("error") if "IPv4" in m],
            [],
            "localhost was treated as an unresolvable name",
        )
        self.assertEqual(
            mission.mock.sockets.connected_to,
            [("127.0.0.1", mission.engine.port)],
            "the client handed the name to connect() instead of the address",
        )


@requires_lua
class TestAnAttritedEntityComesBackAttrited(unittest.TestCase):
    """Re-instantiation must not hand the campaign back what it wrote off.

    A bubble cycle or a DCS restart re-issues every live spawn. If the client
    rebuilt each one from its template, a two-ship that lost a wingman would
    come back whole and the next snapshot would honestly report two aircraft
    -- and the campaign would quietly un-lose an airframe. The same applies to
    a half-flattened target, which would otherwise have to be destroyed twice.
    """

    def test_a_flight_and_a_target_come_back_with_what_survived(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        mission.run_until(
            lambda m: bool(
                m.campaign.packages
                and m.mock.group(group_name(m.package.spawn_id)) is not None
            ),
            limit=1400.0,
        )
        flight = mission.flight_group()
        self.assertEqual(
            len([u for u in mission.mock.unit_names() if u.startswith(flight)]), 2
        )

        mission.mock.kill_unit(
            sorted(u for u in mission.mock.unit_names() if u.startswith(flight))[-1]
        )
        for name in sorted(mission.mock.static_names())[:2]:
            mission.mock.kill_static(name)

        settled = mission.run_until(
            lambda m: m.campaign.tracker.units_alive(m.package.spawn_id) == 1
            and m.campaign.tracker.units_alive(m.depot.spawn_id) == 2,
            limit=mission.mock.time + 180.0,
        )
        self.assertTrue(settled, "the snapshot never carried the losses back")

        mission.engine.drop()
        mission.step(3)
        back = mission.run_until(
            lambda m: m.engine.connections >= 2 and m.mock.group(flight) is not None,
            limit=mission.mock.time + 200.0,
        )
        self.assertTrue(back, "the flight was never re-instantiated")
        mission.step(5)

        self.assertEqual(
            [u for u in mission.mock.unit_names() if u.startswith(flight)],
            [flight + "_1"],
            "the flight came back with a wingman the campaign had written off",
        )
        self.assertEqual(
            len(mission.mock.static_names()),
            2,
            "the destroyed half of the depot was stood back up",
        )
        self.assertEqual(
            len(mission.mock.group_data(flight)["units"]),
            1,
            "the group table itself still asks for two aircraft",
        )
        mission.assert_lua_was_clean(self)


@requires_lua
class TestARejectedSpawnIsNeverRetried(unittest.TestCase):
    """`Campaign.blocked`, which nothing else drives.

    A rejected spawn is a bad template: the entity cannot be instantiated and
    nothing about the next bubble sync will change that. Without the guard the
    engine re-offers it on every pulse -- an observer pulse every 5 s and a
    census every 30 s -- and each frame is a spawn attempt the client runs on
    the DCS simulation thread. That is a frame-rate problem in the sim, not
    merely wasted bytes.

    The only other rejection test refuses a *flight*, and an aborted package
    stops being a bubble candidate for unrelated reasons, so it never reaches
    the retry path. A refused target stays a candidate for as long as it is
    alive, which is what makes it the case that matters.
    """

    def test_a_target_the_client_refuses_is_offered_exactly_once(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        # The content mistake this guard exists for: a TEMPLATES key that does
        # not match the name the engine puts on the wire.
        mission.mock.client.TEMPLATES["fuel_depot_medium"] = None

        depot_id = mission.depot.spawn_id
        # Well short of TOT, so the depot is still standing and still a
        # candidate for every pulse in between.
        mission.run_to(900.0)

        offers = [
            f for f in mission.engine.downlink_of(Spawn) if f.spawn_id == depot_id
        ]
        self.assertEqual(
            len(offers),
            1,
            "the engine re-offered a spawn the client had already refused, "
            "%d times over 900 mission seconds" % len(offers),
        )
        refusals = [
            a
            for a in mission.engine.uplink_of(Ack)
            if a.ref == offers[0].ref and not a.ok
        ]
        self.assertTrue(refusals, "the client did not refuse the bad template")
        self.assertIn("unknown template", refusals[0].error or "")
        self.assertEqual(mission.campaign.blocked, {depot_id})
        self.assertFalse(
            mission.depot.destroyed, "the depot stopped being a candidate"
        )
        mission.assert_lua_was_clean(self)


@requires_lua
class TestAFlightLostEarlyIsClosedOutAtOnce(unittest.TestCase):
    """A package whose flight dies stops being open there and then.

    `_close_out_dead_packages` is what makes that true. Without it a two-ship
    shot down shortly after takeoff stays `is_open` for the rest of its
    scheduled sortie -- which blocks planning, because the slice frags one
    package at a time -- and keeps its airframes and munitions reserved for
    the whole of it, while the player is never told the flight is gone.
    """

    def test_the_package_closes_settles_and_is_replaced_before_its_rtb(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        mission.run_until(
            lambda m: bool(
                m.campaign.packages
                and m.mock.group(group_name(m.package.spawn_id)) is not None
            ),
            limit=1400.0,
        )
        package = mission.package
        self.assertTrue(package.is_open, "the flight was never airborne")

        mission.mock.kill_group(group_name(package.spawn_id))
        # Long enough for one census, which is the only thing that may record
        # the loss, and the pulse that follows it.
        mission.step(40)

        squadron = mission.campaign.inventories["blue"].squadron(SQUADRON)
        self.assertLess(
            mission.campaign.clock,
            package.t_rtb,
            "the flight outlived its scheduled sortie; nothing was proven",
        )
        self.assertEqual(package.state, DESTROYED)
        self.assertNotIn(
            package.reservation_id,
            squadron.open_reservations,
            "a flight that is gone still holds its airframes and bombs",
        )
        self.assertTrue(
            any("is lost" in text for text in mission.engine.messages()),
            "the player was never told the flight was lost: %r"
            % (mission.engine.messages(),),
        )
        replacements = [p for p in mission.campaign.packages.values() if p is not package]
        self.assertEqual(
            len(replacements),
            1,
            "no replacement was fragged; planning stayed blocked until t_rtb",
        )
        squadron.check_invariant()
        mission.assert_lua_was_clean(self)


@requires_lua
class TestACampaignThatCannotTaskStandsStill(unittest.TestCase):
    """A squadron out of ordnance must cost nothing to keep running.

    `_plan` allocates a package id and a spawn id before it knows whether the
    squadron can cover the package, and gives them back when it cannot. That
    restore is what keeps a stalled campaign from burning two ids per pulse --
    one per observer frame -- for the rest of the war: left running overnight
    the counters reach the hundreds of thousands, and the next real package
    gets a five-digit spawn id that no longer fits the `%04x` the rest of the
    engine is written around.
    """

    def test_ids_do_not_creep_while_the_squadron_is_dry(self):
        campaign = Campaign()
        squadron = campaign.inventories["blue"].squadron(SQUADRON)
        # Bombed out, with conservation intact: the rounds were expended, not
        # deleted. Nothing can be tasked from here.
        for munition, total in squadron.munitions_total.items():
            squadron.munitions_available[munition] = 0
            squadron.munitions_expended[munition] = total
        squadron.check_invariant()

        mission = Mission(
            observer_pos=(-3_000.0, CRUISE_ALTITUDE, 41_000.0), campaign=campaign
        )
        self.addCleanup(mission.close)
        mission.run_until(lambda m: m.mock.status()["synced"], limit=60.0)
        spawn_counter = campaign._spawn_counter
        package_counter = campaign._package_counter

        mission.run_to(900.0)

        self.assertEqual(campaign.packages, {}, "a package was fragged with no bombs")
        self.assertEqual(
            (campaign._spawn_counter, campaign._package_counter),
            (spawn_counter, package_counter),
            "the id counters crept on every pulse a stalled campaign failed "
            "to plan",
        )
        dry = [m for m in mission.engine.messages() if "Cannot task" in m]
        self.assertEqual(len(dry), 1, "the stall was announced %d times" % len(dry))
        squadron.check_invariant()
        mission.assert_lua_was_clean(self)


@requires_lua
class TestDeterminism(unittest.TestCase):
    """The same mission twice must produce the same war, byte for byte.

    Mission time here is whatever the mock's timer says, and the only
    randomness in the system is the campaign's seeded RNG. If a real socket's
    timing or a `pairs()` iteration order leaked into the result, a campaign
    would stop being reproducible from its log -- and a bug in it would stop
    being reproducible at all.
    """

    @staticmethod
    def one_war() -> str:
        import json as pyjson

        mission = sortie_with_observer_over_the_target()
        try:

            def resolve(m: Mission) -> None:
                package = next(iter(m.campaign.packages.values()), None)
                if package is None:
                    return
                if m.campaign.clock >= package.t_tot and m.mock.static_names():
                    for name in list(m.mock.static_names()):
                        m.mock.kill_static(name)

            mission.run_to(SORTIE_SECONDS, hook=resolve)
            mission.step(5)
            return pyjson.dumps(mission.campaign.to_dict(), sort_keys=True)
        finally:
            mission.close()

    def test_two_runs_of_the_same_mission_agree(self):
        first = self.one_war()
        self.assertEqual(first, self.one_war())
        self.assertIn("latakia_fuel_depot", first, "the run did nothing at all")


@requires_lua
class TestReloadingTheScript(unittest.TestCase):
    """A second DO SCRIPT FILE, or a mission restart inside one DCS session."""

    def test_a_second_load_replaces_the_first_client_rather_than_racing_it(self):
        mission = sortie_with_observer_over_the_target()
        self.addCleanup(mission.close)
        mission.run_until(lambda m: bool(m.mock.static_names()), limit=200.0)
        old = mission.mock.client
        self.assertTrue(mission.mock.static_names(), "nothing was live before reload")

        mission.mock.load_client()

        self.assertFalse(
            dict(old.status())["running"], "the previous client is still running"
        )
        self.assertEqual(
            mission.mock.static_names(),
            [],
            "the old client left groups behind; the new one spawns over them "
            "and getByName then resolves only one of the two",
        )
        self.assertEqual(
            int(mission.mock.env.pending_schedules()),
            1,
            "two ticks are now scheduled against one socket",
        )
        self.assertEqual(
            len(mission.mock.env.handlers), 1, "two event handlers are installed"
        )

        reconnected = mission.run_until(
            lambda m: m.engine.connections >= 2 and m.mock.status()["synced"],
            limit=mission.mock.time + 120.0,
        )
        self.assertTrue(reconnected, "the reloaded client never connected")
        mission.assert_lua_was_clean(self)


@requires_lua
class TestHelloCarriesTheMissionEpoch(unittest.TestCase):
    """`mission_start_epoch` is hand-rolled civil-date arithmetic.

    os.time() is unavailable in the sanitised environment and would be
    host-timezone dependent anyway, so the client computes days-from-civil
    itself. A campaign has to replay identically on two machines, which makes
    this worth pinning against an independent implementation.
    """

    def epoch_for(self, year: int, month: int, day: int, start: int) -> int:
        campaign = Campaign()
        engine = EngineHarness(campaign)
        self.addCleanup(engine.close)
        mock = DCSMock(port=engine.port, tick=TICK, autostart=False)
        self.addCleanup(mock.stop)
        mock.set_mission_date(year, month, day, start)
        mock.load_client()
        for _ in range(40):
            mock.advance(TICK)
            engine.pump()
            if engine.uplink_of(Hello):
                break
        hellos = engine.uplink_of(Hello)
        self.assertTrue(hellos, "the client never said hello")
        return hellos[0].mission_start_epoch

    def test_it_matches_an_independent_utc_calendar(self):
        import calendar

        for year, month, day, start in [
            (1970, 1, 1, 0),
            (1969, 7, 20, 72_000),  # before the epoch
            (1999, 12, 31, 86_399),
            (2000, 2, 29, 3_600),  # a century that IS a leap year
            (2016, 2, 29, 0),
            (2024, 9, 22, 43_200),
            (2100, 3, 1, 0),  # a century that is not
        ]:
            with self.subTest(date=(year, month, day)):
                self.assertEqual(
                    self.epoch_for(year, month, day, start),
                    calendar.timegm((year, month, day, 0, 0, 0)) + start,
                )


@requires_lua
class TestFramingAndBackpressure(unittest.TestCase):
    """Stream reassembly, the per-tick cap, and the two caps that exist so a
    wedged engine cannot wedge the simulation."""

    def setUp(self) -> None:
        self.mission = Mission(observer_pos=NOWHERE)
        self.addCleanup(self.mission.close)
        self.mission.run_until(lambda m: m.mock.status()["synced"], limit=60.0)
        self.assertTrue(self.mission.mock.status()["synced"], "never synced")

    def test_a_frame_split_across_two_tcp_writes_is_reassembled(self):
        raw = encode(Message(seq=6000, t=1.0, to="all", text="SPLIT", duration=9))
        self.mission.engine.outbox += raw[:12]
        self.mission.step(2)
        self.assertNotIn(
            "SPLIT",
            [m["text"] for m in self.mission.mock.messages()],
            "half a frame was acted on",
        )
        self.mission.engine.outbox += raw[12:]
        self.mission.step(2)
        shown = [m for m in self.mission.mock.messages() if m["text"] == "SPLIT"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["to"], "all")
        self.assertEqual(shown[0]["duration"], 9)

    def test_a_burst_is_spread_over_ticks_rather_than_stalling_one(self):
        """MAX_FRAMES_PER_TICK: a backlog is always better than a frame hitch."""
        burst = b"".join(
            encode(
                Despawn(
                    seq=5000 + i,
                    t=self.mission.mock.time,
                    ref=5000 + i,
                    spawn_id="zz%02d" % i,
                )
            )
            for i in range(40)
        )
        self.mission.engine.outbox += burst

        def acked() -> int:
            return len([a for a in self.mission.engine.uplink_of(Ack) if a.ref >= 5000])

        self.mission.step(2)
        self.assertEqual(acked(), 16, "the client did not stop at its per-tick cap")
        self.mission.step(1)
        self.assertEqual(acked(), 32)
        self.mission.step(4)
        self.assertEqual(acked(), 40, "the backlog never drained")
        self.assertEqual(self.mission.mock.status()["queued_frames"], 0)
        self.mission.assert_lua_was_clean(self)

    def test_an_endless_frame_with_no_newline_drops_the_connection(self):
        self.mission.engine.outbox += b'{"type":"message","junk":"' + b"x" * 90_000
        for _ in range(15):
            self.mission.step(1)
            if self.mission.mock.status()["phase"] != "open":
                break
        self.assertNotEqual(
            self.mission.mock.status()["phase"],
            "open",
            "the client kept buffering a frame that will never end",
        )
        self.assertTrue(
            any("MAX_FRAME_BYTES" in msg for msg in self.mission.mock.logs("warning")),
            self.mission.mock.logs(),
        )

    def test_an_engine_that_stops_reading_is_dropped_before_memory_runs_out(self):
        mission = Mission(observer_pos=NOWHERE, config={"outbox_max": 1024})
        self.addCleanup(mission.close)
        mission.run_until(lambda m: m.mock.status()["synced"], limit=60.0)
        mission.mock.sockets.send_limit = 0  # a socket that accepts nothing

        for _ in range(300):
            mission.step(1)
            if mission.mock.status()["phase"] != "open":
                break
        self.assertNotEqual(mission.mock.status()["phase"], "open")
        self.assertTrue(
            any("outbox full" in msg for msg in mission.mock.logs("error")),
            mission.mock.logs(),
        )


@requires_lua
class TestSpawnShapes(unittest.TestCase):
    """What the client actually builds for the shapes the engine sends."""

    def setUp(self) -> None:
        self.mission = Mission(observer_pos=NOWHERE)
        self.addCleanup(self.mission.close)
        self.mission.run_until(lambda m: m.mock.status()["synced"], limit=60.0)
        self.ref = 7000

    def inject(self, frame):
        self.mission.engine.send([frame])
        self.mission.step(3)
        acks = [a for a in self.mission.engine.uplink_of(Ack) if a.ref == frame.ref]
        self.assertTrue(acks, f"no ack for ref {frame.ref}")
        return acks[-1]

    def spawn(self, **overrides):
        self.ref += 1
        fields = dict(
            seq=self.ref,
            t=self.mission.mock.time,
            ref=self.ref,
            spawn_id="s%03d" % self.ref,
            coalition="blue",
            category="plane",
            template="F-16C_cap",
            position=(1000.0, 5000.0, 2000.0),
            heading=0.5,
            route=[],
            tasking={},
        )
        fields.update(overrides)
        return Spawn(**fields)

    def test_a_spawn_with_no_route_orbits_its_spawn_point(self):
        frame = self.spawn()
        self.assertTrue(self.inject(frame).ok)
        data = self.mission.mock.group_data(group_name(frame.spawn_id))
        points = data["route"]["points"]
        self.assertEqual(len(points), 1)
        # Protocol vec3 is (x, altitude, z); a mission group table is (x, z)
        # plus a separate alt. Swapping these puts the flight in the sea.
        self.assertEqual(points[0]["x"], 1000.0)
        self.assertEqual(points[0]["y"], 2000.0)
        self.assertEqual(points[0]["alt"], 5000.0)
        self.assertEqual(data["units"][0]["alt"], 5000.0)
        self.assertEqual(points[0]["task"]["params"]["tasks"], {})

    def test_the_units_of_a_flight_are_separated_so_they_do_not_collide(self):
        frame = self.spawn()
        self.assertTrue(self.inject(frame).ok)
        units = self.mission.mock.group_data(group_name(frame.spawn_id))["units"]
        self.assertEqual(len(units), 2)
        self.assertNotEqual(
            (units[0]["x"], units[0]["y"]),
            (units[1]["x"], units[1]["y"]),
            "both aircraft spawned on one point: a mid-air on frame one",
        )
        self.assertEqual(
            [u["name"] for u in units],
            [f"cmp_{frame.spawn_id}_1", f"cmp_{frame.spawn_id}_2"],
        )

    def test_a_multi_object_target_is_spread_out_rather_than_stacked(self):
        frame = self.spawn(
            spawn_id="dep1",
            coalition="red",
            category="structure",
            template="fuel_depot_medium",
        )
        self.assertTrue(self.inject(frame).ok)
        names = [n for n in self.mission.mock.static_names() if n.startswith("cmp_dep1")]
        self.assertEqual(len(names), 4)
        points = {self.mission.mock.static_point(n)[::2] for n in names}
        self.assertEqual(len(points), 4, "the four objects share an aim point")

    def test_half_a_target_is_never_left_standing_when_one_object_fails(self):
        """DCS fails a static silently; a half-built target is a phantom loss.

        Four objects issued, three built, and the engine's next census reads
        three of four units -- a loss nobody caused, charged to the campaign.
        """
        self.mission.mock.fail_static_named("cmp_dep2_3")
        frame = self.spawn(
            spawn_id="dep2",
            coalition="red",
            category="structure",
            template="fuel_depot_medium",
        )
        ack = self.inject(frame)
        self.assertFalse(ack.ok, "the client acked a half-built target")
        self.assertIn("created nothing", ack.error or "")
        self.assertEqual(
            [n for n in self.mission.mock.static_names() if n.startswith("cmp_dep2")],
            [],
            "the objects that did build were left standing in the mission",
        )
        self.assertEqual(self.mission.mock.status()["live_spawns"], 0)
        self.mission.mock.fail_static_named(None)

    def test_a_message_to_an_unknown_recipient_is_logged_not_shown(self):
        self.mission.engine.send(
            [Message(seq=6001, t=1.0, to="purple", text="nobody", duration=1)]
        )
        self.mission.step(2)
        self.assertNotIn(
            "nobody", [m["text"] for m in self.mission.mock.messages()]
        )
        self.assertTrue(
            any(
                "unknown recipient" in msg
                for msg in self.mission.mock.logs("warning")
            ),
            self.mission.mock.logs(),
        )


@requires_lua
class TestJsonStrings(unittest.TestCase):
    """Names are what the engine matches entities on, so they must survive."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mock = DCSMock(port=1, autostart=False)
        cls.json = cls.mock.json

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mock.stop()

    def test_every_string_dcs_can_hand_us_round_trips_through_python(self):
        import json as pyjson

        cases = [
            "plain",
            'quote"inside',
            "back\\slash",
            "new\nline",
            "tab\there",
            "Zhukov Ж and 中文",
            "control\x01\x1f",
            "emoji \U0001f525",
            "nul\x00byte",
            "slash/and\u007f",
        ]
        for value in cases:
            with self.subTest(text=repr(value)):
                text = self.json.encode(self.mock.lua.table_from({"name": value}))
                self.assertIsInstance(text, str, "json.lua refused the string")
                self.assertEqual(pyjson.loads(text)["name"], value)
                self.assertEqual(
                    self.json.decode(pyjson.dumps({"name": value}))["name"], value
                )

    def test_a_surrogate_pair_escape_decodes_to_one_codepoint(self):
        decoded = self.json.decode('{"n":"\\ud83d\\udd25"}')
        self.assertEqual(decoded["n"], "\U0001f525")

    def test_encoding_refuses_a_number_json_cannot_carry(self):
        inf = float("inf")
        for label, value in [("nan", inf - inf), ("inf", inf), ("-inf", -inf)]:
            with self.subTest(number=label):
                result = self.json.encode(self.mock.lua.table_from({"v": value}))
                self.assertIsInstance(
                    result, tuple, f"{label} encoded as {result!r}"
                )
                self.assertIsNone(result[0])
                self.assertIn("json.encode", result[1])


if __name__ == "__main__":
    unittest.main()
