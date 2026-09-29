"""Order of battle: who owns what, and how much of it is left.

Inventory in this engine is conserved, not merely decremented. Every airframe
and every round of ordnance is in exactly one of four buckets at all times:

    available + reserved + lost + expended == total

`reserve` moves stock into a named reservation, `release` puts whatever is
still reserved back, and `debit_*` moves reserved stock into the loss or
expenditure buckets. A cancelled or failed package therefore cannot leak
inventory: the only way out of a reservation is through one of those calls,
and both of them preserve the sum. :meth:`Squadron.check_invariant` is the
assertion that says so, and the tests lean on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from campaign.protocol import Coalition

#: Munitions that home on an emitting radar. What the planner looks for when
#: it wants a SEAD element (docs/design.md, section 5), and what it will not
#: hang on a strike: an anti-radiation missile has nothing to guide on at a
#: fuel depot. Content, like the stock itself: a side whose squadrons carry
#: none of these simply never flies SEAD.
ANTI_RADIATION_MUNITIONS = frozenset({"AGM-88C", "Kh-58U"})


class InsufficientInventory(Exception):
    """A reservation was asked for that the squadron cannot cover."""


class UnknownReservation(KeyError):
    """A reservation id that this squadron has never issued, or already closed."""


class InventoryCorrupt(AssertionError):
    """Conservation broke. Always an engine bug, never a data condition."""


@dataclass
class Reservation:
    """Stock held out of `available` for a package that has not resolved yet.

    Mutable: units are debited out of a live reservation as losses are
    observed, so a mid-flight save is honest about what is still airborne.
    """

    id: str
    squadron_id: str
    airframes: int
    munition: str
    rounds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "squadron_id": self.squadron_id,
            "airframes": self.airframes,
            "munition": self.munition,
            "rounds": self.rounds,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Reservation:
        return cls(
            id=raw["id"],
            squadron_id=raw["squadron_id"],
            airframes=int(raw["airframes"]),
            munition=raw["munition"],
            rounds=int(raw["rounds"]),
        )


@dataclass
class Squadron:
    """A flying unit with a finite airframe count and a finite munitions stock.

    TODO(seam): pilot records, experience and fatigue attach to the squadron.
    Out of scope for this slice; a package draws anonymous airframes.
    """

    id: str
    name: str
    coalition: Coalition
    airframe: str
    #: Client-side template name handed to DCS in a spawn frame.
    #:
    #: TODO(seam): real DCS unit-template fidelity -- correct loadouts, skill,
    #: liveries and per-airframe group composition -- lives behind this name.
    #: The slice treats it as an opaque string the client resolves.
    template: str
    home_base: str
    airframes_total: int
    airframes_available: int
    airframes_lost: int = 0
    munitions_total: dict[str, int] = field(default_factory=dict)
    munitions_available: dict[str, int] = field(default_factory=dict)
    munitions_expended: dict[str, int] = field(default_factory=dict)
    munitions_lost: dict[str, int] = field(default_factory=dict)
    open_reservations: dict[str, Reservation] = field(default_factory=dict)

    # -- queries ----------------------------------------------------------

    @property
    def airframes_reserved(self) -> int:
        return sum(r.airframes for r in self.open_reservations.values())

    def rounds_reserved(self, munition: str) -> int:
        return sum(
            r.rounds for r in self.open_reservations.values() if r.munition == munition
        )

    def can_support(self, airframes: int, munition: str, rounds: int) -> bool:
        return (
            self.airframes_available >= airframes
            and self.munitions_available.get(munition, 0) >= rounds
        )

    def check_invariant(self) -> None:
        """Raise if a single airframe or round has been created or destroyed."""
        held = self.airframes_available + self.airframes_reserved + self.airframes_lost
        if held != self.airframes_total:
            raise InventoryCorrupt(
                f"{self.id}: airframes {held} != total {self.airframes_total}"
            )
        for munition, total in self.munitions_total.items():
            counted = (
                self.munitions_available.get(munition, 0)
                + self.rounds_reserved(munition)
                + self.munitions_expended.get(munition, 0)
                + self.munitions_lost.get(munition, 0)
            )
            if counted != total:
                raise InventoryCorrupt(
                    f"{self.id}: {munition} {counted} != total {total}"
                )

    # -- mutation ---------------------------------------------------------

    def reserve(
        self, reservation_id: str, airframes: int, munition: str, rounds: int
    ) -> Reservation:
        if reservation_id in self.open_reservations:
            raise InsufficientInventory(f"reservation {reservation_id} already open")
        if not self.can_support(airframes, munition, rounds):
            raise InsufficientInventory(
                f"{self.id} cannot cover {airframes} airframes / {rounds} {munition}"
            )
        self.airframes_available -= airframes
        self.munitions_available[munition] -= rounds
        res = Reservation(
            id=reservation_id,
            squadron_id=self.id,
            airframes=airframes,
            munition=munition,
            rounds=rounds,
        )
        self.open_reservations[reservation_id] = res
        self.check_invariant()
        return res

    def _reservation(self, reservation_id: str) -> Reservation:
        try:
            return self.open_reservations[reservation_id]
        except KeyError:
            raise UnknownReservation(reservation_id) from None

    def debit_airframes(self, reservation_id: str, count: int) -> int:
        """Move `count` reserved airframes into the loss bucket.

        Returns the number actually debited, which is clamped to what the
        reservation still holds: a duplicate loss report must not invent
        airframes to destroy.
        """
        res = self._reservation(reservation_id)
        taken = min(count, res.airframes)
        res.airframes -= taken
        self.airframes_lost += taken
        self.check_invariant()
        return taken

    def debit_munitions(self, reservation_id: str, count: int, *, lost: bool) -> int:
        """Move `count` reserved rounds into the expended or lost bucket.

        `lost=True` means the ordnance went into the ground with its aircraft
        rather than onto a target. Both are conserved, they are just not the
        same fact about the war.
        """
        res = self._reservation(reservation_id)
        taken = min(count, res.rounds)
        res.rounds -= taken
        bucket = self.munitions_lost if lost else self.munitions_expended
        bucket[res.munition] = bucket.get(res.munition, 0) + taken
        self.check_invariant()
        return taken

    def release(self, reservation_id: str) -> None:
        """Close a reservation, returning everything still held to stock."""
        res = self.open_reservations.pop(reservation_id, None)
        if res is None:
            raise UnknownReservation(reservation_id)
        self.airframes_available += res.airframes
        self.munitions_available[res.munition] = (
            self.munitions_available.get(res.munition, 0) + res.rounds
        )
        self.check_invariant()

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "coalition": self.coalition,
            "airframe": self.airframe,
            "template": self.template,
            "home_base": self.home_base,
            "airframes_total": self.airframes_total,
            "airframes_available": self.airframes_available,
            "airframes_lost": self.airframes_lost,
            "munitions_total": dict(self.munitions_total),
            "munitions_available": dict(self.munitions_available),
            "munitions_expended": dict(self.munitions_expended),
            "munitions_lost": dict(self.munitions_lost),
            "open_reservations": {
                k: v.to_dict() for k, v in self.open_reservations.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Squadron:
        sqn = cls(
            id=raw["id"],
            name=raw["name"],
            coalition=raw["coalition"],
            airframe=raw["airframe"],
            template=raw["template"],
            home_base=raw["home_base"],
            airframes_total=int(raw["airframes_total"]),
            airframes_available=int(raw["airframes_available"]),
            airframes_lost=int(raw["airframes_lost"]),
            munitions_total={k: int(v) for k, v in raw["munitions_total"].items()},
            munitions_available={
                k: int(v) for k, v in raw["munitions_available"].items()
            },
            munitions_expended={k: int(v) for k, v in raw["munitions_expended"].items()},
            munitions_lost={k: int(v) for k, v in raw["munitions_lost"].items()},
            open_reservations={
                k: Reservation.from_dict(v)
                for k, v in raw["open_reservations"].items()
            },
        )
        sqn.check_invariant()
        return sqn


@dataclass
class SideInventory:
    """Everything one coalition can put in the air.

    TODO(seam): ground formations and the supply that feeds them -- SAM
    reloads included -- belong alongside `squadrons`. Not in this slice. Fixed
    air-defence sites are on the map instead (`theater.ThreatSite`): they are
    places a route passes, not stock a planner draws from.
    """

    coalition: Coalition
    squadrons: dict[str, Squadron] = field(default_factory=dict)

    def add(self, squadron: Squadron) -> None:
        self.squadrons[squadron.id] = squadron

    def squadron(self, squadron_id: str) -> Squadron:
        return self.squadrons[squadron_id]

    def squadrons_at(self, base_id: str) -> list[Squadron]:
        return [s for s in self.squadrons.values() if s.home_base == base_id]

    def find_capable(
        self, airframes: int, munition: str, rounds: int
    ) -> Squadron | None:
        """First squadron in declaration order that can cover the request.

        Declaration order, not "best", keeps selection deterministic. Weighing
        squadrons against a mission is planner work and this slice has one.
        """
        for squadron in self.squadrons.values():
            if squadron.can_support(airframes, munition, rounds):
                return squadron
        return None

    def reservation_holder(self, reservation_id: str) -> Squadron | None:
        for squadron in self.squadrons.values():
            if reservation_id in squadron.open_reservations:
                return squadron
        return None

    def release(self, reservation_id: str) -> None:
        holder = self.reservation_holder(reservation_id)
        if holder is None:
            raise UnknownReservation(reservation_id)
        holder.release(reservation_id)

    def check_invariant(self) -> None:
        for squadron in self.squadrons.values():
            squadron.check_invariant()

    def to_dict(self) -> dict[str, Any]:
        # A list, not a dict keyed by id, because declaration order is load
        # bearing: `find_capable` and `squadrons_at` return the first match in
        # this order. `Campaign.save` writes with sort_keys=True for diffable
        # saves, which would silently alphabetise a dict here -- so a reload
        # would start tasking a different squadron than the one that flew
        # before it, with nothing anywhere reporting a change. Order kept as
        # data survives that, the way the attrition tracker already does it.
        return {
            "coalition": self.coalition,
            "squadrons": [v.to_dict() for v in self.squadrons.values()],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SideInventory:
        return cls(
            coalition=raw["coalition"],
            squadrons={s["id"]: Squadron.from_dict(s) for s in raw["squadrons"]},
        )


def build_slice_oob() -> tuple[SideInventory, SideInventory]:
    """A strike squadron and a SEAD squadron a side, at each side's one base.

    F-16Cs at Incirlik and Su-24Ms at Bassel al-Assad, the same size with the
    same stock on purpose. Red plans under exactly blue's rules
    (docs/design.md, section 4), and an inventory tilted either way would
    decide the war before the rules got a say. Each side's air defences are
    `theater.ThreatSite`s on the map rather than inventory.

    Red gets anti-radiation missiles as blue does, for the same reason. The
    Su-24M really does carry the Kh-58U, and giving blue SEAD and red none
    would tilt the war by content nobody has measured. Each SEAD squadron is
    its own unit with its own template, because a SEAD element is its own
    flight (section 5): the strike squadron's jets are loaded for bombs.

    The strike squadron is declared first at each base, and must stay so:
    the planner takes the first squadron that can cover a strike, and a
    squadron carrying only anti-radiation missiles never can.

    TODO(seam): fighters, escorts and further squadrons hang off this, along
    with the multi-package deconfliction that would task them.
    """
    blue = SideInventory(coalition="blue")
    blue.add(
        Squadron(
            id="vfa_incirlik_f16",
            name="510th FS",
            coalition="blue",
            airframe="F-16C_50",
            template="F-16C_strike_jdam",
            home_base="incirlik",
            airframes_total=12,
            airframes_available=12,
            munitions_total={"GBU-38": 48},
            munitions_available={"GBU-38": 48},
        )
    )
    red = SideInventory(coalition="red")
    red.add(
        Squadron(
            # Bassel al-Assad is the airfield Russia operates as Hmeimim, and
            # Su-24Ms flew strike from it. The unit and its stock are invented.
            id="red_bassel_su24",
            name="Hmeimim Su-24M detachment",
            coalition="red",
            airframe="Su-24M",
            template="Su-24M_strike_fab",
            home_base="bassel_al_assad",
            airframes_total=12,
            airframes_available=12,
            munitions_total={"FAB-500": 48},
            munitions_available={"FAB-500": 48},
        )
    )
    # Smaller than the strike squadrons: eight airframes and twenty-four
    # missiles, six two-ship sorties at two missiles an aircraft. Enough to
    # escort a war the slice usually decides in two or three sorties a side,
    # few enough that a long one runs dry and its later strikes go in alone.
    # Invented, like the rest.
    blue.add(
        Squadron(
            id="vfa_incirlik_f16_sead",
            name="Incirlik SEAD detachment",
            coalition="blue",
            airframe="F-16C_50",
            template="F-16C_sead_harm",
            home_base="incirlik",
            airframes_total=8,
            airframes_available=8,
            munitions_total={"AGM-88C": 24},
            munitions_available={"AGM-88C": 24},
        )
    )
    red.add(
        Squadron(
            id="red_bassel_su24_sead",
            name="Hmeimim Su-24M SEAD detachment",
            coalition="red",
            airframe="Su-24M",
            template="Su-24M_sead_kh58",
            home_base="bassel_al_assad",
            airframes_total=8,
            airframes_available=8,
            munitions_total={"Kh-58U": 24},
            munitions_available={"Kh-58U": 24},
        )
    )
    return blue, red
