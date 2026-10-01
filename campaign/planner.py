"""The minimal ATO: pick a target, build a package, schedule a time on target.

This is the smallest thing that can honestly be called air tasking. It picks
the highest-priority surviving target, checks whether a squadron can actually
cover a two-ship, reserves the airframes and ordnance up front, and works out
when the flight takes off, hits and gets home -- all from distance and a fixed
cruise speed, so the whole schedule is a pure function of the inputs.

A package is a set of **elements** sharing one time on target
(docs/design.md, section 5). Each element is a flight with a role: the strike
that is the package's reason to exist, and a SEAD element when the strike's
route is exposed to a live enemy air-defence site and the base has
anti-radiation missiles to send. Each has its own spawn id, its own
reservation and its own schedule, so each can be shot down, recovered,
scrubbed or stood down on its own. A package with only a strike element is
the one-flight package this module always built, and behaves exactly as it
did.

Nothing here knows which side it is planning for, or which side the humans
fly: a coalition's package is built from that coalition's inventory against
the other side's targets, and the campaign asks once per coalition
(docs/design.md, section 4). That is what keeps red subject to exactly the
rules blue is.

A side may have several packages open at once, as many as its squadrons are
ready for (docs/design.md, "Sortie rate"); the campaign keeps two of them off
one target and one squadron out of two of them.

TODO(seam): escort, tanker and AWACS elements are built here, off the same
target selection, and then deconflicted against other packages on time and
route: shared routes, shared envelopes, one package's SEAD covering
another's strike. Nothing deconflicts open packages yet.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from campaign.oob import (
    ANTI_RADIATION_MUNITIONS,
    InsufficientInventory,
    SideInventory,
    Squadron,
)
from campaign.protocol import Vec3
from campaign.theater import Airbase, Target, bearing, ground_distance, interpolate

#: Standard strike flight in this slice.
FLIGHT_SIZE = 2

#: Rounds each aircraft carries, and expends, on a single pass.
ROUNDS_PER_AIRCRAFT = 2

#: A SEAD element is a two-ship with two anti-radiation missiles an aircraft,
#: the same shape as the strike it escorts. Kept as their own names because
#: nothing requires the two to stay equal.
SEAD_FLIGHT_SIZE = 2
SEAD_ROUNDS_PER_AIRCRAFT = 2

#: Seconds the SEAD element is scheduled ahead of the strike element, over
#: the same track. Suppression has to be under way before the strikers are
#: inside an envelope, not after. At the slice's paper-track ground speed of
#: about 140 m/s this is 17 km: blue's strikers cross into the SA-6's 24 km
#: envelope 339 s before their TOT, when the SEAD element, 120 s ahead, is
#: 8 km from the battery and closing on its abeam point (293 s before the
#: TOT), the closest it comes and the shortest shot it has. Red's route is
#: inside the Patriot's 160 km envelope from takeoff, Bassel al-Assad being
#: 146 km from the battery, and the same lead keeps the Su-24M SEAD element
#: 17 km ahead of the strikers all the way in. Much more and the SEAD
#: element is off the target and turning for home while the strikers are
#: still inbound; much less and the two are one formation, and each envelope
#: meets both at once.
#: A constant rather than one derived from each route's geometry, because on
#: paper exposure is resolved as one event at the TOT (section 3) and the
#: lead does not enter that arithmetic: it decides where the SEAD element is
#: on its paper track, which is when the bubble holds it and where DCS flies
#: it. Must stay below PLANNING_LEAD, or the SEAD element would take off
#: before its package was fragged.
SEAD_LEAD = 120.0

#: Package roles. `ROLE_ORDER` is the order elements are resolved in at the
#: TOT (docs/design.md, section 5): the SEAD element flies and shoots first,
#: so that what it does to the sites is what the strike element meets.
ROLE_STRIKE = "strike"
ROLE_SEAD = "sead"
ROLE_ORDER: tuple[str, ...] = (ROLE_SEAD, ROLE_STRIKE)

#: How a role is written in messages and callsigns.
ROLE_LABELS: dict[str, str] = {ROLE_STRIKE: "strike", ROLE_SEAD: "SEAD"}

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

#: Lifecycle, of an element and of the package made of them. `planned` and
#: `enroute` are open (inventory reserved); the rest are terminal (inventory
#: settled). A package is open while any of its elements is.
PLANNED = "planned"
ENROUTE = "enroute"
COMPLETE = "complete"
DESTROYED = "destroyed"
ABORTED = "aborted"

OPEN_STATES = frozenset({PLANNED, ENROUTE})
TERMINAL_STATES = frozenset({COMPLETE, DESTROYED, ABORTED})


@dataclass
class Element:
    """One flight of a package: its own entity from commitment to touchdown.

    It has its own spawn id, reservation and bubble membership, which is the
    whole point of an element (docs/design.md, section 5): the SEAD element
    can be shot down, arrive late or run dry while the strike flies on.
    """

    role: str
    spawn_id: str
    squadron_id: str
    #: Ties the element's inventory back to it; unique across the campaign.
    reservation_id: str
    flight_size: int
    munition: str
    rounds: int
    rounds_per_aircraft: int
    t_takeoff: float
    #: When this element is scheduled over the target. The strike's is the
    #: package's TOT; the SEAD element's is `SEAD_LEAD` earlier. It shapes the
    #: element's paper track and DCS route. Resolution happens once, for the
    #: whole package, at the package's TOT.
    t_tot: float
    t_rtb: float
    state: str = PLANNED
    #: Set when the package's TOT is resolved; after that this element's
    #: ordnance is gone whatever happens to the aircraft.
    weapons_released: bool = False
    #: Rounds of its munition gone from the sim since it was fragged, over
    #: every instantiation: fired, or carried down with an aircraft the sim
    #: destroyed -- a summed count cannot tell the two apart, and the paper
    #: may fire neither. Read from snapshots only, and only a SEAD element's
    #: (docs/design.md, section 5). Zero for an element never instantiated.
    sim_spent: int = 0
    #: The count of its munition last read during the current
    #: instantiation, starting from the client's spawn-time reading (the
    #: baseline). Every drop from one reading to the next is spent. A rise,
    #: which a jet in flight cannot do, is taken as the new reading, so a
    #: later drop is still counted. None until a snapshot of this
    #: instantiation arrives; each instantiation starts again, because the
    #: client builds every spawn with the template's loadout.
    ammo_seen: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def is_airborne(self, now: float) -> bool:
        return self.is_open and self.t_takeoff <= now < self.t_rtb

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "spawn_id": self.spawn_id,
            "squadron_id": self.squadron_id,
            "reservation_id": self.reservation_id,
            "flight_size": self.flight_size,
            "munition": self.munition,
            "rounds": self.rounds,
            "rounds_per_aircraft": self.rounds_per_aircraft,
            "t_takeoff": self.t_takeoff,
            "t_tot": self.t_tot,
            "t_rtb": self.t_rtb,
            "state": self.state,
            "weapons_released": self.weapons_released,
            "sim_spent": self.sim_spent,
            "ammo_seen": self.ammo_seen,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Element:
        return cls(
            role=raw["role"],
            spawn_id=raw["spawn_id"],
            squadron_id=raw["squadron_id"],
            reservation_id=raw["reservation_id"],
            flight_size=int(raw["flight_size"]),
            munition=raw["munition"],
            rounds=int(raw["rounds"]),
            rounds_per_aircraft=int(raw["rounds_per_aircraft"]),
            t_takeoff=float(raw["t_takeoff"]),
            t_tot=float(raw["t_tot"]),
            t_rtb=float(raw["t_rtb"]),
            state=raw["state"],
            weapons_released=bool(raw["weapons_released"]),
            sim_spent=int(raw["sim_spent"]),
            ammo_seen=None if raw["ammo_seen"] is None else int(raw["ammo_seen"]),
        )


@dataclass
class Package:
    """A set of elements with one target and one time on target.

    `elements` is kept in `ROLE_ORDER`, the order they are resolved in, so
    everything that walks a package walks it the same way on every replay.
    """

    id: str
    #: The side that flies it. Decides whose inventory it draws on, whose
    #: threat sites it faces and who is told about it -- never who plans.
    coalition: str
    callsign: str
    base_id: str
    target_id: str
    t_created: float
    #: The package's time on target: the strike element's, and the one
    #: instant at which every element's part in it is resolved.
    t_tot: float
    elements: list[Element] = field(default_factory=list)
    state: str = PLANNED
    #: Set once the package's TOT has been resolved.
    weapons_released: bool = False

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def element(self, role: str) -> Element | None:
        for element in self.elements:
            if element.role == role:
                return element
        return None

    @property
    def strike(self) -> Element:
        element = self.element(ROLE_STRIKE)
        if element is None:
            raise LookupError(f"package {self.id} has no strike element")
        return element

    @property
    def sead(self) -> Element | None:
        return self.element(ROLE_SEAD)

    def element_for(self, spawn_id: str) -> Element | None:
        for element in self.elements:
            if element.spawn_id == spawn_id:
                return element
        return None

    @property
    def t_takeoff(self) -> float:
        """When the first of its elements rolls."""
        return min(e.t_takeoff for e in self.elements)

    @property
    def t_rtb(self) -> float:
        """When the last of its elements is due home."""
        return max(e.t_rtb for e in self.elements)

    def name_of(self, element: Element) -> str:
        """What an element is called in messages and on the wire.

        The strike element carries the package's callsign, exactly as the
        one-flight package did; every other element carries it with its role,
        so the humans can tell whose jets were lost.
        """
        if element.role == ROLE_STRIKE:
            return self.callsign
        return f"{self.callsign} {ROLE_LABELS[element.role]}"

    def composition(self, *, sizes: bool, open_only: bool = False) -> str:
        """The package in words: strike first, then what supports it.

        "2-ship strike with 2-ship SEAD", or "strike with SEAD" without sizes.
        A strike-only package reads exactly as the one-flight package did.
        `open_only` leaves out elements already lost, scrubbed or stood down:
        a player told they were still on task would plan around an escort
        that is not there.
        """

        def words(element: Element) -> str:
            label = ROLE_LABELS[element.role]
            return f"{element.flight_size}-ship {label}" if sizes else label

        chosen = [e for e in self.elements if e.is_open or not open_only]
        strike = [e for e in chosen if e.role == ROLE_STRIKE]
        support = [e for e in chosen if e.role != ROLE_STRIKE]
        if not strike:
            return " and ".join(words(e) for e in support)
        text = words(strike[0])
        if support:
            text += " with " + " and ".join(words(e) for e in support)
        return text

    def settle_state(self) -> None:
        """Derive the package's state from its elements'.

        Open while any element is: en route once any has taken off. Once none
        is: complete if any element came home, destroyed if none did and one
        was lost, aborted if every element was scrubbed or stood down. A
        one-element package therefore ends in exactly its element's state, as
        the one-flight package did.
        """
        if any(e.is_open for e in self.elements):
            if any(e.state == ENROUTE for e in self.elements):
                self.state = ENROUTE
            return
        states = {e.state for e in self.elements}
        if COMPLETE in states:
            self.state = COMPLETE
        elif DESTROYED in states:
            self.state = DESTROYED
        else:
            self.state = ABORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "coalition": self.coalition,
            "callsign": self.callsign,
            "base_id": self.base_id,
            "target_id": self.target_id,
            "t_created": self.t_created,
            "t_tot": self.t_tot,
            "elements": [e.to_dict() for e in self.elements],
            "state": self.state,
            "weapons_released": self.weapons_released,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Package:
        return cls(
            id=raw["id"],
            coalition=raw["coalition"],
            callsign=raw["callsign"],
            base_id=raw["base_id"],
            target_id=raw["target_id"],
            t_created=float(raw["t_created"]),
            t_tot=float(raw["t_tot"]),
            elements=[Element.from_dict(e) for e in raw["elements"]],
            state=raw["state"],
            weapons_released=bool(raw["weapons_released"]),
        )


def targets_by_priority(theater: Any, enemy_coalition: str) -> list[Target]:
    """Surviving enemy targets, the one the planner wants most first.

    Ties break on target id so that two engines fed the same log make the same
    choice. Never a destroyed target: a package tasked against rubble would
    consume real inventory for nothing.
    """
    return sorted(
        theater.surviving_targets_of(enemy_coalition),
        key=lambda t: (t.priority, t.id),
        reverse=True,
    )


def select_target(theater: Any, enemy_coalition: str) -> Target | None:
    """Highest-priority surviving enemy target, or None if the list is empty."""
    ranked = targets_by_priority(theater, enemy_coalition)
    return ranked[0] if ranked else None


def _leg_time(base: Airbase, target: Target) -> float:
    return ground_distance(base.pos, target.pos) / CRUISE_SPEED


def reservation_id_for(package_id: str, role: str) -> str:
    return f"{package_id}-{role}"


#: Whether a squadron may fly `airframes` aircraft on an element taking off at
#: `t_takeoff` in a package whose TOT is `t_tot`: the readiness the campaign
#: knows and the planner does not (docs/design.md, "Sortie rate"). Asked only
#: of a squadron whose stock already covers the element.
Readiness = Callable[[Squadron, int, float, float], bool]


def package_schedule(base: Airbase, target: Target, now: float) -> tuple[float, float, float]:
    """(takeoff, TOT, home) of the strike element fragged at `now`."""
    leg = _leg_time(base, target)
    t_takeoff = now + PLANNING_LEAD
    t_tot = t_takeoff + leg + DEPARTURE_ALLOWANCE
    t_rtb = t_tot + leg + RECOVERY_ALLOWANCE
    return t_takeoff, t_tot, t_rtb


def build_package(
    *,
    package_id: str,
    spawn_id: str,
    inventory: SideInventory,
    base: Airbase,
    target: Target,
    now: float,
    rng: random.Random,
    threats: Sequence[Any] | Callable[[], Sequence[Any]] = (),
    next_spawn_id: Callable[[], str] | None = None,
    flight_size: int = FLIGHT_SIZE,
    rounds_per_aircraft: int = ROUNDS_PER_AIRCRAFT,
    munition: str | None = None,
    ready: Readiness | None = None,
) -> Package | None:
    """Commit a package, or return None if inventory cannot cover its strike.

    `spawn_id` is the strike element's. `threats` are the live enemy sites
    whose envelopes the route enters (`Theater.live_threats_along`); when there
    are any, and the base has a squadron with anti-radiation missiles to cover
    a SEAD element, one is attached, with a spawn id from `next_spawn_id`.
    Without either, the package is its strike element alone -- the one-flight
    package this always built, drawing the same dice. `threats` may be a
    callable returning them, asked only once the strike is reserved: most
    attempts to plan fail before that, and finding the sites is the dearest
    part of an attempt.

    `ready` is asked of each squadron whose stock could cover an element, and
    one it refuses is passed over exactly as a dry one is: the strike tries
    the base's next squadron, and the SEAD element is not attached. Without
    it every squadron with the stock is ready.

    On success every element's airframes and ordnance are already reserved
    when this returns: there is no window in which a package exists but its
    inventory does not, and no way for a caller to forget.
    """
    rounds = flight_size * rounds_per_aircraft
    t_takeoff, t_tot, t_rtb = package_schedule(base, target, now)
    squadron = _pick_squadron(
        inventory, base, flight_size, munition, rounds,
        lambda s: ready is None or ready(s, flight_size, t_takeoff, t_tot),
    )
    if squadron is None:
        return None
    chosen_munition = munition or _default_munition(squadron)
    if chosen_munition is None:
        return None
    strike_reservation = reservation_id_for(package_id, ROLE_STRIKE)
    try:
        squadron.reserve(strike_reservation, flight_size, chosen_munition, rounds)
    except InsufficientInventory:
        return None

    elements = [
        Element(
            role=ROLE_STRIKE,
            spawn_id=spawn_id,
            squadron_id=squadron.id,
            reservation_id=strike_reservation,
            flight_size=flight_size,
            munition=chosen_munition,
            rounds=rounds,
            rounds_per_aircraft=rounds_per_aircraft,
            t_takeoff=t_takeoff,
            t_tot=t_tot,
            t_rtb=t_rtb,
        )
    ]
    if callable(threats):
        threats = threats()
    if threats and next_spawn_id is not None:
        sead = _build_sead_element(
            package_id, inventory, base, t_takeoff, t_tot, t_rtb, next_spawn_id, ready
        )
        if sead is not None:
            elements.append(sead)
    return Package(
        id=package_id,
        coalition=inventory.coalition,
        callsign=rng.choice(CALLSIGNS),
        base_id=base.id,
        target_id=target.id,
        t_created=now,
        t_tot=t_tot,
        elements=sorted(elements, key=lambda e: ROLE_ORDER.index(e.role)),
    )


def _build_sead_element(
    package_id: str,
    inventory: SideInventory,
    base: Airbase,
    t_takeoff: float,
    t_tot: float,
    t_rtb: float,
    next_spawn_id: Callable[[], str],
    ready: Readiness | None = None,
) -> Element | None:
    """Reserve a SEAD two-ship, or None if the base has no ARMs to send.

    The same track as the strike, `SEAD_LEAD` seconds earlier all the way
    round, so the two elements share one paper route and one TOT. Out of the
    base's own squadrons only: a SEAD element from another field would fly
    another route, and the envelope it suppressed might not be the strike's.

    Readiness is asked about the package's TOT, not the element's own 120 s
    earlier: the TOT is the one instant the whole package is resolved at
    (docs/design.md, section 5), so it is the one the daylight rule judges.
    """
    rounds = SEAD_FLIGHT_SIZE * SEAD_ROUNDS_PER_AIRCRAFT
    for squadron in inventory.squadrons_at(base.id):
        munition = _anti_radiation_munition(squadron)
        if munition is None or not squadron.can_support(
            SEAD_FLIGHT_SIZE, munition, rounds
        ):
            continue
        if ready is not None and not ready(
            squadron, SEAD_FLIGHT_SIZE, t_takeoff - SEAD_LEAD, t_tot
        ):
            continue
        reservation = reservation_id_for(package_id, ROLE_SEAD)
        squadron.reserve(reservation, SEAD_FLIGHT_SIZE, munition, rounds)
        return Element(
            role=ROLE_SEAD,
            spawn_id=next_spawn_id(),
            squadron_id=squadron.id,
            reservation_id=reservation,
            flight_size=SEAD_FLIGHT_SIZE,
            munition=munition,
            rounds=rounds,
            rounds_per_aircraft=SEAD_ROUNDS_PER_AIRCRAFT,
            t_takeoff=t_takeoff - SEAD_LEAD,
            t_tot=t_tot - SEAD_LEAD,
            t_rtb=t_rtb - SEAD_LEAD,
        )
    return None


def _default_munition(squadron: Squadron) -> str | None:
    """The squadron's strike munition: its first store that is not an ARM.

    An anti-radiation missile has nothing to home on at a fuel depot, so a
    squadron that carries only ARMs cannot fly a strike at all.

    TODO(seam): weaponeering -- matching a warhead to a target's hardness --
    replaces this. One squadron, one strike store, for now.
    """
    for name in squadron.munitions_total:
        if name not in ANTI_RADIATION_MUNITIONS:
            return name
    return None


def _anti_radiation_munition(squadron: Squadron) -> str | None:
    """The first anti-radiation store the squadron holds, in declaration order."""
    for name in squadron.munitions_total:
        if name in ANTI_RADIATION_MUNITIONS:
            return name
    return None


def _pick_squadron(
    inventory: SideInventory,
    base: Airbase,
    flight_size: int,
    munition: str | None,
    rounds: int,
    ready: Callable[[Squadron], bool] | None = None,
) -> Squadron | None:
    for squadron in inventory.squadrons_at(base.id):
        wanted = munition or _default_munition(squadron)
        if wanted is None:
            continue
        if squadron.can_support(flight_size, wanted, rounds) and (
            ready is None or ready(squadron)
        ):
            return squadron
    return None


def strike_squadron(
    inventory: SideInventory,
    base: Airbase,
    ready: Callable[[Squadron], bool] | None = None,
) -> Squadron | None:
    """The squadron `build_package` would send a standard strike with, if any.

    Reserves nothing. `ready` filters squadrons whose stock covers it.
    """
    return _pick_squadron(
        inventory, base, FLIGHT_SIZE, None, FLIGHT_SIZE * ROUNDS_PER_AIRCRAFT, ready
    )


def can_strike_from(inventory: SideInventory, base: Airbase) -> bool:
    """Has the base the stock for a strike, were every squadron ready?

    Tells a base that is dry from one that is only busy, turning its jets
    round or out of sorties for the day: the first is news, the second is the
    tempo of a war.
    """
    return strike_squadron(inventory, base) is not None


def element_position(element: Element, base: Airbase, target: Target, now: float) -> Vec3:
    """Where the flight is at `now`, on the paper track it was planned onto.

    Pure, and a function of the schedule alone, so a flight that spent part of
    its mission outside the bubble comes back at the position it would have
    had if the bubble had never dropped it. This is the "paper track" every
    dynamic campaign needs: DCS only ever sees the part of it that is nearby.
    Each element has its own, because each has its own schedule.
    """
    if now < element.t_takeoff or now >= element.t_rtb:
        return base.pos
    if now < element.t_tot:
        span = element.t_tot - element.t_takeoff
        fraction = 1.0 if span <= 0.0 else (now - element.t_takeoff) / span
        return interpolate(base.pos, target.pos, fraction, CRUISE_ALTITUDE)
    span = element.t_rtb - element.t_tot
    fraction = 1.0 if span <= 0.0 else (now - element.t_tot) / span
    return interpolate(target.pos, base.pos, fraction, CRUISE_ALTITUDE)


def element_heading(element: Element, base: Airbase, target: Target, now: float) -> float:
    """Heading in radians for a spawn frame: outbound toward the target, then home."""
    if now < element.t_tot:
        return bearing(base.pos, target.pos)
    return bearing(target.pos, base.pos)


def _schedule_speed(distance: float, span: float) -> float:
    """Ground speed that covers `distance` in `span` seconds, within reason."""
    if span <= 0.0 or distance <= 0.0:
        return CRUISE_SPEED
    return min(MAX_ROUTE_SPEED, max(MIN_ROUTE_SPEED, distance / span))


def element_route(
    element: Element, base: Airbase, target: Target, now: float
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
    here = element_position(element, base, target, now)
    base_point = (base.pos[0], CRUISE_ALTITUDE, base.pos[2])
    legs: list[tuple[Vec3, float, float, str]] = [
        (here, CRUISE_ALTITUDE, CRUISE_SPEED, "turning_point")
    ]
    if now < element.t_tot:
        target_point = (target.pos[0], CRUISE_ALTITUDE, target.pos[2])
        legs.append(
            (
                target_point,
                CRUISE_ALTITUDE,
                _schedule_speed(
                    ground_distance(here, target.pos), element.t_tot - now
                ),
                "attack",
            )
        )
        egress_from, egress_span = target.pos, element.t_rtb - element.t_tot
    else:
        egress_from, egress_span = here, element.t_rtb - now
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
    "ROLE_ORDER",
    "ROLE_SEAD",
    "ROLE_STRIKE",
    "ROUNDS_PER_AIRCRAFT",
    "SEAD_FLIGHT_SIZE",
    "SEAD_LEAD",
    "SEAD_ROUNDS_PER_AIRCRAFT",
    "TERMINAL_STATES",
    "Element",
    "Package",
    "Readiness",
    "build_package",
    "can_strike_from",
    "element_heading",
    "element_position",
    "element_route",
    "estimated_mission_duration",
    "package_schedule",
    "reservation_id_for",
    "select_target",
    "strike_squadron",
    "targets_by_priority",
]
