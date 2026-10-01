"""Static theater layout: where things are, and how far apart they are.

Everything downstream of this module measures distance the same way, which is
the point of putting the geometry here rather than inlining ``math.hypot``
calls at each call site.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from campaign.protocol import Coalition, Vec3

# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
#
# A DCS Vec3 is (x, y, z) where **y is altitude**, x runs north and z runs
# east. It is not (x, y, altitude). Every range the campaign reasons about --
# bubble radius, strike leg, base separation -- is a *ground* range, so y is
# dropped. Using a naive three-component norm is the classic DCS scripting
# bug: it makes a target 10 km away on the deck and a target 10 km away at
# 30,000 ft look like different problems, and it makes the bubble breathe
# every time an aircraft climbs.
#
# `ground_distance` is therefore the default. `slant_distance` exists so that
# a caller who genuinely wants three dimensions has to say so out loud.

#: Index of the altitude component in a DCS Vec3.
ALTITUDE_AXIS = 1


def ground_distance(a: Vec3, b: Vec3) -> float:
    """Horizontal distance in metres between two DCS Vec3s, ignoring altitude."""
    return math.hypot(a[0] - b[0], a[2] - b[2])


def slant_distance(a: Vec3, b: Vec3) -> float:
    """True three-dimensional distance. Only for callers that mean it."""
    return math.dist(a, b)


def bearing(origin: Vec3, target: Vec3) -> float:
    """Heading in radians from `origin` to `target`, DCS convention.

    Zero is north (+x), increasing clockwise toward east (+z), which is what
    the `heading` field of a spawn frame expects.
    """
    return math.atan2(target[2] - origin[2], target[0] - origin[0]) % (2.0 * math.pi)


def ground_distance_to_segment(p: Vec3, a: Vec3, b: Vec3) -> float:
    """Horizontal distance from `p` to the nearest point of the leg `a`-`b`."""
    dx, dz = b[0] - a[0], b[2] - a[2]
    length_sq = dx * dx + dz * dz
    if length_sq == 0.0:
        return ground_distance(p, a)
    f = ((p[0] - a[0]) * dx + (p[2] - a[2]) * dz) / length_sq
    f = min(1.0, max(0.0, f))
    return math.hypot(p[0] - (a[0] + f * dx), p[2] - (a[2] + f * dz))


def interpolate(a: Vec3, b: Vec3, fraction: float, altitude: float) -> Vec3:
    """Point `fraction` of the way from `a` to `b` on the ground, at `altitude`."""
    f = min(1.0, max(0.0, fraction))
    return (
        a[0] + (b[0] - a[0]) * f,
        altitude,
        a[2] + (b[2] - a[2]) * f,
    )


def _vec3(raw: Any) -> Vec3:
    x, y, z = raw
    return (float(x), float(y), float(z))


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Airbase:
    """A place aircraft come from. Immutable in this slice.

    TODO(seam): base capture, runway/ramp damage and airfield supply belong
    here. They need the ground war, which is out of scope.
    """

    id: str
    name: str
    coalition: Coalition
    pos: Vec3

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "coalition": self.coalition,
            "pos": list(self.pos),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Airbase:
        return cls(
            id=raw["id"],
            name=raw["name"],
            coalition=raw["coalition"],
            pos=_vec3(raw["pos"]),
        )


@dataclass
class Target:
    """A fixed strategic target, modelled as a group of `units_initial` units.

    Damage is *only* ever reduced by an attrition reconciliation pass fed from
    `state` snapshots. Nothing else in the engine writes `units_alive`.
    """

    id: str
    name: str
    coalition: Coalition
    pos: Vec3
    priority: int
    template: str
    category: str
    units_initial: int
    units_alive: int
    #: Engine-owned identity, stable for the life of the entity across DCS
    #: restarts. Allocated lazily by the Campaign, blank until then.
    spawn_id: str = ""

    @property
    def destroyed(self) -> bool:
        return self.units_alive <= 0

    @property
    def damage_fraction(self) -> float:
        """0.0 untouched, 1.0 flattened."""
        if self.units_initial <= 0:
            return 1.0
        return 1.0 - (self.units_alive / self.units_initial)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "coalition": self.coalition,
            "pos": list(self.pos),
            "priority": self.priority,
            "template": self.template,
            "category": self.category,
            "units_initial": self.units_initial,
            "units_alive": self.units_alive,
            "spawn_id": self.spawn_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Target:
        return cls(
            id=raw["id"],
            name=raw["name"],
            coalition=raw["coalition"],
            pos=_vec3(raw["pos"]),
            priority=int(raw["priority"]),
            template=raw["template"],
            category=raw["category"],
            units_initial=int(raw["units_initial"]),
            units_alive=int(raw["units_alive"]),
            spawn_id=raw.get("spawn_id", ""),
        )


#: Each air-defence template's emitter and launcher, by the DCS unit type
#: names the client's TEMPLATES build it from (mission/campaign_client.lua:
#: `lead_type`, then `unit_type`). The emitter is the radar an anti-radiation
#: missile homes on, and without which the battery cannot engage. A Tor
#: vehicle is its own radar and launcher, so the SA-15 names one type twice:
#: every unit is an emitter, and the battery is blind only when it is gone.
#:
#: The SA-11's 9A310M1 and the Roland ADS carry fire-control radars of their
#: own and could in principle engage without the battery's search radar. Here
#: the lead type is the battery's one emitter, as docs/design.md, section 7,
#: decides, which makes those two easier to blind than the real systems.
#: TODO(threat-model): per-unit emitters, with the rest of the envelope model.
SITE_UNIT_TYPES: dict[str, tuple[str, str]] = {
    "SA-6_Kub_site": ("Kub 1S91 str", "Kub 2P25 ln"),
    "Patriot_site": ("Patriot str", "Patriot ln"),
    "SA-11_Buk_site": ("SA-11 Buk SR 9S18M1", "SA-11 Buk LN 9A310M1"),
    "SA-15_Tor_site": ("Tor 9A331", "Tor 9A331"),
    "Hawk_site": ("Hawk tr", "Hawk ln"),
    "Roland_site": ("Roland Radar", "Roland ADS"),
}


@dataclass(frozen=True)
class SiteRepair:
    """How fast a theater's air defences are repaired while DCS is not holding them.

    Seconds of repair work, accrued only while the site is outside DCS, to
    replace one destroyed radar and to restore one destroyed launcher. The
    two run side by side, one unit at a time each. None turns that half off,
    so a theater without repair runs the same code and changes nothing.

    TODO(seam: logistics): repair is free. A logistics model makes it draw
    spares and crews from a supply network, and stall when that is cut.
    """

    radar_time: float | None = None
    launcher_time: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"radar_time": self.radar_time, "launcher_time": self.launcher_time}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiteRepair:
        def seconds(value: Any) -> float | None:
            return None if value is None else float(value)

        return cls(
            radar_time=seconds(raw["radar_time"]),
            launcher_time=seconds(raw["launcher_time"]),
        )


#: No repair at all: a destroyed unit stays destroyed.
REPAIR_OFF = SiteRepair()


@dataclass
class ThreatSite:
    """An air-defence site: something that shoots at aircraft flying past it.

    Deliberately not a :class:`Target`. A strategic target is what a strike is
    *for*; a threat site is what a strike has to *survive*, and killing one for
    its own sake is DEAD work, not built. Kept in a separate collection so
    `planner.select_target` cannot pick one by accident.

    Strikable and instantiable exactly like a target: it has a tracked unit
    count, reduced by snapshots when DCS holds it and on paper by a SEAD
    element's missiles when it does not (docs/design.md, section 5), and a
    destroyed site stops shooting.

    Its units are typed (`units_by_type`, keyed by DCS unit type): a radar and
    launchers (`SITE_UNIT_TYPES`). An anti-radiation missile homes on the
    radar, so a paper ARM kill removes the radar, and a site whose radar is
    gone cannot engage until it is repaired (docs/design.md, section 7).

    TODO(threat-model): `engagement_radius` and `kill_probability` are flat
    placeholders -- one ground radius and one per-aircraft Pk for the whole
    site, regardless of altitude, terrain, EW or how many of its launchers are
    left. A real envelope model replaces them behind the same two fields.
    """

    id: str
    name: str
    coalition: Coalition
    pos: Vec3
    template: str
    category: str
    units_initial: int
    #: Settable as a bare count, which means the battery the template builds
    #: from that count: its radar first, then launchers, as a spawn without a
    #: `composition` builds it. The engine itself only ever changes the typed
    #: composition (`units_by_type`); see the property installed below.
    units_alive: int
    #: Ground range in metres inside which the site engages.
    engagement_radius: float
    #: Chance that one pass through the envelope kills one aircraft.
    kill_probability: float
    #: Engine-owned identity, allocated lazily by the Campaign like a target's.
    spawn_id: str = ""
    #: Seconds of repair work done toward the next radar and the next
    #: launcher (`SiteRepair`). Saved, because a reload mid-repair must not
    #: start the work over.
    radar_repair: float = 0.0
    launcher_repair: float = 0.0
    #: Campaign time until which the battery's radar is off the air, forced
    #: down by an anti-radiation missile on paper; None when it is not. A
    #: paper state only: the radar is alive, so no snapshot can show it, and
    #: a spawn carries it to DCS as emission control instead. Saved.
    dark_until: float | None = None

    @property
    def radar_type(self) -> str:
        return SITE_UNIT_TYPES[self.template][0]

    @property
    def launcher_type(self) -> str:
        return SITE_UNIT_TYPES[self.template][1]

    def front_built(self, units: int) -> dict[str, int]:
        """The units the template builds from a bare count: radar first.

        What a spawn without a `composition` builds (docs/protocol.md), and
        so the battery a count with no types describes.
        """
        units = max(0, units)
        if self.radar_type == self.launcher_type:
            built = {self.radar_type: units}
        else:
            built = {self.radar_type: min(units, 1), self.launcher_type: max(0, units - 1)}
        return {t: n for t, n in sorted(built.items()) if n > 0}

    @property
    def full_battery(self) -> dict[str, int]:
        """Every unit the site has when nothing is destroyed."""
        return self.front_built(self.units_initial)

    @property
    def radars_alive(self) -> int:
        return self.units_by_type.get(self.radar_type, 0)

    @property
    def can_engage(self) -> bool:
        """Has the site a radar to engage with?

        Launchers do not enter it: the kill probability was already flat
        whatever was left of them (TODO(threat-model) above), and on paper
        nothing takes a launcher off a battery -- only DCS does.
        """
        return self.radars_alive > 0

    def engages_at(self, t: float) -> bool:
        """Can the site engage at campaign time `t`?

        It needs a radar, and the radar on the air: one an anti-radiation
        missile forced down is off until `dark_until`, and then simply back
        (docs/design.md, section 7).
        """
        return self.can_engage and (self.dark_until is None or t >= self.dark_until)

    @property
    def destroyed(self) -> bool:
        return self.units_alive <= 0

    def covers(self, a: Vec3, b: Vec3) -> bool:
        """Does the leg `a`-`b` enter this site's engagement envelope?"""
        return ground_distance_to_segment(self.pos, a, b) <= self.engagement_radius

    def outranged_by(self, launch_range: float) -> bool:
        """Can a weapon with this launch range reach the site from outside it?

        Strictly greater: a missile that can only be fired from the edge of
        the envelope has to be carried to that edge, and a site whose radius
        is at least the launch range gets its shot at the carrier.
        """
        return launch_range > self.engagement_radius

    def repair(self, dt: float, rates: SiteRepair) -> list[str]:
        """`dt` seconds of repair work. Returns the unit types restored, in order.

        The radar and the launchers are separate jobs, each restoring one
        unit per period and carrying the remainder toward the next. Work
        toward a unit that is not missing is not banked: a battery whole again
        starts from nothing when it next loses something. Draws no dice.
        Whether to call it at all -- never while DCS holds the site, never
        for a site with nothing left -- is the caller's (`Campaign._repair`).
        """
        restored: list[str] = []
        full = self.full_battery
        jobs = [("radar_repair", self.radar_type, rates.radar_time)]
        if self.launcher_type != self.radar_type:
            jobs.append(("launcher_repair", self.launcher_type, rates.launcher_time))
        for attribute, unit_type, period in jobs:
            missing = full.get(unit_type, 0) - self.units_by_type.get(unit_type, 0)
            if period is None or missing <= 0:
                setattr(self, attribute, 0.0)
                continue
            progress = getattr(self, attribute) + dt
            while missing > 0 and progress >= period:
                progress -= period
                missing -= 1
                self.units_by_type[unit_type] = self.units_by_type.get(unit_type, 0) + 1
                restored.append(unit_type)
            setattr(self, attribute, progress if missing > 0 else 0.0)
        self.units_by_type = dict(sorted(self.units_by_type.items()))
        return restored

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "coalition": self.coalition,
            "pos": list(self.pos),
            "template": self.template,
            "category": self.category,
            "units_initial": self.units_initial,
            "units_alive": self.units_alive,
            "units_by_type": dict(sorted(self.units_by_type.items())),
            "engagement_radius": self.engagement_radius,
            "kill_probability": self.kill_probability,
            "spawn_id": self.spawn_id,
            "radar_repair": self.radar_repair,
            "launcher_repair": self.launcher_repair,
            "dark_until": self.dark_until,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ThreatSite:
        site = cls(
            id=raw["id"],
            name=raw["name"],
            coalition=raw["coalition"],
            pos=_vec3(raw["pos"]),
            template=raw["template"],
            category=raw["category"],
            units_initial=int(raw["units_initial"]),
            units_alive=int(raw["units_alive"]),
            engagement_radius=float(raw["engagement_radius"]),
            kill_probability=float(raw["kill_probability"]),
            spawn_id=raw.get("spawn_id", ""),
            radar_repair=float(raw["radar_repair"]),
            launcher_repair=float(raw["launcher_repair"]),
            dark_until=None if raw["dark_until"] is None else float(raw["dark_until"]),
        )
        site.units_by_type = {
            str(t): int(n) for t, n in sorted(raw["units_by_type"].items()) if int(n) > 0
        }
        return site


def _units_alive(site: ThreatSite) -> int:
    return sum(site.units_by_type.values())


def _set_units_alive(site: ThreatSite, units: int) -> None:
    site.units_by_type = site.front_built(int(units))


# Installed after the dataclass is built, so the generated __init__ assigns
# `units_alive` through the setter: a site constructed, or edited, with a bare
# count gets the typed battery that count describes, and the two can never
# disagree. The field stays in the dataclass's fields, so equality and repr
# still see the count.
ThreatSite.units_alive = property(_units_alive, _set_units_alive)  # type: ignore[assignment]


#: Where the sun is reckoned from on the DCS Syria map: Aleppo, 36 deg 12'
#: N, 37 deg 12' E, the middle of the fighting -- between the two sides'
#: fields and inside the box their targets span. One point for the whole
#: map: across it sunrise moves by about a quarter of an hour, which is
#: finer than anything the daylight rule decides.
SYRIA_LATITUDE = 36.2
SYRIA_LONGITUDE = 37.2

#: The DCS Syria map's local time is UTC+3 (pydcs,
#: dcs/terrain/syria/syria.py, `utc_offset=datetime.timezone(
#: datetime.timedelta(hours=3))`), which is also Syria's and Turkey's
#: civil time year round. A mission's editor time is in it, and so is the
#: campaign's.
SYRIA_UTC_OFFSET = 3.0


@dataclass
class Theater:
    """The map: named airbases, strategic targets and threat sites.

    `latitude`, `longitude` and `utc_offset` say where on Earth the map is and
    what its local clock is, which is all the daylight rule needs to know
    (`campaign.sun`). Every theater in this repository is on the Syria map,
    so that is the default.

    TODO(seam): the front line, the ground order of battle and the logistics
    network hang off the theater. None of them exist in this slice.
    """

    name: str
    airbases: dict[str, Airbase] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    threats: dict[str, ThreatSite] = field(default_factory=dict)
    #: How fast this map's air defences are repaired (docs/design.md, section
    #: 7). Content, like the sites themselves: one mechanism, and a theater
    #: that wants none says so here.
    repair: SiteRepair = REPAIR_OFF
    latitude: float = SYRIA_LATITUDE
    longitude: float = SYRIA_LONGITUDE
    utc_offset: float = SYRIA_UTC_OFFSET

    def airbase(self, airbase_id: str) -> Airbase:
        return self.airbases[airbase_id]

    def airbases_of(self, coalition: Coalition) -> list[Airbase]:
        return [b for b in self.airbases.values() if b.coalition == coalition]

    def airbases_nearest(self, coalition: Coalition, pos: Vec3) -> list[Airbase]:
        """`coalition`'s airbases, nearest `pos` first, ties broken on id.

        The order a side tries its bases in when it plans against a target.
        Nearest first because the shortest leg is the shortest sortie and the
        least time in the enemy's envelopes; a dry base is passed over for the
        next nearest. The tie-break keeps the order a pure function of the map.
        """
        return sorted(
            self.airbases_of(coalition),
            key=lambda b: (ground_distance(b.pos, pos), b.id),
        )

    def targets_of(self, coalition: Coalition) -> list[Target]:
        return [t for t in self.targets.values() if t.coalition == coalition]

    def surviving_targets_of(self, coalition: Coalition) -> list[Target]:
        return [t for t in self.targets_of(coalition) if not t.destroyed]

    def defeated_coalitions(self) -> list[Coalition]:
        """Sides that held strategic targets and have none left, in name order.

        A side that never held one is not defeated by owning nothing: that is
        a map with nothing of its to strike, not a war it lost.
        """
        owners = sorted({t.coalition for t in self.targets.values()})
        return [c for c in owners if not self.surviving_targets_of(c)]

    def live_threats_along(
        self, coalition: Coalition, a: Vec3, b: Vec3, at: float | None = None
    ) -> list[ThreatSite]:
        """`coalition`'s engaging sites whose envelope the leg `a`-`b` enters.

        A site with no radar left is not among them: it cannot engage
        (docs/design.md, section 7), so it exposes no route, gives a SEAD
        element nothing to home on, and is no reason to send one. With `at`,
        nor is one whose radar will still be off the air then -- the planner
        asks at the package's TOT, the TOT at its own instant. Without it,
        only the radars count: the map with no clock.

        Sorted by id, because the caller rolls dice in this order and a replay
        has to roll them in the same one.
        """
        return sorted(
            (
                site
                for site in self.threats.values()
                if site.coalition == coalition
                and not site.destroyed
                and (site.can_engage if at is None else site.engages_at(at))
                and site.covers(a, b)
            ),
            key=lambda site: site.id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "airbases": {k: v.to_dict() for k, v in self.airbases.items()},
            "targets": {k: v.to_dict() for k, v in self.targets.items()},
            "threats": {k: v.to_dict() for k, v in self.threats.items()},
            "repair": self.repair.to_dict(),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "utc_offset": self.utc_offset,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Theater:
        return cls(
            name=raw["name"],
            airbases={k: Airbase.from_dict(v) for k, v in raw["airbases"].items()},
            targets={k: Target.from_dict(v) for k, v in raw["targets"].items()},
            threats={k: ThreatSite.from_dict(v) for k, v in raw["threats"].items()},
            repair=SiteRepair.from_dict(raw["repair"]),
            latitude=float(raw["latitude"]),
            longitude=float(raw["longitude"]),
            utc_offset=float(raw["utc_offset"]),
        )


# --------------------------------------------------------------------------
# The slice's theater
# --------------------------------------------------------------------------
#
# Coordinates are plausible Syria-map internal metres, not surveyed ones. The
# loop this slice proves does not depend on them being exact; swapping in real
# extracted map coordinates is a content change, not an engine change.

OPPOSING: dict[str, Coalition] = {"blue": "red", "red": "blue", "neutral": "neutral"}


def enemy_of(coalition: Coalition) -> Coalition:
    return OPPOSING[coalition]


#: Placeholder envelope for an SA-6 battery, as one ground radius: the
#: published maximum range of its 3M9 missile, 24 km (Wikipedia, "2K12 Kub",
#: infobox; its variant table gives 22 to 25 km). The published maximum and
#: not a trimmed one, because it is compared against the anti-radiation
#: missiles' published maxima (`oob.ANTI_RADIATION_LAUNCH_RANGE`) to decide
#: whether a SEAD element can stand off, and trimming one side of that
#: comparison would decide it by the trim. A maximum overstates the envelope
#: against a low flyer; the paper track has one altitude. See ThreatSite.
SA6_ENGAGEMENT_RADIUS = 24_000.0

#: Placeholder per-aircraft kill probability for one pass through an SA-6
#: envelope. Not derived from anything: it is set so that an undefended strike
#: route costs blue an airframe every few sorties, which is enough for an
#: offline war to be losable. TODO(threat-model) replaces it.
SA6_KILL_PROBABILITY = 0.15

#: Placeholder envelope for a Patriot battery, as one ground radius: the
#: system's published maximum range, 160 km (Wikipedia, "MIM-104 Patriot",
#: infobox; its table marks the PAC-2 GEM figure an estimate). Untrimmed for
#: the SA-6's reason. A flat radius stands for an envelope that really
#: depends on altitude, aspect and terrain. See ThreatSite.
PATRIOT_ENGAGEMENT_RADIUS = 160_000.0

#: Deliberately the SA-6's number, not a judgement that the two systems are
#: equals. Both are uncalibrated placeholders, and giving one side a better
#: guess than the other would decide the war by content nobody has measured.
#: TODO(threat-model) replaces both.
PATRIOT_KILL_PROBABILITY = SA6_KILL_PROBABILITY


def build_slice_theater() -> Theater:
    """Two airbases, and for each side one strategic target and one threat site.

    Built so that each side's only strike route runs through the other side's
    only envelope: the war is symmetric in shape, if not in content.
    """
    incirlik = Airbase(
        id="incirlik",
        name="Incirlik",
        coalition="blue",
        pos=(142_000.0, 0.0, -38_000.0),
    )
    bassel = Airbase(
        id="bassel_al_assad",
        name="Bassel al-Assad",
        coalition="red",
        pos=(-9_000.0, 0.0, 34_000.0),
    )
    depot = Target(
        id="latakia_fuel_depot",
        name="Latakia Fuel Depot",
        coalition="red",
        pos=(-3_000.0, 0.0, 41_000.0),
        priority=100,
        template="fuel_depot_medium",
        category="structure",
        units_initial=4,
        units_alive=4,
    )
    # Invented, like every coordinate here, and placed on purpose: about 5 km
    # off the Incirlik-Latakia strike leg and 25 km short of the depot, so
    # blue's only strike route runs through its envelope. A site covering the
    # northern approach to Latakia, not a surveyed battery.
    sa6 = ThreatSite(
        id="latakia_north_sa6",
        name="Latakia North SA-6",
        coalition="red",
        pos=(16_000.0, 0.0, 25_000.0),
        template="SA-6_Kub_site",
        category="ground",
        # One 1S91 radar and four 2P25 launchers: the battery the client's
        # SA-6_Kub_site template builds.
        units_initial=5,
        units_alive=5,
        engagement_radius=SA6_ENGAGEMENT_RADIUS,
        kill_probability=SA6_KILL_PROBABILITY,
    )
    # What red strikes (docs/design.md, section 4). Invented like the rest:
    # about 6 km from the Incirlik airbase position above, standing in for the
    # base's weapons storage area rather than surveying it.
    storage = Target(
        id="incirlik_munitions_storage",
        name="Incirlik Munitions Storage",
        coalition="blue",
        pos=(138_000.0, 0.0, -33_000.0),
        priority=100,
        template="munitions_storage_medium",
        category="structure",
        units_initial=4,
        units_alive=4,
    )
    # Placed as the SA-6 is, mirrored: about 5 km off the Bassel al-Assad to
    # Incirlik leg and about 15 km short of the storage area, so red's only
    # strike route runs through its envelope. A Patriot because NATO batteries
    # have in fact been deployed around Adana; the position is not theirs.
    patriot = ThreatSite(
        id="incirlik_patriot",
        name="Incirlik Patriot",
        coalition="blue",
        pos=(126_000.0, 0.0, -22_000.0),
        template="Patriot_site",
        category="ground",
        # One AN/MPQ-53 radar and four M901 launchers: the battery the
        # client's Patriot_site template builds.
        units_initial=5,
        units_alive=5,
        engagement_radius=PATRIOT_ENGAGEMENT_RADIUS,
        kill_probability=PATRIOT_KILL_PROBABILITY,
    )
    return Theater(
        name="Syria",
        airbases={incirlik.id: incirlik, bassel.id: bassel},
        targets={depot.id: depot, storage.id: storage},
        threats={sa6.id: sa6, patriot.id: patriot},
        # No repair: the slice's war is decided in two or three sorties, and
        # every test of it was written against sites that do not come back.
        # The same repair pass runs on it and restores nothing.
        repair=REPAIR_OFF,
    )


# --------------------------------------------------------------------------
# The Syria theater
# --------------------------------------------------------------------------
#
# A real map, sized so a war has an arc: several airbases a side, seven
# strategic targets a side at increasing depth, and air defences layered so
# that the deeper a target lies, the more envelopes a strike on it crosses.
#
# **Airbases** are the DCS Syria map's own, in DCS map coordinates, copied as
# facts from the pydcs library's generated terrain data
# (dcs/terrain/syria/airports.py, LGPL-3.0; numbers only, none of its code),
# each from its class's `mapping.Point(x, y)`. pydcs's x is northing and its
# y easting, which are this engine's Vec3 x and z (y is altitude here), so a
# pydcs Point(x, y) is placed at (x, 0.0, y). Checked by inverting the map's
# transverse Mercator projection: Incirlik comes out at 37.00 N 35.43 E with
# that mapping, and at 34.7 N 38.3 E -- the Syrian desert -- with the axes
# swapped.
#
# **Everything else is a game-design placement, not a real facility.** Each
# target and air-defence site is put at a stated offset from a DCS airbase on
# the map and named for that airbase only so a player can find it; none is a
# surveyed or researched installation. Priorities are game-design numbers
# too. They encode a campaign plan that works inward from the border, so each
# side's first sorties are short and lightly opposed and its last ones long
# and through layered defences. That ordering, not a judgement of what the
# targets are worth, is what gives the war its arc.


def _dcs_point(x: float, y: float) -> Vec3:
    """A pydcs map Point(x, y) as an engine Vec3: x north, y east, on the ground."""
    return (x, 0.0, y)


def _offset(anchor: Vec3, north: float, east: float) -> Vec3:
    """A game-design placement `north` and `east` metres from a map position."""
    return (anchor[0] + north, 0.0, anchor[2] + east)


# DCS airbase positions, each from the pydcs Syria class named beside it. The
# class's `id` is the DCS airdrome id, noted for when Airbase carries one (see
# the TODO(seam) on ramp starts in campaign.py) and unused until then.
SYRIA_INCIRLIK = _dcs_point(221207.773438, -35240.347656)  # Incirlik, id 16
SYRIA_HATAY = _dcs_point(147687.484375, 39418.742188)  # Hatay, id 15
SYRIA_GAZIANTEP = _dcs_point(210333.625365, 147313.672429)  # Gaziantep, id 11
SYRIA_ADANA = _dcs_point(219474.219224, -48324.287805)  # Adana_Sakirpasa, id 2
SYRIA_KAHRAMANMARAS = _dcs_point(276714.765625, 101896.460938)  # Kahramanmaras, id 75
SYRIA_SANLIURFA = _dcs_point(264719.125, 273812.4375)  # Sanliurfa, id 58
SYRIA_BASSEL = _dcs_point(42236.566406, 5836.231689)  # Bassel_Al_Assad, id 21
SYRIA_ABU_AL_DUHUR = _dcs_point(76048.957031, 111344.925781)  # Abu_al_Duhur, id 1
SYRIA_KUWEIRES = _dcs_point(125810.890625, 155253.8125)  # Kuweires, id 31
SYRIA_ALEPPO = _dcs_point(125576.863281, 123125.304688)  # Aleppo, id 27
SYRIA_HAMA = _dcs_point(8662.594238, 74333.1875)  # Hama, id 14
SYRIA_SHAYRAT = _dcs_point(-61368.212891, 90675.136719)  # Shayrat, id 36
SYRIA_TABQA = _dcs_point(76964.6875, 243605.210938)  # Tabqa, id 37

# Envelopes of the Syria map's site types, each one ground radius at the
# system's published maximum range, untrimmed, for the SA-6's reason: section
# 5 of docs/design.md compares them with the anti-radiation missiles'
# published maxima, and a trim would decide that comparison. Each is a
# placeholder for an envelope that really depends on altitude and terrain.

#: Red's area air defence, the 9K37M1 Buk-M1 the DCS SA-11 is: 35 km
#: (Wikipedia, "Buk missile system", comparison table, 9K37M1 "Buk-M1" with
#: 9M38/9M38M1 missiles, "3,3–35 km"; the article's general engagement zone
#: of 3-42 km belongs to later variants).
SA11_ENGAGEMENT_RADIUS = 35_000.0

#: Red's point defence: 12 km, the 9M331 missile's engagement range
#: (Wikipedia, "Tor missile system", Missiles section, "Engagement range is up
#: to 12 kilometres"). The 25 km in that article's infobox is the search
#: radar's detection range, not a range the system engages at.
SA15_ENGAGEMENT_RADIUS = 12_000.0

#: Blue's point defence: 8 km (Wikipedia, "Roland (missile)", infobox,
#: "Operational range: 8,000 m"). The same article gives 6.3 km for the
#: original system and 8.5 km for the later Roland 3.
ROLAND_ENGAGEMENT_RADIUS = 8_000.0

#: Blue's area air defence on the Syria map, a MIM-23 Hawk, which Turkey
#: operates: 50 km, the upper end of the published range (Wikipedia, "MIM-23
#: Hawk", infobox, "Operational range 28–31 mi (45–50 km)"; its text gives 45
#: km for the MIM-23K and 35 km for the MIM-23B to M). Not the Patriot the
#: slice uses: at its published 160 km, one Patriot battery covers
#: blue's whole side of the border, its shallow targets included, and the war
#: loses the depth that gives it an arc. That trade is content, not physics,
#: and the Patriot's radius stays its published figure.
HAWK_ENGAGEMENT_RADIUS = 50_000.0

#: The SA-6's placeholder, for the reason PATRIOT_KILL_PROBABILITY is. What
#: tells the types apart is reach, not lethality, until TODO(threat-model).
SA11_KILL_PROBABILITY = SA6_KILL_PROBABILITY
SA15_KILL_PROBABILITY = SA6_KILL_PROBABILITY
ROLAND_KILL_PROBABILITY = SA6_KILL_PROBABILITY
HAWK_KILL_PROBABILITY = SA6_KILL_PROBABILITY


#: Seconds of repair work, outside DCS, to put a destroyed radar back:
#: twelve hours. A PLACEHOLDER. No published figure for how long an air
#: defence takes to replace a radar was found; the nearest thing is
#: qualitative -- Wikipedia, "AGM-45 Shrike", on the Vietnam war: the
#: warhead's damage to a Fan Song radar rarely went "beyond a shattered
#: radar dish, an easy item to replace or repair". Half a day stands for
#: "easy, but not before the next raid". Chosen before any war was flown
#: with it, and not to be tuned against the loss rates it produces.
SYRIA_RADAR_REPAIR_TIME = 12 * 3600.0

#: Seconds of repair work, outside DCS, to restore one destroyed launcher: a
#: day. A PLACEHOLDER, with no published figure behind it either. Longer than
#: the radar's because nothing on paper destroys a launcher -- only DCS does,
#: with bombs, guns or missiles that wreck a vehicle rather than shatter a
#: dish -- and a wrecked launcher is replaced, not mended.
SYRIA_LAUNCHER_REPAIR_TIME = 24 * 3600.0

SYRIA_REPAIR = SiteRepair(
    radar_time=SYRIA_RADAR_REPAIR_TIME,
    launcher_time=SYRIA_LAUNCHER_REPAIR_TIME,
)


#: Red's strategic targets, which blue strikes, then blue's, which red
#: strikes: (id, name, owner, position, priority, client template, units).
#: Every position is a game-design placement -- an offset in metres (north,
#: east) from the DCS airbase the target is named for -- not a real facility.
#: Each side's run shallowest and highest priority first: two small,
#: undefended targets near the border; two middle ones of 12 units under one
#: envelope; three deep ones of 24 units under two or more (SYRIA_SITES).
#: Size, like depth, is what makes the end of a war slower and dearer than
#: its start.
SYRIA_TARGETS: tuple[tuple[str, str, Coalition, Vec3, int, str, int], ...] = (
    ("kuweires_fuel_storage", "Kuweires Fuel Storage", "red",
     _offset(SYRIA_KUWEIRES, -2_500.0, 3_000.0), 100, "fuel_depot_medium", 4),
    ("aleppo_command_post", "Aleppo Command Post", "red",
     _offset(SYRIA_ALEPPO, -3_000.0, -2_500.0), 95, "command_post_medium", 4),
    ("abu_al_duhur_airbase_infrastructure", "Abu al-Duhur Airbase Infrastructure",
     "red", _offset(SYRIA_ABU_AL_DUHUR, 1_500.0, -2_000.0), 80,
     "airbase_infrastructure_large", 12),
    ("bassel_al_assad_munitions_storage", "Bassel al-Assad Munitions Storage",
     "red", _offset(SYRIA_BASSEL, -4_000.0, 3_500.0), 75,
     "munitions_storage_large", 12),
    ("hama_fuel_storage", "Hama Fuel Storage", "red",
     _offset(SYRIA_HAMA, -3_500.0, 3_000.0), 60, "fuel_depot_large", 24),
    ("tabqa_airbase_infrastructure", "Tabqa Airbase Infrastructure", "red",
     _offset(SYRIA_TABQA, 2_000.0, -1_500.0), 50, "airbase_infrastructure_large", 24),
    ("shayrat_munitions_storage", "Shayrat Munitions Storage", "red",
     _offset(SYRIA_SHAYRAT, 2_500.0, -3_000.0), 35, "munitions_storage_large", 24),
    ("gaziantep_fuel_storage", "Gaziantep Fuel Storage", "blue",
     _offset(SYRIA_GAZIANTEP, 3_000.0, -2_500.0), 100, "fuel_depot_medium", 4),
    ("hatay_command_post", "Hatay Command Post", "blue",
     _offset(SYRIA_HATAY, 2_500.0, 3_000.0), 90, "command_post_medium", 4),
    ("kahramanmaras_airbase_infrastructure", "Kahramanmaras Airbase Infrastructure",
     "blue", _offset(SYRIA_KAHRAMANMARAS, -1_500.0, 2_000.0), 80,
     "airbase_infrastructure_large", 12),
    ("sanliurfa_fuel_storage", "Sanliurfa Fuel Storage", "blue",
     _offset(SYRIA_SANLIURFA, 3_000.0, -3_000.0), 70, "fuel_depot_large", 12),
    ("incirlik_munitions_storage", "Incirlik Munitions Storage", "blue",
     _offset(SYRIA_INCIRLIK, -4_000.0, 4_500.0), 60, "munitions_storage_large", 24),
    ("adana_fuel_storage", "Adana Fuel Storage", "blue",
     _offset(SYRIA_ADANA, 3_000.0, -3_000.0), 45, "fuel_depot_large", 24),
    ("incirlik_airbase_infrastructure", "Incirlik Airbase Infrastructure", "blue",
     _offset(SYRIA_INCIRLIK, 1_500.0, -2_500.0), 35, "airbase_infrastructure_large", 24),
)

#: Air-defence sites: (id, name, owner, position, client template, units,
#: engagement radius, kill probability). Game-design placements like the
#: targets. The layers mirror each other in what a strike meets, not in
#: site count, because the two sides' deep targets lie differently: one
#: envelope over each middle target, two over each deep one, counting the
#: envelopes the strike route from the nearest enemy base enters. Red's
#: deepest, Shayrat, has three: the Hama target lies 4.5 km off the route to
#: it, so nothing that defends Hama can stay off that route.
SYRIA_SITES: tuple[tuple[str, str, Coalition, Vec3, str, int, float, float], ...] = (
    # Red's deep targets are spread 70-210 km apart, so they take two area
    # sites: Hama's sits on the Hama target and across the route to Shayrat,
    # Tabqa's on the Tabqa target. A point-defence site on each deep target
    # makes the second envelope.
    ("hama_sa11", "Hama SA-11", "red", _offset(SYRIA_HAMA, -6_000.0, 2_000.0),
     "SA-11_Buk_site", 5, SA11_ENGAGEMENT_RADIUS, SA11_KILL_PROBABILITY),
    ("tabqa_sa11", "Tabqa SA-11", "red", _offset(SYRIA_TABQA, 6_000.0, -4_000.0),
     "SA-11_Buk_site", 5, SA11_ENGAGEMENT_RADIUS, SA11_KILL_PROBABILITY),
    ("abu_al_duhur_sa15", "Abu al-Duhur SA-15", "red",
     _offset(SYRIA_ABU_AL_DUHUR, 3_000.0, 1_000.0),
     "SA-15_Tor_site", 3, SA15_ENGAGEMENT_RADIUS, SA15_KILL_PROBABILITY),
    ("bassel_al_assad_sa15", "Bassel al-Assad SA-15", "red",
     _offset(SYRIA_BASSEL, -1_000.0, 5_000.0),
     "SA-15_Tor_site", 3, SA15_ENGAGEMENT_RADIUS, SA15_KILL_PROBABILITY),
    ("hama_sa15", "Hama SA-15", "red", _offset(SYRIA_HAMA, -2_000.0, 1_500.0),
     "SA-15_Tor_site", 3, SA15_ENGAGEMENT_RADIUS, SA15_KILL_PROBABILITY),
    ("tabqa_sa15", "Tabqa SA-15", "red", _offset(SYRIA_TABQA, 1_000.0, -1_000.0),
     "SA-15_Tor_site", 3, SA15_ENGAGEMENT_RADIUS, SA15_KILL_PROBABILITY),
    ("shayrat_sa15", "Shayrat SA-15", "red", _offset(SYRIA_SHAYRAT, 1_000.0, -1_000.0),
     "SA-15_Tor_site", 3, SA15_ENGAGEMENT_RADIUS, SA15_KILL_PROBABILITY),
    # Blue's three deep targets are within 13 km of each other around
    # Incirlik and Adana, so one Hawk covers them all. The second is the one
    # envelope over the Kahramanmaras target, a middle one, as an SA-15 is
    # over each of red's. Hawks, not Patriots: see HAWK_ENGAGEMENT_RADIUS.
    ("incirlik_hawk", "Incirlik Hawk", "blue",
     _offset(SYRIA_INCIRLIK, -5_000.0, -5_000.0),
     "Hawk_site", 5, HAWK_ENGAGEMENT_RADIUS, HAWK_KILL_PROBABILITY),
    ("kahramanmaras_hawk", "Kahramanmaras Hawk", "blue",
     _offset(SYRIA_KAHRAMANMARAS, 5_000.0, 0.0),
     "Hawk_site", 5, HAWK_ENGAGEMENT_RADIUS, HAWK_KILL_PROBABILITY),
    ("sanliurfa_roland", "Sanliurfa Roland", "blue",
     _offset(SYRIA_SANLIURFA, 1_500.0, -1_000.0),
     "Roland_site", 3, ROLAND_ENGAGEMENT_RADIUS, ROLAND_KILL_PROBABILITY),
    ("incirlik_roland", "Incirlik Roland", "blue",
     _offset(SYRIA_INCIRLIK, -1_000.0, 1_000.0),
     "Roland_site", 3, ROLAND_ENGAGEMENT_RADIUS, ROLAND_KILL_PROBABILITY),
    ("adana_roland", "Adana Roland", "blue", _offset(SYRIA_ADANA, 1_000.0, -1_000.0),
     "Roland_site", 3, ROLAND_ENGAGEMENT_RADIUS, ROLAND_KILL_PROBABILITY),
)


def build_syria_theater() -> Theater:
    """The DCS Syria map: seven strategic targets a side, layered defences.

    Blue flies from the Turkish side of the map, red from the Syrian side. A
    side plans each target from its airbase nearest that target
    (`Theater.airbases_nearest`), so the bases are chosen for which targets
    they are nearest, and a base nearest none would never fly:

    * Blue: **Hatay**, the nearest Turkish field to five of red's seven
      targets, so blue's main strike base; **Gaziantep**, the nearest to the
      eastern two, around Kuweires and Tabqa. Incirlik is not a blue base
      here, because one of those two is nearer every red target: it is
      blue's rear area instead, where its deepest targets are.
    * Red: **Bassel al-Assad**, on the coast and the nearest red field to
      Incirlik and Adana; **Kuweires**, east of Aleppo and the nearest to
      Gaziantep, Kahramanmaras and Sanliurfa; **Abu al-Duhur**, central and
      the nearest to the Hatay target, so a small detachment.

    A dry base hands its targets to the side's next nearest.
    """
    airbases = [
        Airbase(id="hatay", name="Hatay", coalition="blue", pos=SYRIA_HATAY),
        Airbase(id="gaziantep", name="Gaziantep", coalition="blue", pos=SYRIA_GAZIANTEP),
        Airbase(
            id="bassel_al_assad",
            name="Bassel al-Assad",
            coalition="red",
            pos=SYRIA_BASSEL,
        ),
        Airbase(id="kuweires", name="Kuweires", coalition="red", pos=SYRIA_KUWEIRES),
        Airbase(
            id="abu_al_duhur",
            name="Abu al-Duhur",
            coalition="red",
            pos=SYRIA_ABU_AL_DUHUR,
        ),
    ]
    targets = [
        Target(
            id=target_id,
            name=name,
            coalition=owner,
            pos=pos,
            priority=priority,
            template=template,
            category="structure",
            units_initial=units,
            units_alive=units,
        )
        for target_id, name, owner, pos, priority, template, units in SYRIA_TARGETS
    ]
    threats = [
        ThreatSite(
            id=site_id,
            name=name,
            coalition=owner,
            pos=pos,
            template=template,
            category="ground",
            units_initial=units,
            units_alive=units,
            engagement_radius=radius,
            kill_probability=kill_probability,
        )
        for site_id, name, owner, pos, template, units, radius, kill_probability
        in SYRIA_SITES
    ]
    return Theater(
        name="Syria",
        airbases={b.id: b for b in airbases},
        targets={t.id: t for t in targets},
        threats={s.id: s for s in threats},
        repair=SYRIA_REPAIR,
        latitude=SYRIA_LATITUDE,
        longitude=SYRIA_LONGITUDE,
        utc_offset=SYRIA_UTC_OFFSET,
    )
