"""The minimal ATO: pick a target, build a package, schedule a time on target.

This is the smallest thing that can honestly be called air tasking. It picks
the highest-priority surviving target, checks whether a squadron can actually
cover a two-ship, reserves the airframes and ordnance up front, and works out
when the flight takes off, hits and gets home -- all from distance and a fixed
cruise speed, so the whole schedule is a pure function of the inputs.

TODO(seam): SEAD, escort, tanker and AWACS packages are built here, off the
same target selection, and then deconflicted against each other on time and
route. This slice builds one strike package at a time and nothing else.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from campaign.oob import InsufficientInventory, SideInventory, Squadron
from campaign.protocol import Vec3
from campaign.theater import Airbase, Target, bearing, ground_distance, interpolate

#: Standard strike flight in this slice.
FLIGHT_SIZE = 2

#: Rounds each aircraft carries, and expends, on a single pass.
ROUNDS_PER_AIRCRAFT = 2

#: Seconds between the engine committing a package and the flight rolling.
#: Stands in for briefing, walk and start; the client never sees this window.
PLANNING_LEAD = 300.0

#: Cruise ground speed in m/s used for every leg. Deliberately one number:
#: per-airframe performance is content, not engine.
CRUISE_SPEED = 220.0

#: Cruise altitude in metres, handed to the client as the route altitude.
CRUISE_ALTITUDE = 6_000.0

#: Fixed padding on the outbound leg for start, taxi, departure and run-in.
DEPARTURE_ALLOWANCE = 420.0

#: Fixed padding on the recovery leg for approach and rollout.
RECOVERY_ALLOWANCE = 300.0

#: Band the schedule-derived route speed is held inside. A flight re-spawned
#: a few seconds before its TOT would otherwise be asked to fly at Mach 9.
MIN_ROUTE_SPEED = 100.0
MAX_ROUTE_SPEED = 300.0

#: Package callsigns, drawn with the campaign's seeded RNG so a replayed log
#: produces the same names as the original run.
CALLSIGNS: tuple[str, ...] = (
    "COWBOY",
    "VIPER",
    "DODGE",
    "ENFIELD",
    "UZI",
    "CHEVY",
    "PONTIAC",
    "FORD",
)

#: Package lifecycle. `planned` and `enroute` are open (inventory reserved);
#: the rest are terminal (inventory settled).
PLANNED = "planned"
ENROUTE = "enroute"
COMPLETE = "complete"
DESTROYED = "destroyed"
ABORTED = "aborted"

OPEN_STATES = frozenset({PLANNED, ENROUTE})
TERMINAL_STATES = frozenset({COMPLETE, DESTROYED, ABORTED})


@dataclass
class Package:
    """One tasked flight, from commitment to touchdown.

    The id is stable for the life of the package and is reused as the
    reservation id, which is what ties the inventory back to the tasking.
    """

    id: str
    callsign: str
    squadron_id: str
    base_id: str
    target_id: str
    spawn_id: str
    flight_size: int
    munition: str
    rounds: int
    rounds_per_aircraft: int
    t_created: float
    t_takeoff: float
    t_tot: float
    t_rtb: float
    state: str = PLANNED
    #: Set once the flight has reached its TOT; after that its ordnance is
    #: gone whatever happens to the aircraft.
    weapons_released: bool = False

    @property
    def reservation_id(self) -> str:
        return self.id

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def is_airborne(self, now: float) -> bool:
        return self.is_open and self.t_takeoff <= now < self.t_rtb

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "callsign": self.callsign,
            "squadron_id": self.squadron_id,
            "base_id": self.base_id,
            "target_id": self.target_id,
            "spawn_id": self.spawn_id,
            "flight_size": self.flight_size,
            "munition": self.munition,
            "rounds": self.rounds,
            "rounds_per_aircraft": self.rounds_per_aircraft,
            "t_created": self.t_created,
            "t_takeoff": self.t_takeoff,
            "t_tot": self.t_tot,
            "t_rtb": self.t_rtb,
            "state": self.state,
            "weapons_released": self.weapons_released,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Package:
        return cls(
            id=raw["id"],
            callsign=raw["callsign"],
            squadron_id=raw["squadron_id"],
            base_id=raw["base_id"],
            target_id=raw["target_id"],
            spawn_id=raw["spawn_id"],
            flight_size=int(raw["flight_size"]),
            munition=raw["munition"],
            rounds=int(raw["rounds"]),
            rounds_per_aircraft=int(raw["rounds_per_aircraft"]),
            t_created=float(raw["t_created"]),
            t_takeoff=float(raw["t_takeoff"]),
            t_tot=float(raw["t_tot"]),
            t_rtb=float(raw["t_rtb"]),
            state=raw["state"],
            weapons_released=bool(raw["weapons_released"]),
        )


def select_target(theater: Any, enemy_coalition: str) -> Target | None:
    """Highest-priority surviving enemy target, or None if the list is empty.

    Ties break on target id so that two engines fed the same log make the same
    choice. Never return a destroyed target: a package tasked against rubble
    would consume real inventory for nothing.
    """
    surviving = theater.surviving_targets_of(enemy_coalition)
    if not surviving:
        return None
    return max(surviving, key=lambda t: (t.priority, t.id))


def _leg_time(base: Airbase, target: Target) -> float:
    return ground_distance(base.pos, target.pos) / CRUISE_SPEED


def build_package(
    *,
    package_id: str,
    spawn_id: str,
    inventory: SideInventory,
    base: Airbase,
    target: Target,
    now: float,
    rng: random.Random,
    flight_size: int = FLIGHT_SIZE,
    rounds_per_aircraft: int = ROUNDS_PER_AIRCRAFT,
    munition: str | None = None,
) -> Package | None:
    """Commit a strike package, or return None if inventory cannot cover it.

    On success the airframes and ordnance are already reserved against
    `package_id` when this returns: there is no window in which a package
    exists but its inventory does not, and no way for a caller to forget.
    """
    rounds = flight_size * rounds_per_aircraft
    squadron = _pick_squadron(inventory, base, flight_size, munition, rounds)
    if squadron is None:
        return None
    chosen_munition = munition or _default_munition(squadron)
    if chosen_munition is None:
        return None
    try:
        squadron.reserve(package_id, flight_size, chosen_munition, rounds)
    except InsufficientInventory:
        return None

    leg = _leg_time(base, target)
    t_takeoff = now + PLANNING_LEAD
    t_tot = t_takeoff + leg + DEPARTURE_ALLOWANCE
    t_rtb = t_tot + leg + RECOVERY_ALLOWANCE
    return Package(
        id=package_id,
        callsign=rng.choice(CALLSIGNS),
        squadron_id=squadron.id,
        base_id=base.id,
        target_id=target.id,
        spawn_id=spawn_id,
        flight_size=flight_size,
        munition=chosen_munition,
        rounds=rounds,
        rounds_per_aircraft=rounds_per_aircraft,
        t_created=now,
        t_takeoff=t_takeoff,
        t_tot=t_tot,
        t_rtb=t_rtb,
    )


def _default_munition(squadron: Squadron) -> str | None:
    """The squadron's only munition type in this slice.

    TODO(seam): weaponeering -- matching a warhead to a target's hardness --
    replaces this. One squadron, one store, for now.
    """
    for name in squadron.munitions_total:
        return name
    return None


def _pick_squadron(
    inventory: SideInventory,
    base: Airbase,
    flight_size: int,
    munition: str | None,
    rounds: int,
) -> Squadron | None:
    for squadron in inventory.squadrons_at(base.id):
        wanted = munition or _default_munition(squadron)
        if wanted is None:
            continue
        if squadron.can_support(flight_size, wanted, rounds):
            return squadron
    return None


def package_position(package: Package, base: Airbase, target: Target, now: float) -> Vec3:
    """Where the flight is at `now`, on the paper track it was planned onto.

    Pure, and a function of the schedule alone, so a flight that spent part of
    its mission outside the bubble comes back at the position it would have
    had if the bubble had never dropped it. This is the "paper track" every
    dynamic campaign needs: DCS only ever sees the part of it that is nearby.
    """
    if now < package.t_takeoff or now >= package.t_rtb:
        return base.pos
    if now < package.t_tot:
        span = package.t_tot - package.t_takeoff
        fraction = 1.0 if span <= 0.0 else (now - package.t_takeoff) / span
        return interpolate(base.pos, target.pos, fraction, CRUISE_ALTITUDE)
    span = package.t_rtb - package.t_tot
    fraction = 1.0 if span <= 0.0 else (now - package.t_tot) / span
    return interpolate(target.pos, base.pos, fraction, CRUISE_ALTITUDE)


def package_heading(package: Package, base: Airbase, target: Target, now: float) -> float:
    """Heading in radians for a spawn frame: outbound toward the target, then home."""
    if now < package.t_tot:
        return bearing(base.pos, target.pos)
    return bearing(target.pos, base.pos)


def _schedule_speed(distance: float, span: float) -> float:
    """Ground speed that covers `distance` in `span` seconds, within reason."""
    if span <= 0.0 or distance <= 0.0:
        return CRUISE_SPEED
    return min(MAX_ROUTE_SPEED, max(MIN_ROUTE_SPEED, distance / span))


def package_route(
    package: Package, base: Airbase, target: Target, now: float
) -> list[tuple[Vec3, float, float, str]]:
    """The legs still ahead of the flight at `now`, as (pos, alt, speed, action).

    Returned as plain tuples rather than protocol Waypoints so the planner
    stays a pure ATO module; the Campaign wraps them for the wire.

    Two things a fixed base-target-base route gets wrong, both of which show
    up as a flight thrashing in and out of the bubble:

    * The route has to start where the flight *is*. The bubble re-instantiates
      a flight wherever it happens to be on its paper track, and a route that
      begins at the departure base sends it home again.
    * The leg speeds have to be the ones the schedule implies, not the nominal
      cruise speed. `CRUISE_SPEED` sizes the legs, but the departure and
      recovery allowances then stretch the schedule around them, so a client
      flying `CRUISE_SPEED` reaches the target minutes before the paper track
      says it should. The engine then believes the flight is somewhere it is
      not, and despawns it while the player is looking at it.
    """
    here = package_position(package, base, target, now)
    base_point = (base.pos[0], CRUISE_ALTITUDE, base.pos[2])
    legs: list[tuple[Vec3, float, float, str]] = [
        (here, CRUISE_ALTITUDE, CRUISE_SPEED, "turning_point")
    ]
    if now < package.t_tot:
        target_point = (target.pos[0], CRUISE_ALTITUDE, target.pos[2])
        legs.append(
            (
                target_point,
                CRUISE_ALTITUDE,
                _schedule_speed(
                    ground_distance(here, target.pos), package.t_tot - now
                ),
                "attack",
            )
        )
        egress_from, egress_span = target.pos, package.t_rtb - package.t_tot
    else:
        egress_from, egress_span = here, package.t_rtb - now
    legs.append(
        (
            base_point,
            CRUISE_ALTITUDE,
            _schedule_speed(ground_distance(egress_from, base.pos), egress_span),
            "landing",
        )
    )
    return legs


def estimated_mission_duration(base: Airbase, target: Target) -> float:
    """Takeoff-to-touchdown seconds for a strike on `target` out of `base`."""
    leg = _leg_time(base, target)
    return 2.0 * leg + DEPARTURE_ALLOWANCE + RECOVERY_ALLOWANCE


__all__ = [
    "ABORTED",
    "CALLSIGNS",
    "COMPLETE",
    "CRUISE_ALTITUDE",
    "CRUISE_SPEED",
    "DESTROYED",
    "ENROUTE",
    "FLIGHT_SIZE",
    "OPEN_STATES",
    "PLANNED",
    "PLANNING_LEAD",
    "ROUNDS_PER_AIRCRAFT",
    "TERMINAL_STATES",
    "Package",
    "build_package",
    "estimated_mission_duration",
    "package_heading",
    "package_position",
    "package_route",
    "select_target",
]
