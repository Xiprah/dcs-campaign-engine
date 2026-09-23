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

    TODO(seam): ground formations, SAM batteries and the supply that feeds
    them belong alongside `squadrons`. Not in this slice.
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
        return {
            "coalition": self.coalition,
            "squadrons": {k: v.to_dict() for k, v in self.squadrons.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SideInventory:
        return cls(
            coalition=raw["coalition"],
            squadrons={k: Squadron.from_dict(v) for k, v in raw["squadrons"].items()},
        )


def build_slice_oob() -> tuple[SideInventory, SideInventory]:
    """Blue gets one F-16C squadron at Incirlik. Red flies nothing yet.

    TODO(seam): red air, and the threat model that would make blue plan
    around it, go here. Out of scope.
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
    return blue, red
