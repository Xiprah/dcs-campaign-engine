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


@dataclass
class ThreatSite:
    """An air-defence site: something that shoots at aircraft flying past it.

    Deliberately not a :class:`Target`. A strategic target is what a strike is
    *for*; a threat site is what a strike has to *survive*, and killing one is
    DEAD work planned for its own sake (docs/design.md, section 5). Kept in a
    separate collection so `planner.select_target` cannot pick one by accident.

    Strikable and instantiable exactly like a target: it has a tracked unit
    count, reduced by snapshots when DCS holds it, and a destroyed site stops
    shooting. Nothing reduces it on paper yet; that is DEAD, section 5.

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
    units_alive: int
    #: Ground range in metres inside which the site engages.
    engagement_radius: float
    #: Chance that one pass through the envelope kills one aircraft.
    kill_probability: float
    #: Engine-owned identity, allocated lazily by the Campaign like a target's.
    spawn_id: str = ""

    @property
    def destroyed(self) -> bool:
        return self.units_alive <= 0

    def covers(self, a: Vec3, b: Vec3) -> bool:
        """Does the leg `a`-`b` enter this site's engagement envelope?"""
        return ground_distance_to_segment(self.pos, a, b) <= self.engagement_radius

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
            "engagement_radius": self.engagement_radius,
            "kill_probability": self.kill_probability,
            "spawn_id": self.spawn_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ThreatSite:
        return cls(
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
        )


@dataclass
class Theater:
    """The map: named airbases, strategic targets and threat sites.

    TODO(seam): the front line, the ground order of battle and the logistics
    network hang off the theater. None of them exist in this slice.
    """

    name: str
    airbases: dict[str, Airbase] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    threats: dict[str, ThreatSite] = field(default_factory=dict)

    def airbase(self, airbase_id: str) -> Airbase:
        return self.airbases[airbase_id]

    def airbases_of(self, coalition: Coalition) -> list[Airbase]:
        return [b for b in self.airbases.values() if b.coalition == coalition]

    def targets_of(self, coalition: Coalition) -> list[Target]:
        return [t for t in self.targets.values() if t.coalition == coalition]

    def surviving_targets_of(self, coalition: Coalition) -> list[Target]:
        return [t for t in self.targets_of(coalition) if not t.destroyed]

    def live_threats_along(
        self, coalition: Coalition, a: Vec3, b: Vec3
    ) -> list[ThreatSite]:
        """`coalition`'s surviving sites whose envelope the leg `a`-`b` enters.

        Sorted by id, because the caller rolls dice in this order and a replay
        has to roll them in the same one.
        """
        return sorted(
            (
                site
                for site in self.threats.values()
                if site.coalition == coalition
                and not site.destroyed
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
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Theater:
        return cls(
            name=raw["name"],
            airbases={k: Airbase.from_dict(v) for k, v in raw["airbases"].items()},
            targets={k: Target.from_dict(v) for k, v in raw["targets"].items()},
            threats={k: ThreatSite.from_dict(v) for k, v in raw["threats"].items()},
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


#: Placeholder envelope for an SA-6 battery: roughly the 2K12's published
#: maximum engagement range, trimmed, as one ground radius. See ThreatSite.
SA6_ENGAGEMENT_RADIUS = 20_000.0

#: Placeholder per-aircraft kill probability for one pass through an SA-6
#: envelope. Not derived from anything: it is set so that an undefended strike
#: route costs blue an airframe every few sorties, which is enough for an
#: offline war to be losable. TODO(threat-model) replaces it.
SA6_KILL_PROBABILITY = 0.15


def build_slice_theater() -> Theater:
    """Two airbases, one strategic target, one threat site. Nothing else."""
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
    # off the Incirlik-Latakia strike leg and 25 km short of the depot, so the
    # slice's only strike route runs through its envelope. A site covering the
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
    return Theater(
        name="Syria",
        airbases={incirlik.id: incirlik, bassel.id: bassel},
        targets={depot.id: depot},
        threats={sa6.id: sa6},
    )
