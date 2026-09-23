"""Reconciliation: turning a lossy simulation into a campaign that adds up.

This is the load-bearing module. Everything else in the slice is arrangement;
this is the part that decides whether the campaign is still true after eight
hours of DCS dropping events on the floor.

The rule, restated because it is easy to erode by accident:

    `state` snapshots are the only thing that may record a loss.
    `event` frames supply attribution and nothing else.

Concretely, :meth:`AttritionTracker.ingest` is the only method that appends to
the loss ledger, and :meth:`AttritionTracker.note_event` touches nothing but
the attribution hint queues. Delete every event frame from a campaign log and
the ledger comes out the same length, with the same entries, differing only in
the `attribution` field -- which becomes ``"unknown"``. A loss is never
dropped for want of an explanation.

Three things can be true of a group between one snapshot and the next, and the
tracker must tell them apart, because they mean different things to the war:

  `attrited`   fewer units than last time, still alive. Partial loss.
  `destroyed`  the snapshot says alive=false, or units=0. Total loss.
  `vanished`   the group was instantiated and should have been in the report,
               and was not there at all. DCS ate it. Every remaining unit is a
               loss, attributed to whatever hints happen to be queued and to
               "unknown" otherwise.

`vanished` is the case an event-counting attrition model silently loses, and
the reason it drifts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from campaign.protocol import (
    DEFAULT_STATE_PERIOD,
    Event,
    StateReport,
    spawn_id_of,
)

#: What a loss says when no event ever explained it. Recorded, never dropped.
ATTRIBUTION_UNKNOWN = "unknown"

CAUSE_ATTRITED = "attrited"
CAUSE_DESTROYED = "destroyed"
CAUSE_VANISHED = "vanished"
#: Killed on paper, outside the bubble, by campaign.resolver. Never produced by
#: `ingest`: a snapshot and the resolver are mutually exclusive authorities over
#: any one entity, so a loss carrying this cause was never observed by DCS.
CAUSE_UNOBSERVED = "unobserved"

#: Attribution for an unobserved loss. Not event-derived -- there was no event
#: and no observer -- but it occupies the same field, and says so plainly rather
#: than claiming the `unknown` of a loss whose event merely went missing.
ATTRIBUTION_UNOBSERVED = "unobserved"

KIND_FLIGHT = "flight"
KIND_TARGET = "target"

#: Event kinds whose *target* is the thing that died.
_VICTIM_IS_TARGET = frozenset({"kill", "hit"})

#: Event kinds whose *initiator* is the thing that died.
_VICTIM_IS_INITIATOR = frozenset({"dead", "crash", "eject", "pilot_dead", "unit_lost"})


@dataclass(frozen=True)
class Attribution:
    """Who did it and with what. Best effort, and frequently absent."""

    kind: str
    initiator: str | None = None
    weapon: str | None = None
    t: float = 0.0

    def render(self) -> str:
        parts = [self.kind]
        if self.initiator:
            parts.append(self.initiator)
        if self.weapon:
            parts.append(self.weapon)
        return "/".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "initiator": self.initiator,
            "weapon": self.weapon,
            "t": self.t,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Attribution:
        return cls(
            kind=raw["kind"],
            initiator=raw.get("initiator"),
            weapon=raw.get("weapon"),
            t=float(raw.get("t", 0.0)),
        )


@dataclass(frozen=True)
class LossRecord:
    """Exactly one unit, gone.

    One record per unit rather than a count, so that a two-ship losing both
    aircraft to different shooters keeps both stories. Every field but
    `attribution` comes from a state snapshot; `attribution` is the only place
    an event is allowed to reach.
    """

    t: float
    spawn_id: str
    entity_id: str
    entity_kind: str
    coalition: str
    cause: str
    attribution: str = ATTRIBUTION_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "spawn_id": self.spawn_id,
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "coalition": self.coalition,
            "cause": self.cause,
            "attribution": self.attribution,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LossRecord:
        return cls(
            t=float(raw["t"]),
            spawn_id=raw["spawn_id"],
            entity_id=raw["entity_id"],
            entity_kind=raw["entity_kind"],
            coalition=raw["coalition"],
            cause=raw["cause"],
            attribution=raw["attribution"],
        )


@dataclass
class TrackedGroup:
    """The engine's belief about one owned entity, and how it is verified."""

    spawn_id: str
    entity_id: str
    entity_kind: str
    coalition: str
    units_initial: int
    units_alive: int
    #: True once the client has acknowledged the spawn. Only then does the
    #: group's absence from a snapshot mean anything.
    instantiated: bool = False
    #: Report time from which absence may be read as a vanish. Guards against
    #: calling a group missing on a snapshot that was generated before the
    #: client had finished creating it.
    expect_from: float | None = None
    ever_seen: bool = False
    resolved: bool = False

    @property
    def expects_snapshot(self) -> bool:
        return self.instantiated and not self.resolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "spawn_id": self.spawn_id,
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "coalition": self.coalition,
            "units_initial": self.units_initial,
            "units_alive": self.units_alive,
            "instantiated": self.instantiated,
            "expect_from": self.expect_from,
            "ever_seen": self.ever_seen,
            "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrackedGroup:
        return cls(
            spawn_id=raw["spawn_id"],
            entity_id=raw["entity_id"],
            entity_kind=raw["entity_kind"],
            coalition=raw["coalition"],
            units_initial=int(raw["units_initial"]),
            units_alive=int(raw["units_alive"]),
            instantiated=bool(raw["instantiated"]),
            expect_from=(
                None if raw["expect_from"] is None else float(raw["expect_from"])
            ),
            ever_seen=bool(raw["ever_seen"]),
            resolved=bool(raw["resolved"]),
        )


class AttritionTracker:
    """Holds the engine's belief about owned entities and reconciles it.

    Insertion order of `groups` is the iteration order everywhere, so the loss
    ledger from a given log is byte-identical on every replay.
    """

    def __init__(self, vanish_grace: float = DEFAULT_STATE_PERIOD) -> None:
        #: Seconds a group gets between "the client acked the spawn" and "its
        #: absence from a snapshot is a vanish". One state period: a snapshot
        #: in flight when the spawn landed cannot indict the new group.
        self.vanish_grace = vanish_grace
        self.groups: dict[str, TrackedGroup] = {}
        self.losses: list[LossRecord] = []
        #: Event-derived, and event-derived only. Nothing in this dict may
        #: ever change whether a loss happens -- only what it says happened.
        self.hints: dict[str, list[Attribution]] = {}

    # -- lifecycle --------------------------------------------------------

    def track(
        self,
        spawn_id: str,
        *,
        entity_id: str,
        entity_kind: str,
        coalition: str,
        units_initial: int,
        units_alive: int | None = None,
    ) -> TrackedGroup:
        group = TrackedGroup(
            spawn_id=spawn_id,
            entity_id=entity_id,
            entity_kind=entity_kind,
            coalition=coalition,
            units_initial=units_initial,
            units_alive=units_initial if units_alive is None else units_alive,
        )
        self.groups[spawn_id] = group
        return group

    def mark_instantiated(self, spawn_id: str, t: float) -> None:
        """The client acked a spawn: from now on this group owes us snapshots."""
        group = self.groups.get(spawn_id)
        if group is None or group.resolved:
            return
        group.instantiated = True
        group.expect_from = t

    def mark_removed(self, spawn_id: str) -> None:
        """We despawned it, or the client went away. Absence is expected again."""
        group = self.groups.get(spawn_id)
        if group is None:
            return
        group.instantiated = False
        group.expect_from = None

    def detach_all(self) -> None:
        """DCS went away. Nothing is instantiated; the campaign belief stands."""
        for group in self.groups.values():
            group.instantiated = False
            group.expect_from = None

    def forget(self, spawn_id: str) -> None:
        """Stop tracking an entity whose campaign life is over."""
        self.groups.pop(spawn_id, None)
        self.hints.pop(spawn_id, None)

    # -- queries ----------------------------------------------------------

    def units_alive(self, spawn_id: str) -> int:
        group = self.groups.get(spawn_id)
        return 0 if group is None else group.units_alive

    def is_instantiated(self, spawn_id: str) -> bool:
        """Is DCS currently holding this entity?

        The question that decides which authority may record a loss for it: a
        snapshot if DCS has it, the unobserved resolver if it does not.
        """
        group = self.groups.get(spawn_id)
        return group is not None and group.instantiated

    def is_alive(self, spawn_id: str) -> bool:
        group = self.groups.get(spawn_id)
        return group is not None and not group.resolved and group.units_alive > 0

    def losses_for(self, spawn_id: str) -> list[LossRecord]:
        return [loss for loss in self.losses if loss.spawn_id == spawn_id]

    # -- attribution (events) ---------------------------------------------

    def note_event(self, event: Event) -> None:
        """Queue an attribution hint. Records no loss, and never can.

        Safe to call zero times per loss, or ten times: hints are consumed at
        most one per unit lost, and surplus hints are discarded when the group
        is forgotten.
        """
        victim = self._victim_spawn_id(event)
        if victim is None:
            return
        group = self.groups.get(victim)
        if group is None or group.resolved:
            return
        queue = self.hints.setdefault(victim, [])
        # A group cannot lose more units than it has left, so it cannot need
        # more explanations than that either. Bounding the queue keeps a noisy
        # event stream from growing without limit.
        if len(queue) >= group.units_alive:
            return
        queue.append(
            Attribution(
                kind=event.kind,
                initiator=event.initiator,
                weapon=event.weapon,
                t=event.t,
            )
        )

    @staticmethod
    def _victim_spawn_id(event: Event) -> str | None:
        """Which engine-owned entity, if any, this event says something about.

        Unit references that do not carry the owned prefix belong to scenery,
        a client aircraft or another script, and are ignored entirely.
        """
        if event.kind in _VICTIM_IS_TARGET:
            name = event.target
        elif event.kind in _VICTIM_IS_INITIATOR:
            name = event.initiator
        else:
            return None
        if name is None:
            return None
        return spawn_id_of(name)

    def _take_attribution(self, spawn_id: str) -> str:
        queue = self.hints.get(spawn_id)
        if not queue:
            return ATTRIBUTION_UNKNOWN
        return queue.pop(0).render()

    # -- reconciliation (state) -------------------------------------------

    def ingest(self, report: StateReport) -> list[LossRecord]:
        """Reconcile belief against ground truth. The only source of losses.

        Returns the losses this snapshot revealed, in a deterministic order:
        reported groups in report order, then absent groups in tracking order.
        """
        revealed: list[LossRecord] = []
        seen: set[str] = set()

        for snapshot in report.groups:
            group = self.groups.get(snapshot.spawn_id)
            if group is None or group.resolved:
                # Not ours, or already written off. A snapshot cannot resurrect.
                continue
            seen.add(group.spawn_id)
            group.ever_seen = True
            # Deliberately does NOT set `instantiated`. A snapshot generated
            # just before a despawn was acknowledged still lists the group;
            # believing it would re-arm vanish detection on an entity the
            # engine itself removed, and the next report would indict it.
            # Only an ack says a group is live.

            if not snapshot.alive:
                observed = 0
            else:
                # Clamp upward reports. A snapshot claiming more units than the
                # engine issued is a client bug; believing it would manufacture
                # airframes out of nothing, and the inventory would stop adding up.
                observed = max(0, min(snapshot.units, group.units_alive))

            lost = group.units_alive - observed
            if lost > 0:
                cause = CAUSE_DESTROYED if observed == 0 else CAUSE_ATTRITED
                revealed.extend(self._record(group, lost, report.t, cause))
            group.units_alive = observed
            if observed == 0:
                self._resolve(group)

        for group in self.groups.values():
            if group.spawn_id in seen or not group.expects_snapshot:
                continue
            if (
                group.expect_from is None
                or report.t < group.expect_from + self.vanish_grace
            ):
                continue
            if group.units_alive > 0:
                revealed.extend(
                    self._record(group, group.units_alive, report.t, CAUSE_VANISHED)
                )
            group.units_alive = 0
            self._resolve(group)

        return revealed

    def _record(
        self, group: TrackedGroup, count: int, t: float, cause: str
    ) -> list[LossRecord]:
        records = [
            LossRecord(
                t=t,
                spawn_id=group.spawn_id,
                entity_id=group.entity_id,
                entity_kind=group.entity_kind,
                coalition=group.coalition,
                cause=cause,
                attribution=self._take_attribution(group.spawn_id),
            )
            for _ in range(count)
        ]
        self.losses.extend(records)
        return records

    def _resolve(self, group: TrackedGroup) -> None:
        group.resolved = True
        group.instantiated = False
        group.expect_from = None
        self.hints.pop(group.spawn_id, None)

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "vanish_grace": self.vanish_grace,
            "groups": [g.to_dict() for g in self.groups.values()],
            "losses": [loss.to_dict() for loss in self.losses],
            # Separated under its own key so a caller auditing the
            # reconciliation rule can strip everything event-derived by name.
            "attribution_hints": {
                k: [a.to_dict() for a in v] for k, v in self.hints.items() if v
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AttritionTracker:
        tracker = cls(vanish_grace=float(raw["vanish_grace"]))
        for entry in raw["groups"]:
            group = TrackedGroup.from_dict(entry)
            tracker.groups[group.spawn_id] = group
        tracker.losses = [LossRecord.from_dict(e) for e in raw["losses"]]
        tracker.hints = {
            k: [Attribution.from_dict(a) for a in v]
            for k, v in raw.get("attribution_hints", {}).items()
        }
        return tracker
