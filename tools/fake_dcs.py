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

``--loadout N``, ``--sead-shots N``
    What each aircraft carries of its template's munition, and how many
    missiles a SEAD pass fires. Snapshots report the ammunition aboard, as
    the Lua client reads it through Unit.getAmmo, and a pass that fires
    nothing destroys nothing. ``--loadout 0`` is DCS as it is today, with
    the client's pylons empty.

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
from collections import Counter, deque
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

#: The most units a template can hold -- this harness's stand-in for `count` in
#: the client's TEMPLATES table. `spawn` says how many to build; a count beyond
#: this is refused, exactly as mission/campaign_client.lua refuses it.
# TODO(templates): real DCS unit-template fidelity - loadouts, liveries, skill,
# per-unit types - belongs behind this map, not in the campaign.
DEFAULT_AIR_UNITS = 2
DEFAULT_GROUND_UNITS = 4
#: Templates whose size differs from their category's default above, with
#: the `count` the client's TEMPLATES gives them. Without this the harness
#: would refuse the slice's SA-6 and Patriot batteries that the client builds,
#: and the engine would block them for the rest of the run. (The red Su-24M
#: two-ship and the blue storage area's four objects are the defaults.)
TEMPLATE_CAPACITY: dict[str, int] = {"SA-6_Kub_site": 5, "Patriot_site": 5}
#: Who the harness's scripted defender is, by the coalition of the flight it
#: shoots at. Attribution only -- the engine may never act on it -- but a red
#: jet "killed by red_sa6_bassel" would make a misleading ledger to read.
DEFENDERS: dict[str, tuple[tuple[str, ...], str]] = {
    "blue": (("red_sa6_bassel", "red_sa8_bassel", "red_manpad"), "9M33"),
    "red": (("blue_patriot_incirlik", "blue_stinger_incirlik", "blue_manpad"), "MIM-104"),
}
#: What a SEAD element fires, by its coalition. Attribution only, like
#: DEFENDERS: the engine never learns a thing from it.
ANTI_RADIATION: dict[str, str] = {"blue": "AGM-88C", "red": "Kh-58U"}
#: The munition each air template carries, under the engine's name for it --
#: what the Lua client reports once its WEAPON_NAMES table has mapped DCS's
#: type name. `--loadout` says how many an aircraft carries.
TEMPLATE_MUNITION: dict[str, str] = {
    "F-16C_strike_jdam": "GBU-38",
    "Su-24M_strike_fab": "FAB-500",
    "F-16C_sead_harm": "AGM-88C",
    "Su-24M_sead_kh58": "Kh-58U",
}
#: Inbound frames handled per sim tick, mirroring the real client's cap.
MAX_FRAMES_PER_TICK = 32
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
    sead_kills: int = 0
    sead_range: float = 25_000.0
    #: Rounds of its template's munition each aircraft is built with. The
    #: engine reserves two an aircraft; zero is the empty pylons DCS has.
    loadout: int = 2
    #: Missiles a SEAD pass fires, at most what it carries; None fires all.
    sead_shots: int | None = None
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
    #: Logged once, not every step, when a strike cannot resolve for want of a
    #: target the engine has not instantiated.
    warned_no_target: bool = False
    #: Rounds aboard per weapon, summed over the living aircraft, and what was
    #: aboard when the group was built. None for anything that is not an
    #: aircraft, which the client reports no ammunition for either.
    ammo: dict[str, int] | None = None
    ammo_initial: dict[str, int] | None = None

    @property
    def name(self) -> str:
        return group_name(self.spawn_id)


def _whole(value: object) -> bool:
    """A JSON number with no fractional part, as Lua's `x == math.floor(x)` sees it."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


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
        #: Kept apart from `outcomes`, which is strikes: a SEAD element's shot
        #: is not a strike, and the two answer different questions.
        self.sead_outcomes: list[dict] = []
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
        # Inbound frames are queued here and handled at one fixed point in the
        # sim tick, never straight off the socket. Handling them in the reader
        # task raced the tick: `_on_despawn` sends a state snapshot, so the
        # snapshot's `t` and contents depended on which coroutine the event
        # loop happened to resume first. Events-on runs serialised themselves
        # by accident (every `_emit_event` is an await) while --drop-events
        # did not, so two identical no-event runs could disagree and the save
        # differ blamed the event stream for what was really a harness race.
        # It is also what the real client does: DCS Lua drains its socket
        # inside timer.scheduleFunction, and cannot have a reader running
        # concurrently with its own tick.
        self._inbox: deque[Downlink] = deque()
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
                self._inbox.append(frame)

    async def _drain_inbox(self) -> None:
        """Handle queued downlink frames at a fixed point in the tick.

        The real client caps this per tick and lets a backlog grow rather than
        hitch the sim; the cap here exists to keep that behaviour honest.
        """
        for _ in range(MAX_FRAMES_PER_TICK):
            if not self._inbox:
                return
            await self._handle(self._inbox.popleft())

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

    def _capacity_for(self, template: str, category: str) -> int:
        if template in self.cfg.template_units:
            return self.cfg.template_units[template]
        if template in TEMPLATE_CAPACITY:
            return TEMPLATE_CAPACITY[template]
        return self.cfg.air_units if category in _AIR_CATEGORIES else self.cfg.ground_units

    def _refusal(self, frame: Spawn) -> str | None:
        """Why the Lua client would refuse this spawn, in its words; else None.

        This harness may be no more forgiving than the client it stands in
        for. A spawn accepted here and refused in DCS is a bug the whole
        offline loop is blind to -- which is exactly how a strike flight once
        spawned with no attack task and every test stayed green.
        """
        # Same order as the client: the count is judged before the route is built.
        units = frame.units
        if not _whole(units) or units < 1:
            return f"bad units: {units!r} (want a positive integer)"
        capacity = self._capacity_for(frame.template, frame.category)
        if units > capacity:
            return (
                f"units {int(units)} exceeds template {frame.template} "
                f"capacity of {capacity}"
            )
        for wp in frame.route:
            if wp.airdrome_id is not None and not _whole(wp.airdrome_id):
                return f"bad spawn payload: malformed airdrome_id: {wp.airdrome_id!r}"
        return None

    async def _on_spawn(self, frame: Spawn) -> None:
        if frame.spawn_id in self.groups:
            await self._ack(frame.ref, False, f"duplicate spawn_id: {frame.spawn_id}")
            return
        refusal = self._refusal(frame)
        if refusal is not None:
            log.error("spawn %s: %s", frame.spawn_id, refusal)
            await self._ack(frame.ref, False, refusal)
            return
        tasking = dict(frame.tasking)
        units = int(frame.units)
        ammo: dict[str, int] | None = None
        if frame.category in _AIR_CATEGORIES:
            munition = TEMPLATE_MUNITION.get(frame.template)
            ammo = {}
            if munition is not None and self.cfg.loadout > 0:
                ammo[munition] = units * self.cfg.loadout
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
            ammo=ammo,
            ammo_initial=None if ammo is None else dict(ammo),
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
            # pop, not del: the census above expires groups that have finished
            # lingering dead, so despawning one of those would already have
            # removed the key -- and the KeyError would kill the reader task
            # and look, from the outside, like a dropped connection.
            self.groups.pop(frame.spawn_id, None)
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
            if tasking.get("kind") == "sead":
                await self._resolve_sead(group)
                continue
            if tasking.get("kind") != "strike":
                # TODO(packages): escort, tanker and AWACS taskings land here
                # as new `kind`s, each with its own resolution. Package
                # deconfliction (who shoots first when two arrive together) is
                # the engine's problem, not this loop's.
                continue
            target_name = tasking.get("target")
            target_id = spawn_id_of(target_name) if isinstance(target_name, str) else None
            target = self.groups.get(target_id) if target_id else None
            tot = tasking.get("tot")
            overdue = isinstance(tot, (int, float)) and self.t >= float(tot) + 120.0
            if target is None:
                # A target DCS has not instantiated cannot be struck, and this
                # harness does not get to pretend otherwise. Resolving the
                # strike anyway -- which the overdue path used to do -- made the
                # offline loop report a clean kill for a sortie that delivers
                # nothing in the sim, and the reconciliation proof rests on this
                # harness being no kinder than DCS.
                if overdue and not group.warned_no_target:
                    group.warned_no_target = True
                    log.warning(
                        "%s is past its TOT but %r is not instantiated; nothing "
                        "is struck, exactly as in DCS",
                        group.name,
                        target_name,
                    )
                continue
            in_range = _hdist(group.pos, target.pos) <= self.cfg.weapon_range
            if not in_range and not overdue:
                continue
            await self._strike(group, target)

    async def _resolve_sead(self, flight: SimGroup) -> None:
        """A SEAD element engages the named sites it has in range, once.

        Scripted like the strike: `--sead-kills` units come off every named
        site that is instantiated and within `--sead-range` when the flight
        first has one there. Zero by default, so the harness's standard runs
        are the ones they were before SEAD existed; the engine may only learn
        what happened from the next census either way. A site the engine has
        not instantiated cannot be engaged, exactly as in DCS.
        """
        tasking = flight.tasking
        names = tasking.get("targets")
        if not isinstance(names, list):
            names = []
        sites = [
            site
            for site in (
                self.groups.get(spawn_id_of(name))
                for name in names
                if isinstance(name, str)
            )
            if site is not None and site.alive
        ]
        in_range = [
            site for site in sites
            if _hdist(flight.pos, site.pos) <= self.cfg.sead_range
        ]
        tot = tasking.get("tot")
        overdue = isinstance(tot, (int, float)) and self.t >= float(tot) + 120.0
        if not in_range:
            if overdue and not flight.warned_no_target:
                flight.warned_no_target = True
                log.warning(
                    "%s is past its TOT with none of %r instantiated in range; "
                    "nothing is suppressed, exactly as in DCS",
                    flight.name,
                    names,
                )
            return
        flight.resolved = True
        self.struck.add(flight.spawn_id)
        weapon = ANTI_RADIATION.get(flight.coalition, "AGM-88C")
        munition = TEMPLATE_MUNITION.get(flight.template)
        carried = (flight.ammo or {}).get(munition, 0) if munition else 0
        shots = carried
        if self.cfg.sead_shots is not None:
            shots = min(carried, max(0, self.cfg.sead_shots))
        if munition is not None and flight.ammo is not None:
            self._expend(flight, munition, shots)
        if shots == 0:
            # No more forgiving than DCS: a jet with empty pylons makes its
            # pass and destroys nothing.
            log.info("SEAD at t=%.0f: %s carries nothing to fire", self.t, flight.name)
        for index, site in enumerate(in_range):
            # Shared over the sites in reach in the order they were named, as
            # the engine shares its paper missiles.
            missiles = shots // len(in_range) + (1 if index < shots % len(in_range) else 0)
            if missiles == 0:
                continue
            await self._emit_event(
                "shot", initiator=flight.name, target=site.name, weapon=weapon
            )
            before = site.units
            site.units = max(0, site.units - max(0, self.cfg.sead_kills))
            site.alive = site.units > 0
            for _ in range(before - site.units):
                await self._emit_event(
                    "hit", initiator=flight.name, target=site.name, weapon=weapon
                )
                await self._emit_event(
                    "kill", initiator=flight.name, target=site.name, weapon=weapon
                )
            if not site.alive:
                await self._emit_event("dead", initiator=site.name)
            self.sead_outcomes.append(
                {
                    "t": self.t,
                    "flight": flight.name,
                    "site": site.name,
                    "missiles": missiles,
                    "site_units_killed": before - site.units,
                    "site_alive": site.alive,
                }
            )
            log.info(
                "SEAD at t=%.0f: %s -> %s, %d unit(s) destroyed, %d left",
                self.t,
                flight.name,
                site.name,
                before - site.units,
                site.units,
            )

    async def _strike(self, flight: SimGroup, target: SimGroup) -> None:
        flight.resolved = True
        self.struck.add(flight.spawn_id)
        cfg = self.cfg
        munition = TEMPLATE_MUNITION.get(flight.template)
        carried = (flight.ammo or {}).get(munition, 0) if munition else 0
        if munition is not None and flight.ammo is not None:
            self._expend(flight, munition, carried)
        await self._emit_event(
            "shot",
            initiator=flight.name,
            target=target.name,
            weapon=cfg.weapon,
        )
        killed = 0
        # The bombs are checked after the die is thrown, so an unarmed flight
        # does not shift the harness's own dice for everything after it.
        if (
            cfg.target_outcome != "intact"
            and self.rng.random() < cfg.target_pk
            and carried > 0
        ):
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
            self._lose(flight, losses)
            # One draw from a three-name list either way, so which side is
            # being shot at does not move the harness's own dice.
            names, weapon = DEFENDERS.get(flight.coalition, DEFENDERS["blue"])
            defender = self.rng.choice(list(names))
            for _ in range(losses):
                await self._emit_event(
                    "hit", initiator=defender, target=flight.name, weapon=weapon
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
                "target": target.name,
                "target_units_killed": killed,
                "target_alive": target.alive,
                "flight_losses": losses,
                "flight_units": flight.units,
            }
        )
        log.info(
            "strike at t=%.0f: %s -> %s, target %s, flight %d/%d remaining",
            self.t,
            flight.name,
            target.name,
            "destroyed" if not target.alive else "damaged",
            flight.units,
            flight.units_initial,
        )

    @staticmethod
    def _expend(group: SimGroup, munition: str, count: int) -> None:
        if group.ammo is None or count <= 0:
            return
        group.ammo[munition] = max(0, group.ammo.get(munition, 0) - count)

    @staticmethod
    def _lose(group: SimGroup, count: int) -> None:
        """Aircraft shot down take their share of what is aboard with them."""
        before = group.units
        group.units -= count
        group.alive = group.units > 0
        if group.ammo is not None and before > 0:
            group.ammo = {
                weapon: rounds * group.units // before
                for weapon, rounds in sorted(group.ammo.items())
            }

    @staticmethod
    def _ammo_report(ammo: dict[str, int] | None, units: int) -> dict[str, int] | None:
        """What Unit.getAmmo summed over the living units would say.

        DCS lists a weapon only while some is aboard, and a group with nobody
        left alive has nothing to list.
        """
        if ammo is None:
            return None
        if units <= 0:
            return {}
        return {weapon: rounds for weapon, rounds in sorted(ammo.items()) if rounds > 0}

    def _observer_goal(self) -> tuple[float, float]:
        if self.t < self.cfg.observer_hold:
            return self.cfg.observer_from
        if self.cfg.chase:
            # Blue's strike flight if there is one, since that is what the
            # scripted outcome resolves; its SEAD element flies the same
            # track two minutes ahead and is chased only when it is alone.
            blue = [
                group for group in self.groups.values()
                if group.alive and group.category in _AIR_CATEGORIES and group.coalition == "blue"
            ]
            for group in sorted(blue, key=lambda g: g.tasking.get("kind") != "strike"):
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
                    ammo=self._ammo_report(group.ammo, group.units),
                    ammo_initial=self._ammo_report(group.ammo_initial, group.units_initial),
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
        self._inbox.clear()
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
            # Sync arrives through the inbox like everything else, so the
            # handshake has to drain rather than wait on the event alone.
            for _ in range(1000):
                await self._drain_inbox()
                if self._sync.is_set():
                    break
                if reader_task.done():
                    raise SystemExit("engine hung up before answering hello")
                await asyncio.sleep(0.01)
            else:
                raise SystemExit("engine never answered hello with sync")
            if self.finished:
                return "done"
            await self._send_observer()
            self._next_observer = self.t + self.observer_period
            return await self._sim_loop(reader_task)
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # A harness bug in here used to be indistinguishable from the
                # engine hanging up: `_sim_loop` sees the task done, returns
                # "closed", and `run()` quietly reconnects. Say so instead.
                log.exception("reader task failed; this is a harness bug")
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
            await self._drain_inbox()
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
            "sead_outcomes": self.sead_outcomes,
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
                   help="most units an air template holds, unless --template-units names it")
    p.add_argument("--ground-units", type=int, default=DEFAULT_GROUND_UNITS,
                   help="most units a ground template holds, unless --template-units names it")
    p.add_argument("--template-units", type=_template_units, action="append", default=[],
                   metavar="TEMPLATE=N",
                   help="most units one template holds; a spawn asking for more is "
                        "refused, as the Lua client refuses it; repeatable")
    p.add_argument("--weapon", default="GBU-38")
    p.add_argument("--weapon-range", type=float, default=9000.0)
    p.add_argument("--target-outcome", choices=["destroyed", "damaged", "intact"],
                   default="destroyed")
    p.add_argument("--target-pk", type=float, default=1.0)
    p.add_argument("--flight-losses", type=int, default=1,
                   help="airframes the package loses over the target")
    p.add_argument("--sead-kills", type=int, default=0,
                   help="units a SEAD element destroys on each named site it reaches")
    p.add_argument("--sead-range", type=float, default=25_000.0,
                   help="ground range at which a SEAD element engages a named site")
    p.add_argument("--loadout", type=int, default=2,
                   help="rounds of its template's munition each aircraft carries; "
                        "0 is DCS today, whose pylons the client leaves empty")
    p.add_argument("--sead-shots", type=int, default=None,
                   help="missiles a SEAD pass fires (default: everything it carries)")
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
        sead_kills=args.sead_kills,
        sead_range=args.sead_range,
        loadout=args.loadout,
        sead_shots=args.sead_shots,
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
