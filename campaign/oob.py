"""Order of battle: who owns what, and how much of it is left.

Inventory in this engine is conserved, not merely decremented. Every airframe
is in exactly one of four buckets at all times, and every round of ordnance in
one of four:

    airframes: available + reserved + turning + lost == total
    rounds:    available + reserved + expended + lost == total

`reserve` moves stock into a named reservation, `release` puts whatever is
still reserved back -- an airframe that flew going first through turnaround
(`turning`) -- and `debit_*` moves reserved stock into the loss or
expenditure buckets. A cancelled or failed package therefore cannot leak
inventory: the only way out of a reservation is through one of those calls,
and all of them preserve the sum. :meth:`Squadron.check_invariant` is the
assertion that says so, and the tests lean on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from campaign.protocol import Coalition

#: Launch range in metres of each anti-radiation munition: how far from a
#: site a SEAD element can fire at it. A SEAD element whose missile out-ranges
#: a site engages it from outside its envelope and, on paper, is not exposed
#: to it (docs/design.md, section 5). Each figure is the munition's published
#: *maximum*, because each site's `engagement_radius` is its published
#: maximum too, and comparing one statistic against a different one would
#: decide the standoff by the choice of statistic. Placeholders, untuned.
#: TODO(threat-model): launch range really depends on release altitude and
#: speed, as the sources below say; the paper track has one altitude.
ANTI_RADIATION_LAUNCH_RANGE: dict[str, float] = {
    # 80 nmi "standoff", Wikipedia, "AGM-88 HARM", infobox (uncited there;
    # the same infobox gives 25 km low-level and 80 km medium-level). No
    # separate figure is published for the C model. The USAF fact sheet
    # gives only "48 plus kilometers".
    "AGM-88C": 148_000.0,
    # Wikipedia, "Kh-58", infobox, "Kh-58U: 250 km", citing the Journal of
    # Electronic Defense's International Electronic Countermeasures Handbook
    # (2004). The same article gives the original Kh-58 36 km from low level
    # and 120 km from 10,000 m.
    "Kh-58U": 250_000.0,
}

#: Munitions that home on an emitting radar. What the planner looks for when
#: it wants a SEAD element (docs/design.md, section 5), and what it will not
#: hang on a strike: an anti-radiation missile has nothing to guide on at a
#: fuel depot. Content, like the stock itself: a side whose squadrons carry
#: none of these simply never flies SEAD.
ANTI_RADIATION_MUNITIONS = frozenset(ANTI_RADIATION_LAUNCH_RANGE)


def launch_range(munition: str) -> float:
    """How far from its target `munition` can be released, in metres.

    Zero for anything not in :data:`ANTI_RADIATION_LAUNCH_RANGE`, so a
    munition nobody has given a range out-ranges nothing and its carrier
    flies into every envelope on its route, as every flight did before.
    """
    return ANTI_RADIATION_LAUNCH_RANGE.get(munition, 0.0)


@dataclass(frozen=True)
class SortieRate:
    """How hard a squadron can be worked: docs/design.md, "Sortie rate".

    Content, like the airframe count, so it lives with the squadron and comes
    from the order of battle. Every squadron goes through the same readiness
    code; a squadron that is not constrained simply carries the values that
    constrain nothing (:data:`UNCONSTRAINED`).
    """

    #: Seconds an airframe that has landed spends being refuelled and rearmed
    #: before it can be fragged again.
    turnaround: float = 0.0
    #: Sustained aircraft sorties a day per airframe on strength, or None for
    #: no daily limit.
    sorties_per_day: float | None = None
    #: Whether the squadron may only be fragged for a time on target in
    #: daylight.
    #: TODO(seam): night-capable squadrons (LANTIRN, the Su-24M's own night
    #: attack kit) clear this; nothing distinguishes them yet.
    day_only: bool = False

    def daily_limit(self, strength: int) -> int | None:
        """Aircraft sorties a day for `strength` airframes, or None if unlimited.

        Floored, because a fraction of a sortie cannot be flown; the epsilon
        keeps 1.35 * 20 from flooring to 26 on a float that came out a hair
        under 27.
        """
        if self.sorties_per_day is None:
            return None
        return math.floor(self.sorties_per_day * strength + 1e-9)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turnaround": self.turnaround,
            "sorties_per_day": self.sorties_per_day,
            "day_only": self.day_only,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SortieRate:
        rate = raw["sorties_per_day"]
        return cls(
            turnaround=float(raw["turnaround"]),
            sorties_per_day=None if rate is None else float(rate),
            day_only=bool(raw["day_only"]),
        )


#: No turnaround, no daily limit, any hour: the slice's squadrons, which its
#: tests and its recorded wars were written against.
UNCONSTRAINED = SortieRate()


@dataclass
class Turnaround:
    """Airframes back from one sortie, not ready until `ready_at`.

    One record per landing rather than per airframe: airframes are anonymous,
    and every airframe that comes home in one element lands at the same
    instant, so a per-airframe timestamp would carry nothing this does not.
    """

    airframes: int
    ready_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"airframes": self.airframes, "ready_at": self.ready_at}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Turnaround:
        return cls(airframes=int(raw["airframes"]), ready_at=float(raw["ready_at"]))


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
    sortie_rate: SortieRate = UNCONSTRAINED
    #: Airframes home from a sortie and not yet ready, in landing order.
    turning: list[Turnaround] = field(default_factory=list)
    #: Aircraft sorties fragged, by the local day (`campaign.sun.DAY`s since
    #: 1970-01-01) each element takes off on. What the daily limit counts.
    sorties_by_day: dict[int, int] = field(default_factory=dict)

    # -- queries ----------------------------------------------------------

    @property
    def airframes_reserved(self) -> int:
        return sum(r.airframes for r in self.open_reservations.values())

    @property
    def airframes_turning(self) -> int:
        return sum(batch.airframes for batch in self.turning)

    @property
    def airframes_on_strength(self) -> int:
        """Every airframe the squadron still has, whatever it is doing."""
        return self.airframes_total - self.airframes_lost

    def sorties_left(self, day: int) -> int | None:
        """Aircraft sorties the daily limit still allows on `day`, or None."""
        limit = self.sortie_rate.daily_limit(self.airframes_on_strength)
        if limit is None:
            return None
        return limit - self.sorties_by_day.get(day, 0)

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
        held = (
            self.airframes_available
            + self.airframes_reserved
            + self.airframes_turning
            + self.airframes_lost
        )
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

    def release(self, reservation_id: str, *, landed_at: float | None = None) -> None:
        """Close a reservation, returning everything still held to stock.

        `landed_at` is when the airframes came home from a sortie, or None if
        they never left the ground. Airframes that flew are not ready until
        the squadron's turnaround has passed; unflown ones, and unspent
        ordnance, go straight back, because nothing has to be done to them.
        A turnaround of zero makes them ready the instant they land.
        """
        res = self.open_reservations.pop(reservation_id, None)
        if res is None:
            raise UnknownReservation(reservation_id)
        ready_at = None if landed_at is None else landed_at + self.sortie_rate.turnaround
        if res.airframes > 0 and ready_at is not None and ready_at > landed_at:
            self.turning.append(Turnaround(airframes=res.airframes, ready_at=ready_at))
        else:
            self.airframes_available += res.airframes
        self.munitions_available[res.munition] = (
            self.munitions_available.get(res.munition, 0) + res.rounds
        )
        self.check_invariant()

    def mature(self, now: float) -> int:
        """Make ready every airframe whose turnaround is over by `now`.

        Returns how many. In landing order, so the books move the same way on
        every replay.
        """
        ready = [batch for batch in self.turning if batch.ready_at <= now]
        if not ready:
            return 0
        self.turning = [batch for batch in self.turning if batch.ready_at > now]
        count = sum(batch.airframes for batch in ready)
        self.airframes_available += count
        self.check_invariant()
        return count

    def count_sorties(self, day: int, airframes: int) -> None:
        """Charge `airframes` aircraft sorties to the daily limit for `day`."""
        self.sorties_by_day[day] = self.sorties_by_day.get(day, 0) + airframes

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
            "sortie_rate": self.sortie_rate.to_dict(),
            "turning": [batch.to_dict() for batch in self.turning],
            # JSON keys are strings; written in day order for a diffable save.
            "sorties_by_day": {
                str(day): count for day, count in sorted(self.sorties_by_day.items())
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
            sortie_rate=SortieRate.from_dict(raw["sortie_rate"]),
            turning=[Turnaround.from_dict(b) for b in raw["turning"]],
            sorties_by_day={
                int(day): int(count) for day, count in raw["sorties_by_day"].items()
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

    Every squadron here is UNCONSTRAINED: no turnaround, no daily limit, any
    hour. The slice is the fixture the tests and the recorded wars were
    written against, and it goes through the same readiness code as Syria
    with values that constrain nothing (docs/design.md, "Sortie rate").

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
            sortie_rate=UNCONSTRAINED,
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
            sortie_rate=UNCONSTRAINED,
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
            sortie_rate=UNCONSTRAINED,
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
            sortie_rate=UNCONSTRAINED,
        )
    )
    return blue, red


def _squadron(
    squadron_id: str,
    name: str,
    coalition: Coalition,
    airframe: str,
    template: str,
    home_base: str,
    airframes: int,
    munition: str,
    rounds: int,
    sortie_rate: SortieRate,
) -> Squadron:
    return Squadron(
        id=squadron_id,
        name=name,
        coalition=coalition,
        airframe=airframe,
        template=template,
        home_base=home_base,
        airframes_total=airframes,
        airframes_available=airframes,
        munitions_total={munition: rounds},
        munitions_available={munition: rounds},
        sortie_rate=sortie_rate,
    )


# Sortie-generation figures for the Syria map's types, from published
# sources and labelled placeholders, as the threat model's radii are: none was
# chosen for the war it gives, and the war was measured only after they were
# set (docs/design.md, "Sortie rate", has the result).

#: An F-16's ground time between sorties: 45 minutes, the maximum allotted
#: for an F-16 integrated combat turn -- refuel and rearm -- in Air National
#: Guard practice (DVIDS, "F-16 Integrated Combat Turns enable ACE at
#: Northern Strike 24-2", 180th Fighter Wing, 20 August 2024). A routine
#: turn with no fault to fix; the maintenance a sustained campaign also
#: needs is in the daily rate below, not here.
F16_TURNAROUND = 45.0 * 60.0

#: No published Su-24M turnaround figure was found, so red's is the F-16's,
#: for the reason every site type carries the SA-6's kill probability: a
#: better guess for one side than the other would decide the war by content
#: nobody has measured.
SU24M_TURNAROUND = F16_TURNAROUND

#: Sustained F-16 sorties a day per airframe: 1.35, the F-16's average over
#: the 43 days of Desert Storm, "the highest use rate of any aircraft in
#: theater" (Cordesman and Wagner, *The Lessons of Modern War, Volume IV:
#: The Gulf War*, CSIS, 1994, chapter 7). An achieved wartime average, which
#: is what a planner sizes a sustained campaign by, rather than a surge.
F16_SORTIES_PER_DAY = 1.35

#: Sustained Su-24M sorties a day per airframe: about 1.27, the same
#: statistic for the Russian strike group's first weeks in Syria, derived
#: from Alexander Yermakov, "Russian Aces in Syrian Skies" (Russian
#: International Affairs Council, 23 October 2015): "strike aircraft have
#: made 669 sorties over two and a half weeks", flown by twelve Su-24Ms,
#: twelve Su-25SMs and six Su-34s, so 669 / (17.5 days x 30 aircraft). A
#: mixed group, of which the Su-24M flew about half the sorties; no
#: Su-24M-only figure was found. The same kind of figure as the F-16's -- an
#: achieved combat average over weeks -- so the two sides' limits differ by
#: what was measured, not by a choice of statistic.
SU24M_SORTIES_PER_DAY = 669.0 / (17.5 * 30.0)

#: Each Syria type's sortie rate. Day-only on both sides: the daylight rule
#: is the map's planning rule, not a claim about the jets (both types have
#: night attack kit), until night-capable squadrons exist (the TODO(seam) on
#: SortieRate.day_only).
SYRIA_SORTIE_RATES: dict[str, SortieRate] = {
    "F-16C_50": SortieRate(
        turnaround=F16_TURNAROUND, sorties_per_day=F16_SORTIES_PER_DAY, day_only=True
    ),
    "Su-24M": SortieRate(
        turnaround=SU24M_TURNAROUND, sorties_per_day=SU24M_SORTIES_PER_DAY, day_only=True
    ),
}


#: (base, strike airframes, bombs, SEAD airframes, anti-radiation missiles)
#: for each side's fields in `theater.build_syria_theater`.
#:
#: Each side gets the same totals -- 44 strike airframes, 360 bombs, 20 SEAD
#: airframes, 128 missiles -- the same refusal to tilt the war by unmeasured
#: content as build_slice_oob's. How a side's totals are spread over its
#: fields follows which targets each field is nearest, which the map does
#: not make symmetric: blue's two fields share red's seven targets five to
#: two, red's three share blue's seven three, three and one.
#:
#: Sized so that targets and attrition decide the war, not stock: there is
#: no resupply (out of scope). A sortie is a two-ship dropping four bombs, so
#: 360 bombs are 90 strike sorties, where the 104 target units a side need
#: about 60 at the paper Pk and the rest is what the defences and overkill
#: cost. In a batch of offline wars no side ran out, though in the longest a
#: single field did and its targets passed to the next nearest, as they
#: should. Invented numbers, like build_slice_oob's.
SYRIA_SQUADRON_SIZES: dict[str, tuple[tuple[str, int, int, int, int], ...]] = {
    "blue": (
        ("hatay", 24, 240, 10, 64),
        ("gaziantep", 20, 120, 10, 64),
    ),
    "red": (
        ("bassel_al_assad", 20, 200, 10, 64),
        ("kuweires", 16, 120, 10, 64),
        ("abu_al_duhur", 8, 40, 0, 0),
    ),
}

_SYRIA_BASE_NAMES: dict[str, str] = {
    "hatay": "Hatay",
    "gaziantep": "Gaziantep",
    "bassel_al_assad": "Bassel al-Assad",
    "abu_al_duhur": "Abu al-Duhur",
    "kuweires": "Kuweires",
}

#: Airframe, strike template, strike store, SEAD template, anti-radiation
#: store, by side: the slice's types, whose client templates already exist.
_SYRIA_TYPES: dict[str, tuple[str, str, str, str, str]] = {
    "blue": ("F-16C_50", "F-16C_strike_jdam", "GBU-38", "F-16C_sead_harm", "AGM-88C"),
    "red": ("Su-24M", "Su-24M_strike_fab", "FAB-500", "Su-24M_sead_kh58", "Kh-58U"),
}


def build_syria_oob() -> tuple[SideInventory, SideInventory]:
    """Strike and SEAD squadrons at each side's fields, for `build_syria_theater`.

    A strike squadron and a SEAD squadron at each of blue's two fields and
    red's two main ones, and a strike detachment alone at Abu al-Duhur. The
    strike squadron is declared first at each base, as build_slice_oob
    requires. A SEAD element flies only from its strike's own base
    (docs/design.md, section 5), so a raid from the detachment goes in
    unescorted; the one target it is nearest has no defences on it.
    """
    inventories: dict[str, SideInventory] = {}
    for coalition in ("blue", "red"):
        airframe, strike_tmpl, bomb, sead_tmpl, arm = _SYRIA_TYPES[coalition]
        inventory = SideInventory(coalition=coalition)
        for base, strike_n, bombs, sead_n, arms in SYRIA_SQUADRON_SIZES[coalition]:
            label = _SYRIA_BASE_NAMES[base]
            inventory.add(
                _squadron(
                    f"{coalition}_{base}_strike",
                    f"{label} {airframe} strike squadron",
                    coalition,
                    airframe,
                    strike_tmpl,
                    base,
                    strike_n,
                    bomb,
                    bombs,
                    SYRIA_SORTIE_RATES[airframe],
                )
            )
            if sead_n > 0:
                inventory.add(
                    _squadron(
                        f"{coalition}_{base}_sead",
                        f"{label} {airframe} SEAD squadron",
                        coalition,
                        airframe,
                        sead_tmpl,
                        base,
                        sead_n,
                        arm,
                        arms,
                        SYRIA_SORTIE_RATES[airframe],
                    )
                )
        inventories[coalition] = inventory
    return inventories["blue"], inventories["red"]
