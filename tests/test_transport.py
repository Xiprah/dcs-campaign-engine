"""Transport tests: campaign/server.py over a real loopback socket.

The engine here is a stub that only records what it was asked. The real
campaign is written by someone else and is deliberately not imported: the
transport's contract is campaign.api.CampaignEngine and nothing more.

Every test binds port 0 and asks the OS which port it got. Hardcoding 7777
would make the suite fail whenever a real engine is running.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import socket
import unittest

from campaign.api import CampaignEngine
from campaign.protocol import (
    PROTOCOL_VERSION,
    Ack,
    Despawn,
    Downlink,
    Event,
    FrameBuffer,
    GroupSnapshot,
    Hello,
    Message,
    Observer,
    ObserverReport,
    ProtocolError,
    Spawn,
    StateReport,
    Sync,
    Uplink,
    Waypoint,
    decode_downlink,
    encode,
)
from campaign.server import CampaignServer

TIMEOUT = 5.0


def setUpModule() -> None:
    # The server logs handler crashes at ERROR on purpose; tests that care
    # assert on them with assertLogs, the rest should stay quiet.
    logging.getLogger("campaign.server").setLevel(logging.CRITICAL)


SPAWN_TEMPLATE = Spawn(
    seq=0,
    t=0.0,
    ref=0,
    spawn_id="a91f",
    coalition="blue",
    category="plane",
    template="F-16C_strike_jdam",
    position=(40000.0, 4500.0, -90000.0),
    heading=1.57,
    route=[Waypoint(pos=(41000.0, 6000.0, -92000.0), alt=6000.0, speed=240.0)],
    tasking={"kind": "strike", "target": "cmp_7c02", "tot": 1230.0},
)


class StubEngine:
    """Records calls, returns whatever the test told it to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Uplink | float | None]] = []
        self.disconnects = 0
        self.ticks = 0
        self.raise_in: set[str] = set()
        #: Handlers that raise ProtocolError rather than RuntimeError. The
        #: transport has to tell the two apart: one closes the connection, the
        #: other must not.
        self.protocol_error_in: set[str] = set()
        self.live_spawns: list[Spawn] = []
        self.tick_frames: list[Downlink] = []
        #: When set, returned by on_state instead of the usual Despawn.
        self.state_frames: list[Downlink] | None = None
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _guard(self, name: str) -> None:
        if name in self.protocol_error_in:
            raise ProtocolError(f"stub engine refuses {name}")
        if name in self.raise_in:
            raise RuntimeError(f"stub engine exploded in {name}")

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]

    def frames_of(self, kind: str) -> list[Uplink]:
        return [msg for name, msg in self.calls if name == kind]

    def on_hello(self, msg: Hello) -> list[Downlink]:
        self.calls.append(("hello", msg))
        self._guard("on_hello")
        out: list[Downlink] = [
            Sync(seq=self._next(), t=msg.t, campaign_time=417600.0, state_period=10.0)
        ]
        for spawn in self.live_spawns:
            seq = self._next()
            out.append(dataclasses.replace(spawn, seq=seq, ref=seq, t=msg.t))
        return out

    def on_observer(self, msg: ObserverReport) -> list[Downlink]:
        self.calls.append(("observer", msg))
        self._guard("on_observer")
        return []

    def on_event(self, msg: Event) -> list[Downlink]:
        self.calls.append(("event", msg))
        self._guard("on_event")
        return []

    def on_state(self, msg: StateReport) -> list[Downlink]:
        self.calls.append(("state", msg))
        self._guard("on_state")
        if self.state_frames is not None:
            frames, self.state_frames = self.state_frames, None
            return frames
        return [Despawn(seq=self._next(), t=msg.t, ref=self._seq, spawn_id="a91f", reason="dead")]

    def on_ack(self, msg: Ack) -> list[Downlink]:
        self.calls.append(("ack", msg))
        self._guard("on_ack")
        return []

    def tick(self, now: float) -> list[Downlink]:
        self.ticks += 1
        self.calls.append(("tick", now))
        self._guard("tick")
        frames, self.tick_frames = self.tick_frames, []
        return frames

    def on_disconnect(self) -> None:
        self.disconnects += 1
        self.calls.append(("disconnect", None))
        self._guard("on_disconnect")


class Client:
    """The mission-client half, just enough of it to drive the server."""

    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._buffer = FrameBuffer()
        self._pending: list[bytes] = []

    async def connect(self, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)

    async def send(self, *frames: Uplink) -> None:
        assert self.writer is not None
        self.writer.write(b"".join(encode(f) for f in frames))
        await self.writer.drain()

    async def send_raw(self, blob: bytes) -> None:
        assert self.writer is not None
        self.writer.write(blob)
        await self.writer.drain()

    async def recv(self) -> Downlink:
        assert self.reader is not None
        while not self._pending:
            chunk = await asyncio.wait_for(self.reader.read(4096), TIMEOUT)
            if not chunk:
                raise EOFError("server closed the connection")
            self._pending.extend(self._buffer.feed(chunk))
        return decode_downlink(self._pending.pop(0))

    async def recv_many(self, count: int) -> list[Downlink]:
        return [await self.recv() for _ in range(count)]

    async def wait_closed(self) -> None:
        assert self.reader is not None
        while True:
            chunk = await asyncio.wait_for(self.reader.read(4096), TIMEOUT)
            if not chunk:
                return

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.writer = None


class TransportTestCase(unittest.IsolatedAsyncioTestCase):
    tick_period = 3600.0  # never fires unless a test asks for it

    async def asyncSetUp(self) -> None:
        self.engine = StubEngine()
        self.server = CampaignServer(
            self.engine, host="127.0.0.1", port=0, tick_period=self.tick_period
        )
        await self.server.start()
        self.assertNotEqual(self.server.port, 0)
        self.clients: list[Client] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        await self.server.close()

    async def client(self) -> Client:
        client = Client()
        await client.connect(self.server.port)
        self.clients.append(client)
        return client

    async def hello(self, client: Client, t: float = 0.0) -> Sync:
        await client.send(
            Hello(seq=1, t=t, protocol=PROTOCOL_VERSION, theater="Syria", dcs_version="2.9.29")
        )
        frame = await client.recv()
        self.assertIsInstance(frame, Sync)
        return frame

    async def wait_for(self, predicate, what: str) -> None:
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.01)


class HelloTests(TransportTestCase):
    def test_stub_satisfies_the_engine_protocol(self) -> None:
        self.assertIsInstance(StubEngine(), CampaignEngine)

    async def test_hello_is_answered_with_sync(self) -> None:
        client = await self.client()
        sync = await self.hello(client, t=12.5)
        self.assertEqual(sync.protocol, PROTOCOL_VERSION)
        self.assertEqual(sync.campaign_time, 417600.0)
        self.assertEqual(sync.state_period, 10.0)
        self.assertEqual(sync.t, 12.5)
        hello = self.engine.frames_of("hello")[0]
        self.assertEqual(hello.theater, "Syria")
        self.assertEqual(hello.protocol, PROTOCOL_VERSION)

    async def test_hello_reissues_live_spawns_after_sync(self) -> None:
        self.engine.live_spawns = [
            SPAWN_TEMPLATE,
            dataclasses.replace(SPAWN_TEMPLATE, spawn_id="7c02", category="ground"),
        ]
        client = await self.client()
        await client.send(Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria"))
        frames = await client.recv_many(3)
        self.assertIsInstance(frames[0], Sync)
        self.assertEqual([f.spawn_id for f in frames[1:]], ["a91f", "7c02"])
        self.assertEqual(frames[1].route, SPAWN_TEMPLATE.route)
        self.assertEqual(frames[1].tasking, SPAWN_TEMPLATE.tasking)
        # A ref the client can ack against, distinct per frame.
        self.assertEqual(len({f.ref for f in frames[1:]}), 2)


class PreHelloTests(TransportTestCase):
    """Nothing goes out on a socket that has not said hello yet.

    An accepted socket is not yet a client. The mission client starts a
    non-blocking connect on one tick and sends `hello` on the next, so there
    is always a window where the connection exists and the engine has not been
    told. The engine restarts its downlink stream at every hello -- seq back to
    1, refs reissued -- so a tick landing in that window writes frames from the
    stream that is about to be discarded: two frames with the same seq and ref
    on one connection, and a `spawn` the hello re-issues under a second ref.
    The client is obliged to refuse that as a duplicate spawn_id, and the
    engine then blocks the entity for the rest of the campaign -- a target it
    can never instantiate again and never stops fragging packages at.
    """

    tick_period = 0.01

    async def test_a_tick_before_hello_is_not_written_to_the_socket(self) -> None:
        client = await self.client()
        self.engine.tick_frames = [
            Message(seq=99, t=0.0, to="blue", text="from the stream before hello")
        ]
        await self.wait_for(
            lambda: not self.engine.tick_frames, "a tick to consume the frames"
        )
        await client.send(
            Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        first = await client.recv()
        self.assertIsInstance(
            first, Sync, "a frame reached the client before its sync"
        )

    async def test_a_spawn_before_hello_does_not_come_back_as_a_duplicate(self) -> None:
        client = await self.client()
        # The campaign's own sequence when a tick lands in the window: it
        # issues the spawn, records the id as live, and the hello that follows
        # re-issues that same id under a fresh ref.
        self.engine.live_spawns = [SPAWN_TEMPLATE]
        self.engine.tick_frames = [dataclasses.replace(SPAWN_TEMPLATE, seq=41, ref=41)]
        await self.wait_for(
            lambda: not self.engine.tick_frames, "a tick to consume the spawn"
        )
        await client.send(
            Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        frames = await client.recv_many(2)
        self.assertIsInstance(
            frames[0],
            Sync,
            "a spawn was written before the hello that re-issues it; the "
            "client must refuse the second one as a duplicate spawn_id",
        )
        spawns = [f for f in frames if isinstance(f, Spawn)]
        self.assertEqual(
            [f.spawn_id for f in spawns], ["a91f"], "the spawn_id arrived twice"
        )

    async def test_a_handler_reply_before_hello_is_dropped_too(self) -> None:
        client = await self.client()
        # Out of order by the spec, but the engine answers anything it is
        # handed, and that answer belongs to the stream hello is about to
        # discard just as much as a tick's does.
        await client.send(StateReport(seq=1, t=30.0, groups=[]))
        await self.wait_for(lambda: self.engine.frames_of("state"), "the state frame")
        await client.send(
            Hello(seq=2, t=31.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        first = await client.recv()
        self.assertIsInstance(
            first, Sync, "a reply reached the client before its sync"
        )


class DispatchTests(TransportTestCase):
    async def test_every_uplink_type_reaches_its_handler(self) -> None:
        client = await self.client()
        await self.hello(client)
        await client.send(
            ObserverReport(
                seq=2,
                t=5.0,
                observers=[Observer(id="player:Jakem", pos=(1.0, 2.0, 3.0), speed=220.0)],
            ),
            Event(seq=3, t=6.0, kind="kill", initiator="cmp_a91f", target="cmp_7c02"),
            Ack(seq=4, t=7.0, ref=91, ok=False, error="unknown template"),
        )
        await self.wait_for(
            lambda: {"observer", "event", "ack"} <= set(self.engine.kinds()),
            "observer/event/ack dispatch",
        )
        observer = self.engine.frames_of("observer")[0]
        self.assertEqual(observer.observers[0].id, "player:Jakem")
        self.assertEqual(observer.observers[0].pos, (1.0, 2.0, 3.0))
        event = self.engine.frames_of("event")[0]
        self.assertEqual((event.kind, event.initiator, event.target), ("kill", "cmp_a91f", "cmp_7c02"))
        ack = self.engine.frames_of("ack")[0]
        self.assertFalse(ack.ok)
        self.assertEqual(ack.error, "unknown template")

    async def test_state_dispatch_returns_frames_to_the_client(self) -> None:
        client = await self.client()
        await self.hello(client)
        await client.send(
            StateReport(
                seq=2,
                t=30.0,
                groups=[
                    GroupSnapshot(
                        spawn_id="a91f", alive=True, units=1, units_initial=2, pos=(1.0, 2.0, 3.0)
                    )
                ],
            )
        )
        reply = await client.recv()
        self.assertIsInstance(reply, Despawn)
        self.assertEqual(reply.spawn_id, "a91f")
        state = self.engine.frames_of("state")[0]
        self.assertEqual(state.groups[0].units, 1)
        self.assertEqual(state.groups[0].units_initial, 2)

    async def test_several_frames_in_one_write_are_dispatched_in_order(self) -> None:
        client = await self.client()
        blob = b"".join(
            encode(Event(seq=n, t=float(n), kind="shot", initiator=f"cmp_{n:04x}"))
            for n in range(1, 21)
        )
        await client.send_raw(blob)
        await self.wait_for(lambda: len(self.engine.frames_of("event")) == 20, "20 events")
        self.assertEqual([f.seq for f in self.engine.frames_of("event")], list(range(1, 21)))

    async def test_a_frame_split_across_writes_is_reassembled(self) -> None:
        client = await self.client()
        blob = encode(Event(seq=7, t=1.0, kind="pilot_dead", initiator="cmp_a91f"))
        for index in range(len(blob)):
            await client.send_raw(blob[index : index + 1])
        await self.wait_for(lambda: self.engine.frames_of("event"), "the reassembled event")
        self.assertEqual(self.engine.frames_of("event")[0].kind, "pilot_dead")

    async def test_blank_lines_are_ignored_not_fatal(self) -> None:
        client = await self.client()
        await client.send_raw(b"\n\n")
        await client.send(Event(seq=1, t=1.0, kind="shot"))
        await self.wait_for(lambda: self.engine.frames_of("event"), "the event after blank lines")


class ReconnectTests(TransportTestCase):
    async def test_disconnect_then_reconnect(self) -> None:
        first = await self.client()
        await self.hello(first)
        await first.close()
        await self.wait_for(lambda: self.engine.disconnects == 1, "on_disconnect")
        self.assertFalse(self.server.connected)

        self.engine.live_spawns = [SPAWN_TEMPLATE]
        second = await self.client()
        await second.send(Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria"))
        frames = await second.recv_many(2)
        self.assertIsInstance(frames[0], Sync)
        self.assertIsInstance(frames[1], Spawn)
        self.assertEqual(len(self.engine.frames_of("hello")), 2)
        self.assertEqual(self.engine.disconnects, 1)

    async def test_disconnect_is_reported_once_per_connection(self) -> None:
        for _ in range(3):
            client = await self.client()
            await self.hello(client)
            await client.close()
            await asyncio.sleep(0)
        await self.wait_for(lambda: self.engine.disconnects == 3, "three disconnects")
        self.assertEqual(self.engine.disconnects, 3)

    async def test_disconnect_precedes_the_next_hello(self) -> None:
        first = await self.client()
        await self.hello(first)
        await first.close()
        second = await self.client()
        await self.hello(second)
        order = [name for name in self.engine.kinds() if name in ("hello", "disconnect")]
        self.assertEqual(order, ["hello", "disconnect", "hello"])

    async def test_a_second_client_displaces_the_first(self) -> None:
        first = await self.client()
        await self.hello(first)
        second = await self.client()
        await self.hello(second)
        await self.wait_for(lambda: self.engine.disconnects == 1, "the displaced client")
        await first.wait_closed()
        # The newcomer is the live one and still works.
        await second.send(Event(seq=2, t=1.0, kind="shot"))
        await self.wait_for(lambda: self.engine.frames_of("event"), "an event from the new client")

    async def test_protocol_error_closes_only_the_connection(self) -> None:
        client = await self.client()
        await self.hello(client)
        await client.send_raw(b"{this is not json}\n")
        await client.wait_closed()
        await self.wait_for(lambda: self.engine.disconnects == 1, "on_disconnect")

        survivor = await self.client()
        sync = await self.hello(survivor)
        self.assertIsInstance(sync, Sync)

    async def test_unknown_frame_type_closes_the_connection(self) -> None:
        client = await self.client()
        await client.send_raw(b'{"type":"launch_nukes","seq":1,"t":0.0}\n')
        await client.wait_closed()
        survivor = await self.client()
        self.assertIsInstance(await self.hello(survivor), Sync)


class HandlerFailureTests(TransportTestCase):
    async def test_a_raising_handler_leaves_the_server_alive(self) -> None:
        client = await self.client()
        await self.hello(client)
        self.engine.raise_in = {"on_event"}
        with self.assertLogs("campaign.server", level="ERROR") as logs:
            await client.send(Event(seq=2, t=1.0, kind="kill", initiator="cmp_a91f"))
            await self.wait_for(lambda: self.engine.frames_of("event"), "the doomed event")
            await asyncio.sleep(0.05)
        self.assertTrue(any("on_event" in line for line in logs.output))

        # Same connection, still serving.
        self.engine.raise_in = set()
        await client.send(
            StateReport(seq=3, t=30.0, groups=[
                GroupSnapshot(spawn_id="a91f", alive=False, units=0, units_initial=2)
            ])
        )
        reply = await client.recv()
        self.assertIsInstance(reply, Despawn)

        # And a fresh connection still works.
        fresh = await self.client()
        self.assertIsInstance(await self.hello(fresh), Sync)

    async def test_a_raising_hello_does_not_kill_the_server(self) -> None:
        self.engine.raise_in = {"on_hello"}
        client = await self.client()
        with self.assertLogs("campaign.server", level="ERROR"):
            await client.send(Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria"))
            await self.wait_for(lambda: self.engine.frames_of("hello"), "the doomed hello")
            await asyncio.sleep(0.05)
        self.engine.raise_in = set()
        fresh = await self.client()
        self.assertIsInstance(await self.hello(fresh), Sync)

    async def test_a_raising_on_disconnect_does_not_kill_the_server(self) -> None:
        client = await self.client()
        await self.hello(client)
        self.engine.raise_in = {"on_disconnect"}
        with self.assertLogs("campaign.server", level="ERROR"):
            await client.close()
            await self.wait_for(lambda: self.engine.disconnects == 1, "on_disconnect")
            await asyncio.sleep(0.05)
        self.engine.raise_in = set()
        fresh = await self.client()
        self.assertIsInstance(await self.hello(fresh), Sync)


class TickTests(TransportTestCase):
    tick_period = 0.01

    async def test_tick_runs_on_its_own_cadence(self) -> None:
        await self.wait_for(lambda: self.engine.ticks >= 3, "three ticks")
        self.assertGreaterEqual(self.engine.ticks, 3)

    async def test_tick_output_reaches_the_client(self) -> None:
        client = await self.client()
        await self.hello(client)
        self.engine.tick_frames = [
            Message(seq=99, t=1.0, to="blue", text="Package COWBOY off target, RTB.")
        ]
        frame = await client.recv()
        self.assertIsInstance(frame, Message)
        self.assertEqual(frame.text, "Package COWBOY off target, RTB.")

    async def test_tick_keeps_running_with_no_client(self) -> None:
        self.engine.tick_frames = [Message(seq=1, t=0.0, to="blue", text="into the void")]
        before = self.engine.ticks
        await self.wait_for(lambda: self.engine.ticks > before + 3, "ticks without a client")
        client = await self.client()
        self.assertIsInstance(await self.hello(client), Sync)

    async def test_a_raising_tick_does_not_stop_the_tick_task(self) -> None:
        self.engine.raise_in = {"tick"}
        with self.assertLogs("campaign.server", level="ERROR"):
            before = self.engine.ticks
            await self.wait_for(lambda: self.engine.ticks > before + 2, "ticks after a crash")
        self.engine.raise_in = set()
        after = self.engine.ticks
        await self.wait_for(lambda: self.engine.ticks > after, "ticks resume")


class ProtocolMismatchTests(TransportTestCase):
    """docs/protocol.md: the engine closes the connection on a mismatch.

    The engine says so by raising ProtocolError out of `on_hello`. If the
    transport logs that like any other handler crash, the refused client keeps
    its socket and keeps driving the campaign it was just declared incompatible
    with -- which is worse than either accepting it or hanging up on it.
    """

    async def test_a_handler_protocol_error_closes_the_connection(self) -> None:
        self.engine.protocol_error_in = {"on_hello"}
        client = await self.client()
        await client.send(Hello(seq=1, t=0.0, protocol=99, theater="Syria"))
        await client.wait_closed()
        await self.wait_for(lambda: self.engine.disconnects == 1, "on_disconnect")
        self.assertFalse(self.server.connected)

    async def test_a_refused_client_cannot_keep_driving_the_campaign(self) -> None:
        self.engine.protocol_error_in = {"on_hello"}
        client = await self.client()
        await client.send(
            Hello(seq=1, t=0.0, protocol=99, theater="Syria"),
            ObserverReport(
                seq=2, t=10.0, observers=[Observer(id="player:X", pos=(0.0, 0.0, 0.0))]
            ),
        )
        await client.wait_closed()
        self.assertEqual(self.engine.frames_of("observer"), [])

    async def test_a_matching_client_is_still_served_afterwards(self) -> None:
        self.engine.protocol_error_in = {"on_hello"}
        doomed = await self.client()
        await doomed.send(Hello(seq=1, t=0.0, protocol=99, theater="Syria"))
        await doomed.wait_closed()
        self.engine.protocol_error_in = set()
        survivor = await self.client()
        self.assertIsInstance(await self.hello(survivor), Sync)


class EncodeFailureTests(TransportTestCase):
    """A frame the engine cannot encode is a bug, not a reason to stop the war.

    `encode` raises TypeError, not ProtocolError, for a non-dataclass or a value
    json cannot serialise, and the send sites are outside the try/except that
    guards the handler and tick calls themselves.
    """

    tick_period = 0.01

    async def test_an_unencodable_tick_frame_does_not_stop_the_clock(self) -> None:
        client = await self.client()
        await self.hello(client)
        self.engine.tick_frames = [None]  # type: ignore[list-item]
        with self.assertLogs("campaign.server", level="ERROR"):
            before = self.engine.ticks
            await self.wait_for(
                lambda: self.engine.ticks > before + 3, "ticks after an unsendable frame"
            )

    async def test_shutdown_still_runs_after_an_unencodable_tick_frame(self) -> None:
        client = await self.client()
        await self.hello(client)
        self.engine.tick_frames = [None]  # type: ignore[list-item]
        with self.assertLogs("campaign.server", level="ERROR"):
            await self.wait_for(lambda: self.engine.ticks > 2, "a tick past the bad frame")
        # close() awaits the tick task; a stored exception coming back out of
        # here would propagate through __main__'s finally and skip the save.
        await self.server.close()
        self.assertEqual(self.engine.disconnects, 1)

    async def test_the_rest_of_a_batch_survives_an_unencodable_frame(self) -> None:
        client = await self.client()
        await self.hello(client)
        self.engine.tick_frames = [
            None,  # type: ignore[list-item]
            Message(seq=99, t=0.0, to="blue", text="the rest of the batch"),
        ]
        with self.assertLogs("campaign.server", level="ERROR"):
            frame = await client.recv()
        self.assertIsInstance(frame, Message)
        self.assertEqual(frame.text, "the rest of the batch")


class DispatchEncodeFailureTests(TransportTestCase):
    async def test_an_unencodable_handler_frame_does_not_kill_the_connection(self) -> None:
        client = await self.client()
        await self.hello(client)
        self.engine.state_frames = [None]  # type: ignore[list-item]
        with self.assertLogs("campaign.server", level="ERROR"):
            await client.send(StateReport(seq=2, t=30.0, groups=[]))
            await self.wait_for(lambda: self.engine.frames_of("state"), "the doomed reply")
            await asyncio.sleep(0.05)
        await client.send(Event(seq=3, t=31.0, kind="shot"))
        await self.wait_for(
            lambda: self.engine.frames_of("event"), "the connection still serving"
        )


class WedgedClientTests(TransportTestCase):
    """A DCS that stopped reading must not lock out the one replacing it.

    `transport.close()` defers `connection_lost` until the write buffer drains,
    which a client that has stopped reading never lets happen. Everything that
    waits on `_Connection.closed` then waits forever: the displacing client's
    handler, and `close()` itself.
    """

    tick_period = 0.01

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.flooding = False

    def _backed_up(self) -> bool:
        conn = self.server._conn
        return conn is not None and conn.writer.transport.get_write_buffer_size() > 0

    async def _wedge(self) -> None:
        """Attach a client that never reads, and stuff the engine's write buffer.

        The flood has to keep coming: one large write is swallowed whole by the
        loopback stack, and it is an *outstanding* write that makes a graceful
        close defer forever.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        self.addCleanup(sock.close)
        sock.connect(("127.0.0.1", self.server.port))
        sock.sendall(
            encode(Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria"))
        )
        await self.wait_for(lambda: self.server.connected, "the deaf client")
        batch = [
            Message(seq=n, t=0.0, to="blue", text="x" * 40_000) for n in range(1, 21)
        ]
        self.engine.tick = (  # type: ignore[method-assign]
            lambda now: list(batch) if self.flooding else []
        )
        self.flooding = True
        await self.wait_for(self._backed_up, "the deaf client's socket to back up")

    async def test_a_deaf_client_is_displaced_by_the_next_one(self) -> None:
        await self._wedge()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.client()
        await self.wait_for(
            lambda: self.engine.disconnects == 1, "the deaf client to be let go"
        )
        elapsed = loop.time() - started
        self.flooding = False
        # Displacement is bounded by a timeout so a wedged predecessor can never
        # lock the sim out entirely; aborting makes it immediate. Anything near
        # that bound means the graceful close is back.
        self.assertLess(elapsed, 2.0, "displacement waited on a drain")

    async def test_close_completes_with_a_deaf_client_attached(self) -> None:
        await self._wedge()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.server.close()
        elapsed = loop.time() - started
        self.flooding = False
        self.assertLess(elapsed, 2.0, "close waited on a drain")
        self.assertFalse(self.server.connected)


class ServeForeverTests(TransportTestCase):
    """Cancelling `serve_forever` must tear down, not deadlock.

    `asyncio.Server.serve_forever()` answers cancellation by awaiting
    `wait_closed()`, which since 3.12 waits for every connection handler --
    and this server's handler only ends when the client socket does. Delegating
    to it meant Ctrl-C with DCS attached hung, the operator killed the process,
    and every sortie since the last disconnect was lost.
    """

    async def test_cancelling_serve_forever_tears_down_a_live_client(self) -> None:
        task = asyncio.create_task(self.server.serve_forever())
        client = await self.client()
        await self.hello(client)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=TIMEOUT)
        self.assertEqual(done, {task}, "serve_forever did not return after cancel")
        self.assertTrue(task.cancelled())
        self.assertFalse(self.server.connected)
        self.assertEqual(self.engine.disconnects, 1)

    async def test_close_releases_serve_forever(self) -> None:
        task = asyncio.create_task(self.server.serve_forever())
        client = await self.client()
        await self.hello(client)
        await self.server.close()
        done, _ = await asyncio.wait({task}, timeout=TIMEOUT)
        self.assertEqual(done, {task}, "serve_forever did not return after close")
        self.assertIsNone(task.exception())


class ShutdownTests(TransportTestCase):
    async def test_close_is_idempotent_and_leaves_no_tasks(self) -> None:
        client = await self.client()
        await self.hello(client)
        before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        await self.server.close()
        await self.server.close()
        await asyncio.sleep(0.05)
        leftover = {
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done() and t not in before
        }
        self.assertEqual(leftover, set())
        self.assertEqual(self.engine.disconnects, 1)


if __name__ == "__main__":
    unittest.main()
