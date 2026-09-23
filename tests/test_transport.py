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
        self.live_spawns: list[Spawn] = []
        self.tick_frames: list[Downlink] = []
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _guard(self, name: str) -> None:
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
