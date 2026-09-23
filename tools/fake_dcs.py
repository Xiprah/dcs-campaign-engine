#!/usr/bin/env python3
"""Offline DCS stand-in: runs the whole campaign loop with no DCS installed.

This speaks the *mission client* half of docs/protocol.md over a real socket,
so the engine under test cannot tell it apart from Lua-inside-DCS except by
what it does: connect and say hello, fly a scripted observer, obey spawns,
walk the spawned groups down their routes, resolve a scripted strike, and
report ground truth in `state` snapshots.

Three flags earn their keep:

``--drop-events``
    Suppresses every `event` frame while still sending every snapshot. This is
    how the reconciliation rule is proven: run once normally, once with this
    flag, and the campaign must end in identical state. Events are attribution
    only; they may enrich a loss, never create or withhold one.

``--seed``
    The only source of randomness. Same seed, same scenario, every time.

``--restart-at T``
    Hard-drops the socket at mission time T and reconnects, the way a DCS
    restart does. The client then knows nothing: the engine re-issues every
    spawn that should be live, which is what makes a campaign survive the sim.

Nothing here reads a wall clock for simulation purposes; mission time advances
in fixed steps, and ``--speed`` only decides how fast those steps are paced.

    python tools/fake_dcs.py --port 7777
    python tools/fake_dcs.py --port 7777 --drop-events --seed 7
    python tools/fake_dcs.py --port 7777 --restart-at 600
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campaign.protocol import (  # noqa: E402  (path bootstrap must run first)
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
    group_name,
    spawn_id_of,
)

log = logging.getLogger("fake_dcs")

# The observer is the only thing this harness positions itself, and the bubble
# is computed from it, so these have to agree with the engine's world model.
# They mirror campaign/theater.py's slice: the player starts at Incirlik and
# flies toward the Latakia fuel depot. --observer-from / --observer-to override
# them, and --chase keeps the observer with the package once one exists.
# TODO(world): read these from the theater once the engine publishes one,
# instead of keeping a second copy of the map here.
INCIRLIK_XZ = (142_000.0, -38_000.0)
TARGET_XZ = (-3_000.0, 41_000.0)

#: Group sizes the protocol cannot tell us: `spawn` names a template, and in
#: DCS the template decides how many units come with it.
# TODO(templates): real DCS unit-template fidelity - loadouts, liveries, skill,
# per-unit types - belongs behind this map, not in the campaign.
DEFAULT_AIR_UNITS = 2
DEFAULT_GROUND_UNITS = 4
_AIR_CATEGORIES = frozenset({"plane", "helicopter"})


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 7777
    theater: str = "Syria"
    dcs_version: str = "2.9.29"
    epoch: int = 1_758_499_200
    seed: int = 1
    step: float = 1.0
    speed: float = 60.0
    duration: float = 3600.0
    connect_timeout: float = 10.0
    max_reconnects: int = 3
    observer_from: tuple[float, float] = INCIRLIK_XZ
    observer_to: tuple[float, float] = TARGET_XZ
    observer_alt: float = 5000.0
    observer_speed: float = 220.0
    observer_hold: float = 120.0
    chase: bool = True
    drop_events: bool = False
    restart_at: float | None = None
    restart_gap: float = 15.0
    restart_keeps_clock: bool = False
    air_units: int = DEFAULT_AIR_UNITS
    ground_units: int = DEFAULT_GROUND_UNITS
    template_units: dict[str, int] = field(default_factory=dict)
    weapon_range: float = 9000.0
    weapon: str = "GBU-38"
    target_outcome: str = "destroyed"
    target_pk: float = 1.0
    flight_losses: int = 1
    dead_linger: int = 1
    takeoff_delay: float = 5.0
    spawn_timeout: float = 900.0
    summary: Path | None = None


@dataclass
class SimGroup:
    """One instantiated engine-owned DCS group, as the client sees it."""

    spawn_id: str
    coalition: str
    category: str
    template: str
    pos: list[float]
    heading: float
    route: list[Waypoint]
    tasking: dict
    units: int
    units_initial: int
    born_at: float
    alive: bool = True
    leg: int = 0
    rtb: bool = False
    landed: bool = False
    resolved: bool = False
    took_off: bool = False
    dead_reports: int = 0

    @property
    def name(self) -> str:
        return group_name(self.spawn_id)


def _hdist(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    """Horizontal distance. DCS positions are (x, altitude, z)."""
    return math.hypot(a[0] - b[0], a[2] - b[2])


class FakeDCS:
    """The mission-client side of the protocol, driven by a scripted mission."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.t = 0.0
        self.seq = 0
        self.groups: dict[str, SimGroup] = {}
        self.sent: Counter[str] = Counter()
        self.received: Counter[str] = Counter()
        self.outcomes: list[dict] = []
        self.messages: list[str] = []
        self.struck: set[str] = set()
        self.restarted = False
        self.finished = False
        self.saw_spawn = False
        self.state_period = 30.0
        self.observer_period = 5.0
        self.bubble_radius = 75_000.0
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._sync: asyncio.Event = asyncio.Event()
        self._drop_now = False
        self._observer_pos = [cfg.observer_from[0], cfg.observer_alt, cfg.observer_from[1]]
        self._next_state = 0.0
        self._next_observer = 0.0

    # -- frame plumbing ----------------------------------------------------

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    async def _send(self, frame: Uplink) -> None:
        if self.cfg.drop_events and isinstance(frame, Event):
            # The single chokepoint for --drop-events. If this ever fires the
            # reconciliation proof is worthless, so it is loud, not silent.
            raise RuntimeError("event frame escaped --drop-events")
        writer = self._writer
        if writer is None:
            return
        async with self._write_lock:
            try:
                writer.write(encode(frame))
                await writer.drain()
            except (ConnectionError, OSError) as exc:
                log.info("write failed: %s", exc)
                self._writer = None
                return
        self.sent[frame.type] += 1
        log.debug("-> %s", frame)

    async def _emit_event(self, kind: str, **kw: object) -> None:
        """Every event in this harness is born here, and only here."""
        if self.cfg.drop_events:
            return
        await self._send(Event(seq=self._next_seq(), t=self.t, kind=kind, **kw))  # type: ignore[arg-type]

    # -- inbound -----------------------------------------------------------

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        buffer = FrameBuffer()
        while True:
            chunk = await reader.read(8192)
            if not chunk:
                log.info("engine closed the connection")
                return
            for raw in buffer.feed(chunk):
                if not raw.strip():
                    continue
                try:
                    frame = decode_downlink(raw)
                except ProtocolError as exc:
                    log.error("undecodable downlink, dropping connection: %s", exc)
                    return
                self.received[frame.type] += 1
                log.debug("<- %s", frame)
                await self._handle(frame)

    async def _handle(self, frame: Downlink) -> None:
        if isinstance(frame, Sync):
            await self._on_sync(frame)
        elif isinstance(frame, Spawn):
            await self._on_spawn(frame)
        elif isinstance(frame, Despawn):
            await self._on_despawn(frame)
        elif isinstance(frame, Message):
            self.messages.append(frame.text)
            log.info("MSG [%s] %s", frame.to, frame.text)

    async def _on_sync(self, frame: Sync) -> None:
        if frame.protocol != PROTOCOL_VERSION:
            log.error("protocol mismatch: engine %s, client %s", frame.protocol, PROTOCOL_VERSION)
            self.finished = True
            self._drop_now = True
            return
        self.state_period = frame.state_period
        self.observer_period = frame.observer_period
        self.bubble_radius = frame.bubble_radius
        self._next_state = self.t + self.state_period
        self._next_observer = self.t
        self._sync.set()
        log.info(
            "sync: campaign_time=%.0f state=%.0fs observer=%.0fs bubble=%.0fm",
            frame.campaign_time,
            frame.state_period,
            frame.observer_period,
            frame.bubble_radius,
        )

    def _units_for(self, template: str, category: str, tasking: dict) -> int:
        """How many units a spawn actually brings.

        `spawn` has no unit-count field: in DCS the template decides. The
        engine does put one in `tasking` when it knows better - a package that
        already lost a jet comes back as a single-ship - and that is more
        truthful than any template default, so it wins.
        """
        declared = tasking.get("units")
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
            return declared
        if template in self.cfg.template_units:
            return self.cfg.template_units[template]
        return self.cfg.air_units if category in _AIR_CATEGORIES else self.cfg.ground_units

    async def _on_spawn(self, frame: Spawn) -> None:
        if frame.spawn_id in self.groups:
            await self._ack(frame.ref, False, f"duplicate spawn_id: {frame.spawn_id}")
            return
        tasking = dict(frame.tasking)
        units = self._units_for(frame.template, frame.category, tasking)
        group = SimGroup(
            spawn_id=frame.spawn_id,
            coalition=frame.coalition,
            category=frame.category,
            template=frame.template,
            pos=list(frame.position),
            heading=frame.heading,
            route=list(frame.route),
            tasking=tasking,
            units=units,
            units_initial=units,
            born_at=self.t,
            # An entity that already put its bombs on target does not do it
            # again when the bubble re-instantiates it. spawn_id is stable for
            # the life of the entity, so this survives a reconnect too.
            resolved=frame.spawn_id in self.struck,
        )
        self.groups[frame.spawn_id] = group
        self.saw_spawn = True
        log.info(
            "spawned %s (%s, %s) %d unit(s), %d waypoint(s), tasking=%s",
            group.name,
            frame.template,
            frame.category,
            units,
            len(group.route),
            frame.tasking or "-",
        )
        await self._ack(frame.ref, True)
        await self._emit_event("birth", initiator=group.name)

    async def _on_despawn(self, frame: Despawn) -> None:
        group = self.groups.get(frame.spawn_id)
        if group is not None:
            # Ground truth has to reach the engine before the entity stops
            # existing, so a loss can never be lost with it -- and it has to
            # be the *whole* census. A `state` frame is every instantiated
            # entity (docs/protocol.md); a report listing only the group being
            # despawned tells the engine that everything else disappeared, and
            # it will correctly write off a flight that is still flying.
            await self._send_state()
            del self.groups[frame.spawn_id]
            log.info("despawned %s (%s)", group.name, frame.reason or "no reason given")
        await self._ack(frame.ref, True)

    async def _ack(self, ref: int, ok: bool, error: str | None = None) -> None:
        await self._send(Ack(seq=self._next_seq(), t=self.t, ref=ref, ok=ok, error=error))

    # -- simulation --------------------------------------------------------

    def _advance_group(self, group: SimGroup, dt: float) -> None:
        # TODO(ground war): ground groups walk their route exactly like aircraft
        # here. A front line, contested movement and a logistics network all
        # need this to become a real ground movement model.
        if not group.alive or group.landed:
            return
        remaining = dt
        while remaining > 1e-9 and group.leg < len(group.route):
            wp = group.route[group.leg]
            target = (wp.pos[0], wp.alt or wp.pos[1], wp.pos[2])
            speed = wp.speed or (self.cfg.observer_speed if group.category in _AIR_CATEGORIES else 0.0)
            dist = _hdist(group.pos, target)
            if speed <= 0.0:
                return
            if dist <= speed * remaining:
                group.pos = [target[0], target[1], target[2]]
                group.leg += 1
                remaining -= dist / speed
                continue
            frac = (speed * remaining) / dist
            group.heading = math.atan2(target[2] - group.pos[2], target[0] - group.pos[0])
            group.pos = [
                group.pos[i] + (target[i] - group.pos[i]) * frac for i in range(3)
            ]
            remaining = 0.0
        if group.leg >= len(group.route) and group.route:
            self._route_complete(group)

    def _route_complete(self, group: SimGroup) -> None:
        if group.rtb:
            group.landed = True
            return
        if group.category not in _AIR_CATEGORIES:
            return
        group.rtb = True
        group.leg = 0
        group.route = [
            Waypoint(pos=wp.pos, alt=wp.alt, speed=wp.speed, action="turning_point")
            for wp in reversed(group.route)
        ]

    async def _fly(self, dt: float) -> None:
        for group in list(self.groups.values()):
            was_landed = group.landed
            self._advance_group(group, dt)
            if group.landed and not was_landed:
                await self._emit_event("land", initiator=group.name)
            if (
                not group.took_off
                and group.alive
                and group.category in _AIR_CATEGORIES
                and self.t - group.born_at >= self.cfg.takeoff_delay
            ):
                group.took_off = True
                await self._emit_event("takeoff", initiator=group.name)

    async def _resolve_strikes(self) -> None:
        for group in list(self.groups.values()):
            if group.resolved or not group.alive:
                continue
            tasking = group.tasking
            if tasking.get("kind") != "strike":
                # TODO(packages): SEAD, escort, tanker and AWACS taskings land
                # here as new `kind`s, each with its own resolution. Package
                # deconfliction (who shoots first when two arrive together) is
                # the engine's problem, not this loop's.
                continue
            target_name = tasking.get("target")
            target_id = spawn_id_of(target_name) if isinstance(target_name, str) else None
            target = self.groups.get(target_id) if target_id else None
            tot = tasking.get("tot")
            in_range = target is not None and _hdist(group.pos, target.pos) <= self.cfg.weapon_range
            overdue = isinstance(tot, (int, float)) and self.t >= float(tot) + 120.0
            if not in_range and not overdue:
                continue
            await self._strike(group, target, target_name)

    async def _strike(self, flight: SimGroup, target: SimGroup | None, target_name: object) -> None:
        flight.resolved = True
        self.struck.add(flight.spawn_id)
        cfg = self.cfg
        await self._emit_event(
            "shot",
            initiator=flight.name,
            target=target.name if target else None,
            weapon=cfg.weapon,
        )
        killed = 0
        if target is None:
            log.warning(
                "%s struck %r, which is not instantiated: the engine can only "
                "learn this from events, so --drop-events will show nothing",
                flight.name,
                target_name,
            )
        elif cfg.target_outcome != "intact" and self.rng.random() < cfg.target_pk:
            before = target.units
            if cfg.target_outcome == "destroyed":
                target.units = 0
                target.alive = False
            else:
                target.units = max(1, target.units - max(1, target.units // 2))
            killed = before - target.units
            for _ in range(min(killed, 4)):
                await self._emit_event(
                    "hit", initiator=flight.name, target=target.name, weapon=cfg.weapon
                )
                await self._emit_event(
                    "kill", initiator=flight.name, target=target.name, weapon=cfg.weapon
                )
            if not target.alive:
                await self._emit_event("dead", initiator=target.name)

        losses = min(cfg.flight_losses, flight.units)
        if losses:
            flight.units -= losses
            flight.alive = flight.units > 0
            defender = self.rng.choice(["red_sa6_bassel", "red_sa8_bassel", "red_manpad"])
            for _ in range(losses):
                await self._emit_event(
                    "hit", initiator=defender, target=flight.name, weapon="9M33"
                )
                await self._emit_event("dead", initiator=flight.name)
                ejected = self.rng.random() < 0.5
                # TODO(pilots): pilot records - who ejected, who was captured,
                # who flies again next sortie - hang off these two events.
                await self._emit_event("eject" if ejected else "pilot_dead", initiator=flight.name)

        self.outcomes.append(
            {
                "t": self.t,
                "flight": flight.name,
                "target": target.name if target else target_name,
                "target_units_killed": killed,
                "target_alive": target.alive if target else None,
                "flight_losses": losses,
                "flight_units": flight.units,
            }
        )
        log.info(
            "strike at t=%.0f: %s -> %s, target %s, flight %d/%d remaining",
            self.t,
            flight.name,
            target.name if target else target_name,
            "destroyed" if target and not target.alive else "damaged" if target else "unknown",
            flight.units,
            flight.units_initial,
        )

    def _observer_goal(self) -> tuple[float, float]:
        if self.t < self.cfg.observer_hold:
            return self.cfg.observer_from
        if self.cfg.chase:
            for group in self.groups.values():
                if group.alive and group.category in _AIR_CATEGORIES and group.coalition == "blue":
                    return (group.pos[0], group.pos[2])
        return self.cfg.observer_to

    def _move_observer(self, dt: float) -> None:
        gx, gz = self._observer_goal()
        dx, dz = gx - self._observer_pos[0], gz - self._observer_pos[2]
        dist = math.hypot(dx, dz)
        step = self.cfg.observer_speed * dt
        if dist <= step or dist == 0.0:
            self._observer_pos[0], self._observer_pos[2] = gx, gz
            return
        self._observer_pos[0] += dx / dist * step
        self._observer_pos[2] += dz / dist * step

    async def _send_observer(self) -> None:
        await self._send(
            ObserverReport(
                seq=self._next_seq(),
                t=self.t,
                observers=[
                    Observer(
                        id="player:Jakem",
                        pos=(self._observer_pos[0], self._observer_pos[1], self._observer_pos[2]),
                        speed=self.cfg.observer_speed,
                    )
                ],
            )
        )

    async def _send_state(self) -> None:
        """One complete census of everything instantiated. Never a subset."""
        snapshots: list[GroupSnapshot] = []
        expired: list[str] = []
        for group in list(self.groups.values()):
            if not group.alive:
                if group.dead_reports >= self.cfg.dead_linger:
                    continue
                group.dead_reports += 1
                if group.dead_reports >= self.cfg.dead_linger:
                    expired.append(group.spawn_id)
            snapshots.append(
                GroupSnapshot(
                    spawn_id=group.spawn_id,
                    alive=group.alive,
                    units=group.units,
                    units_initial=group.units_initial,
                    pos=(group.pos[0], group.pos[1], group.pos[2]),
                )
            )
        await self._send(StateReport(seq=self._next_seq(), t=self.t, groups=snapshots))
        for spawn_id in expired:
            # DCS stops reporting a destroyed group; the snapshot above already
            # told the engine it died, so nothing is lost by forgetting it.
            self.groups.pop(spawn_id, None)

    # -- session -----------------------------------------------------------

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        deadline = self.cfg.connect_timeout
        waited = 0.0
        while True:
            try:
                return await asyncio.open_connection(self.cfg.host, self.cfg.port)
            except (ConnectionRefusedError, OSError) as exc:
                if waited >= deadline:
                    raise SystemExit(f"cannot reach engine at {self.cfg.host}:{self.cfg.port}: {exc}")
                await asyncio.sleep(0.25)
                waited += 0.25

    async def _session(self) -> str:
        reader, writer = await self._connect()
        self._writer = writer
        self.seq = 0
        self._sync.clear()
        self._drop_now = False
        # DCS restarted: the client owns no persistent state, and the engine
        # re-issues every spawn that should be live.
        self.groups.clear()
        reader_task = asyncio.create_task(self._reader_loop(reader), name="fake-dcs-reader")
        try:
            await self._send(
                Hello(
                    seq=self._next_seq(),
                    t=self.t,
                    protocol=PROTOCOL_VERSION,
                    theater=self.cfg.theater,
                    dcs_version=self.cfg.dcs_version,
                    mission_start_epoch=self.cfg.epoch,
                )
            )
            try:
                await asyncio.wait_for(self._sync.wait(), timeout=10.0)
            except TimeoutError:
                raise SystemExit("engine never answered hello with sync")
            if self.finished:
                return "done"
            await self._send_observer()
            self._next_observer = self.t + self.observer_period
            return await self._sim_loop(reader_task)
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader_task
            self._writer = None
            with contextlib.suppress(Exception):
                if self._drop_now:
                    writer.transport.abort()
                else:
                    writer.close()
                    await writer.wait_closed()

    async def _sim_loop(self, reader_task: asyncio.Task[None]) -> str:
        cfg = self.cfg
        pause = (cfg.step / cfg.speed) if cfg.speed > 0 else 0.0
        warned = False
        while self.t < cfg.duration:
            if reader_task.done():
                return "closed"
            self.t += cfg.step
            self._move_observer(cfg.step)
            await self._fly(cfg.step)
            await self._resolve_strikes()
            if self.t >= self._next_observer:
                self._next_observer += self.observer_period
                await self._send_observer()
            if self.t >= self._next_state:
                self._next_state += self.state_period
                await self._send_state()
            if not warned and not self.saw_spawn and self.t >= cfg.spawn_timeout:
                warned = True
                log.warning(
                    "no spawn after %.0fs of mission time: the observer at %s is "
                    "probably nowhere near the engine's blue airbase (see "
                    "--observer-from)",
                    cfg.spawn_timeout,
                    (round(self._observer_pos[0]), round(self._observer_pos[2])),
                )
            if (
                cfg.restart_at is not None
                and not self.restarted
                and self.t >= cfg.restart_at
            ):
                self.restarted = True
                self._drop_now = True
                log.info("hard-dropping the connection at t=%.0f", self.t)
                return "restart"
            if self._writer is None:
                return "closed"
            # Always yield, even at --speed 0: the reader task has to get a
            # turn or downlink frames would never be processed.
            await asyncio.sleep(pause)
        self.finished = True
        return "done"

    async def run(self) -> int:
        reconnects = 0
        while not self.finished:
            reason = await self._session()
            if reason == "done" or self.finished:
                break
            if reason == "restart":
                if not self.cfg.restart_keeps_clock:
                    # A DCS restart resets timer.getTime(); the engine's own
                    # campaign clock is what survives, not the mission's.
                    self.t = 0.0
                    self._next_state = 0.0
                    self._next_observer = 0.0
                    self._observer_pos = [
                        self.cfg.observer_from[0],
                        self.cfg.observer_alt,
                        self.cfg.observer_from[1],
                    ]
                log.info("reconnecting after %.0fs", self.cfg.restart_gap)
                await asyncio.sleep(
                    self.cfg.restart_gap / self.cfg.speed if self.cfg.speed > 0 else 0.0
                )
                continue
            reconnects += 1
            if reconnects > self.cfg.max_reconnects:
                log.error("engine dropped us %d times; giving up", reconnects)
                return 1
            log.info("connection lost, reconnecting (%d/%d)", reconnects, self.cfg.max_reconnects)
        return 0

    def summary(self) -> dict:
        return {
            "seed": self.cfg.seed,
            "drop_events": self.cfg.drop_events,
            "mission_time": round(self.t, 3),
            "sent": dict(sorted(self.sent.items())),
            "received": dict(sorted(self.received.items())),
            "outcomes": self.outcomes,
            "messages": self.messages,
            "restarted": self.restarted,
            "live_groups": {
                g.spawn_id: {"units": g.units, "of": g.units_initial, "alive": g.alive}
                for g in self.groups.values()
            },
        }


def _xz(text: str) -> tuple[float, float]:
    try:
        x, z = (float(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"want 'x,z', got {text!r}") from None
    return (x, z)


def _template_units(text: str) -> tuple[str, int]:
    name, _, count = text.partition("=")
    if not name or not count.isdigit():
        raise argparse.ArgumentTypeError(f"want 'TEMPLATE=N', got {text!r}")
    return (name, int(count))


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        prog="fake_dcs",
        description="Offline DCS stand-in for the campaign engine.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--seed", type=int, default=1, help="the only source of randomness")
    p.add_argument("--drop-events", action="store_true", help="send no event frames at all")
    p.add_argument("--restart-at", type=float, default=None, metavar="T",
                   help="hard-drop the connection at this mission time and reconnect")
    p.add_argument("--restart-gap", type=float, default=15.0, help="mission seconds offline")
    p.add_argument("--restart-keeps-clock", action="store_true",
                   help="do not reset mission time on reconnect (a net drop, not a sim restart)")
    p.add_argument("--duration", type=float, default=3600.0,
                   help="mission seconds to simulate; the slice's own strike needs "
                        "about 2600 to take off, hit and land again")
    p.add_argument("--step", type=float, default=1.0, help="simulation step, mission seconds")
    p.add_argument("--speed", type=float, default=60.0,
                   help="mission seconds per wall second; 0 runs flat out")
    p.add_argument("--theater", default="Syria")
    p.add_argument("--dcs-version", default="2.9.29")
    p.add_argument("--epoch", type=int, default=1_758_499_200, help="mission_start_epoch in hello")
    p.add_argument("--observer-from", type=_xz, default=INCIRLIK_XZ, metavar="X,Z")
    p.add_argument("--observer-to", type=_xz, default=TARGET_XZ, metavar="X,Z")
    p.add_argument("--observer-alt", type=float, default=5000.0)
    p.add_argument("--observer-speed", type=float, default=220.0)
    p.add_argument("--observer-hold", type=float, default=120.0,
                   help="mission seconds the observer waits at base before departing")
    p.add_argument("--no-chase", dest="chase", action="store_false",
                   help="fly the scripted path instead of following the package")
    p.add_argument("--air-units", type=int, default=DEFAULT_AIR_UNITS,
                   help="units per air group when the template is unknown")
    p.add_argument("--ground-units", type=int, default=DEFAULT_GROUND_UNITS,
                   help="units per ground group when the template is unknown")
    p.add_argument("--template-units", type=_template_units, action="append", default=[],
                   metavar="TEMPLATE=N", help="exact unit count for one template; repeatable")
    p.add_argument("--weapon", default="GBU-38")
    p.add_argument("--weapon-range", type=float, default=9000.0)
    p.add_argument("--target-outcome", choices=["destroyed", "damaged", "intact"],
                   default="destroyed")
    p.add_argument("--target-pk", type=float, default=1.0)
    p.add_argument("--flight-losses", type=int, default=1,
                   help="airframes the package loses over the target")
    p.add_argument("--dead-linger", type=int, default=1,
                   help="snapshots a destroyed group still appears in before DCS forgets it")
    p.add_argument("--spawn-timeout", type=float, default=900.0)
    p.add_argument("--summary", type=Path, default=None, help="write the run summary here as JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="log every frame")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    return Config(
        host=args.host,
        port=args.port,
        theater=args.theater,
        dcs_version=args.dcs_version,
        epoch=args.epoch,
        seed=args.seed,
        step=args.step,
        speed=args.speed,
        duration=args.duration,
        observer_from=args.observer_from,
        observer_to=args.observer_to,
        observer_alt=args.observer_alt,
        observer_speed=args.observer_speed,
        observer_hold=args.observer_hold,
        chase=args.chase,
        drop_events=args.drop_events,
        restart_at=args.restart_at,
        restart_gap=args.restart_gap,
        restart_keeps_clock=args.restart_keeps_clock,
        air_units=args.air_units,
        ground_units=args.ground_units,
        template_units=dict(args.template_units),
        weapon=args.weapon,
        weapon_range=args.weapon_range,
        target_outcome=args.target_outcome,
        target_pk=args.target_pk,
        flight_losses=args.flight_losses,
        dead_linger=args.dead_linger,
        spawn_timeout=args.spawn_timeout,
        summary=args.summary,
    )


async def _amain(cfg: Config) -> int:
    sim = FakeDCS(cfg)
    try:
        code = await sim.run()
    finally:
        report = sim.summary()
        if cfg.summary is not None:
            cfg.summary.parent.mkdir(parents=True, exist_ok=True)
            cfg.summary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    return code


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    try:
        return asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
