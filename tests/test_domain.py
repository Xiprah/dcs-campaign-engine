"""Tests for the campaign domain.

These are written to catch the invariants breaking, not to describe the
implementation back to itself. The four that matter:

  * reconciliation -- the campaign lands in the same place with every event
    frame delivered and with every event frame thrown away;
  * bubble hysteresis -- an observer loitering on the boundary cannot make the
    client spawn and despawn the same group over and over;
  * inventory conservation -- no airframe and no round is ever created or
    destroyed except by an explicit loss or expenditure;
  * save/load -- a reloaded campaign continues the same war, not a similar one.

The reconciliation and save/load tests drive a closed loop through a small
stand-in for the mission client, because an open-loop script could not catch
the engine issuing different frames.
"""

from __future__ import annotations

import copy
import json
import math
import random
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from campaign.api import CampaignEngine
from campaign.attrition import (
    ATTRIBUTION_UNKNOWN,
    CAUSE_ATTRITED,
    CAUSE_DESTROYED,
    CAUSE_VANISHED,
    KIND_FLIGHT,
    KIND_TARGET,
    AttritionTracker,
)
from campaign.bubble import BubbleConfigError, bubble_delta, resolve_bubble
from campaign.campaign import Campaign
from campaign.oob import (
    InsufficientInventory,
    Squadron,
    UnknownReservation,
    build_slice_oob,
)
from campaign.planner import (
    CRUISE_ALTITUDE,
    ENROUTE,
    OPEN_STATES,
    build_package,
    package_position,
    select_target,
)
from campaign.protocol import (
    PROTOCOL_VERSION,
    Ack,
    Despawn,
    Event,
    GroupSnapshot,
    Hello,
    Observer,
    ObserverReport,
    Spawn,
    StateReport,
    encode,
    group_name,
)
from campaign.theater import (
    build_slice_theater,
    enemy_of,
    ground_distance,
    slant_distance,
)

STEP = 5
OBSERVER_PERIOD = 5
STATE_PERIOD = 30


# ---------------------------------------------------------------------------
# A stand-in for the mission client
# ---------------------------------------------------------------------------


@dataclass
class Damage:
    """One scripted thing DCS does to an engine-owned group.

    `event` is the event kind DCS reports, or None for the case the whole
    reconciliation rule exists to survive: a loss nothing ever explained.
    """

    t: int
    category: str
    remove: int = 0
    vanish: bool = False
    event: str | None = None
    initiator: str | None = None
    weapon: str | None = None


@dataclass
class _LiveGroup:
    spawn_id: str
    category: str
    units: int
    units_initial: int
    alive: bool = True


@dataclass
class FakeDCS:
    """Just enough mission client to close the loop, and no policy of its own."""

    campaign: Campaign
    deliver_events: bool = True
    damages: list[Damage] = field(default_factory=list)
    groups: dict[str, _LiveGroup] = field(default_factory=dict)
    downlink: list[object] = field(default_factory=list)
    applied: set[int] = field(default_factory=set)
    uplink_seq: int = 0
    t: float = 0.0

    def assert_script_ran(self, start: int, end: int) -> None:
        """A damage the driver silently skipped would make the test vacuous."""
        missed = [
            d
            for d in self.damages
            if start <= d.t <= end and id(d) not in self.applied
        ]
        assert not missed, (
            f"scripted damage never applied: {missed}; damage times must land on "
            f"a state-report boundary (multiples of {STATE_PERIOD}s)"
        )

    def _seq(self) -> int:
        self.uplink_seq += 1
        return self.uplink_seq

    # -- downlink handling ------------------------------------------------

    def pump(self, frames: list[object]) -> None:
        for frame in frames:
            self.downlink.append(frame)
            if isinstance(frame, Spawn):
                self._on_spawn(frame)
            elif isinstance(frame, Despawn):
                self._on_despawn(frame)

    def _on_spawn(self, frame: Spawn) -> None:
        assert frame.spawn_id not in self.groups, (
            f"engine spawned {frame.spawn_id} twice; the client would have to "
            f"reject it and the campaign would lose the entity"
        )
        units = int(frame.tasking.get("units", 1))
        self.groups[frame.spawn_id] = _LiveGroup(
            spawn_id=frame.spawn_id,
            category=frame.category,
            units=units,
            units_initial=units,
        )
        self.pump(
            self.campaign.on_ack(
                Ack(seq=self._seq(), t=self.t, ref=frame.ref, ok=True)
            )
        )

    def _on_despawn(self, frame: Despawn) -> None:
        self.groups.pop(frame.spawn_id, None)
        self.pump(
            self.campaign.on_ack(
                Ack(seq=self._seq(), t=self.t, ref=frame.ref, ok=True)
            )
        )

    # -- uplink -----------------------------------------------------------

    def connect(self, t: float) -> None:
        self.t = t
        self.pump(
            self.campaign.on_hello(
                Hello(
                    seq=self._seq(),
                    t=t,
                    protocol=PROTOCOL_VERSION,
                    theater="Syria",
                )
            )
        )

    def observers(self, t: float, positions: list[tuple[float, float, float]]) -> None:
        self.t = t
        self.pump(
            self.campaign.on_observer(
                ObserverReport(
                    seq=self._seq(),
                    t=t,
                    observers=[
                        Observer(id=f"player:{i}", pos=p)
                        for i, p in enumerate(positions)
                    ],
                )
            )
        )

    def state(self, t: int) -> None:
        self.t = float(t)
        self._apply_damage(t)
        self.pump(
            self.campaign.on_state(
                StateReport(
                    seq=self._seq(),
                    t=float(t),
                    groups=[
                        GroupSnapshot(
                            spawn_id=g.spawn_id,
                            alive=g.alive,
                            units=g.units,
                            units_initial=g.units_initial,
                        )
                        for g in self.groups.values()
                    ],
                )
            )
        )

    def _group_of(self, category: str) -> _LiveGroup | None:
        for group in self.groups.values():
            if group.category == category:
                return group
        return None

    def _apply_damage(self, t: int) -> None:
        for damage in self.damages:
            if damage.t != t:
                continue
            self.applied.add(id(damage))
            group = self._group_of(damage.category)
            assert group is not None, (
                f"scripted damage at t={t} has no {damage.category} instantiated; "
                f"the scenario drifted and the test would be measuring nothing"
            )
            if damage.vanish:
                # DCS simply stops reporting it. No event, no snapshot, no
                # explanation -- the case an event-counting model loses.
                del self.groups[group.spawn_id]
                continue
            removed = min(damage.remove, group.units)
            group.units -= removed
            if group.units == 0:
                group.alive = False
            if damage.event is None:
                continue
            for _ in range(removed):
                self._emit_event(damage, group)

    def _emit_event(self, damage: Damage, group: _LiveGroup) -> None:
        if not self.deliver_events:
            return
        self.pump(
            self.campaign.on_event(
                Event(
                    seq=self._seq(),
                    t=self.t,
                    kind=damage.event or "kill",
                    initiator=damage.initiator,
                    target=group_name(group.spawn_id),
                    weapon=damage.weapon,
                )
            )
        )


class SilentDespawnDCS(FakeDCS):
    """A client that obeys a despawn but whose ack never arrives.

    An ordinary failure: a socket dropping between the despawn and the ack, or
    a Lua client that destroyed the group and then failed before answering. The
    engine must not need the ack to know it removed the entity itself.
    """

    def _on_despawn(self, frame: Despawn) -> None:
        self.groups.pop(frame.spawn_id, None)


def fork_client(dcs: FakeDCS, campaign: Campaign) -> FakeDCS:
    """A second client holding the same picture of DCS, bound to another campaign.

    Needed by the save/load test: a reloaded campaign must be handed the world
    as it actually is, not an empty one, or the comparison measures a restart
    rather than a continuation.
    """
    return FakeDCS(
        campaign=campaign,
        deliver_events=dcs.deliver_events,
        damages=list(dcs.damages),
        groups=copy.deepcopy(dcs.groups),
        applied=set(dcs.applied),
        uplink_seq=dcs.uplink_seq,
        t=dcs.t,
    )


def drive(
    campaign: Campaign,
    *,
    observer_positions: list[tuple[float, float, float]],
    damages: list[Damage],
    deliver_events: bool,
    start: int,
    duration: int,
    hook=None,
    dcs: FakeDCS | None = None,
) -> FakeDCS:
    """Run the closed loop from `start` to `start + duration` seconds."""
    if dcs is None:
        dcs = FakeDCS(
            campaign=campaign, deliver_events=deliver_events, damages=list(damages)
        )
        if start == 0:
            dcs.connect(0.0)
    else:
        dcs.campaign = campaign
    for t in range(start, start + duration + 1, STEP):
        dcs.t = float(t)
        dcs.pump(campaign.tick(float(t)))
        if t % OBSERVER_PERIOD == 0:
            dcs.observers(float(t), observer_positions)
        if t % STATE_PERIOD == 0:
            dcs.state(t)
        if hook is not None:
            hook(campaign, t)
    dcs.assert_script_ran(start, start + duration)
    return dcs


# ---------------------------------------------------------------------------
# The scenario both reconciliation runs share
# ---------------------------------------------------------------------------

#: Observer parked on the target, so the depot is always instantiated and the
#: strike flight enters and leaves the bubble on its own paper track.
THEATER = build_slice_theater()
DEPOT = THEATER.targets["latakia_fuel_depot"]
OBSERVER_AT_TARGET = [(DEPOT.pos[0], 100.0, DEPOT.pos[2])]

#: Hundreds of kilometres from anything in the slice: whatever was in the
#: bubble leaves it, and nothing re-enters.
OBSERVER_FAR_AWAY = [(DEPOT.pos[0] + 300_000.0, 100.0, DEPOT.pos[2] + 300_000.0)]

SCENARIO: list[Damage] = [
    # Bombs on target, fully explained by events.
    Damage(t=1500, category="structure", remove=2, event="kill",
           initiator="cmp_0002", weapon="GBU-38"),
    # More damage, and DCS reports nothing at all about it.
    Damage(t=1560, category="structure", remove=1),
    # A jet shot down, explained.
    Damage(t=1800, category="plane", remove=1, event="kill",
           initiator="sam:kub_01", weapon="9M38"),
    # The rest of the flight simply stops being reported. No event, no dead
    # snapshot, nothing. The engine must still write the loss down.
    Damage(t=1920, category="plane", vanish=True),
]

SCENARIO_DURATION = 2700

def fingerprint(campaign: Campaign) -> dict:
    """Campaign state with every event-derived field removed, by name."""
    raw = copy.deepcopy(campaign.to_dict())
    raw["tracker"].pop("attribution_hints", None)
    for loss in raw["tracker"]["losses"]:
        loss.pop("attribution", None)
    return raw


def attributions(campaign: Campaign) -> list[str]:
    return [loss.attribution for loss in campaign.tracker.losses]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class TestGeometry(unittest.TestCase):
    def test_ground_distance_ignores_altitude(self):
        """y is altitude in a DCS Vec3. Treating it as a ground axis is the bug."""
        deck = (0.0, 0.0, 0.0)
        overhead = (0.0, 9_000.0, 0.0)
        self.assertEqual(ground_distance(deck, overhead), 0.0)
        self.assertAlmostEqual(slant_distance(deck, overhead), 9_000.0)

        a = (1_000.0, 0.0, 0.0)
        b = (0.0, 12_000.0, 0.0)
        self.assertAlmostEqual(ground_distance(a, b), 1_000.0)
        self.assertGreater(slant_distance(a, b), ground_distance(a, b))

    def test_ground_distance_uses_x_and_z(self):
        self.assertAlmostEqual(
            ground_distance((0.0, 0.0, 0.0), (3_000.0, 500.0, 4_000.0)), 5_000.0
        )


# ---------------------------------------------------------------------------
# Inventory conservation
# ---------------------------------------------------------------------------


def _fresh_squadron() -> Squadron:
    blue, _ = build_slice_oob()
    return blue.squadron("vfa_incirlik_f16")


class TestInventoryConservation(unittest.TestCase):
    def assert_conserved(self, squadron: Squadron) -> None:
        squadron.check_invariant()
        self.assertEqual(squadron.airframes_total, 12)
        self.assertEqual(squadron.munitions_total, {"GBU-38": 48})

    def test_reserve_then_release_returns_everything(self):
        sqn = _fresh_squadron()
        sqn.reserve("p1", 2, "GBU-38", 4)
        self.assertEqual(sqn.airframes_available, 10)
        self.assertEqual(sqn.munitions_available["GBU-38"], 44)
        self.assert_conserved(sqn)

        sqn.release("p1")
        self.assertEqual(sqn.airframes_available, 12)
        self.assertEqual(sqn.munitions_available["GBU-38"], 48)
        self.assertEqual(sqn.airframes_lost, 0)
        self.assert_conserved(sqn)

    def test_losses_and_expenditure_are_the_only_ways_stock_leaves(self):
        sqn = _fresh_squadron()
        sqn.reserve("p1", 2, "GBU-38", 4)
        sqn.debit_airframes("p1", 1)
        sqn.debit_munitions("p1", 2, lost=True)
        self.assert_conserved(sqn)
        sqn.debit_munitions("p1", 2, lost=False)
        sqn.release("p1")

        self.assertEqual(sqn.airframes_available, 11)
        self.assertEqual(sqn.airframes_lost, 1)
        self.assertEqual(sqn.munitions_available["GBU-38"], 44)
        self.assertEqual(sqn.munitions_lost["GBU-38"], 2)
        self.assertEqual(sqn.munitions_expended["GBU-38"], 2)
        self.assert_conserved(sqn)

    def test_overdebit_cannot_invent_stock_to_destroy(self):
        sqn = _fresh_squadron()
        sqn.reserve("p1", 2, "GBU-38", 4)
        self.assertEqual(sqn.debit_airframes("p1", 99), 2)
        self.assertEqual(sqn.debit_munitions("p1", 99, lost=False), 4)
        sqn.release("p1")
        self.assertEqual(sqn.airframes_lost, 2)
        self.assertEqual(sqn.airframes_available, 10)
        self.assert_conserved(sqn)

    def test_a_failed_reservation_changes_nothing(self):
        sqn = _fresh_squadron()
        with self.assertRaises(InsufficientInventory):
            sqn.reserve("p1", 99, "GBU-38", 4)
        self.assertEqual(sqn.airframes_available, 12)
        self.assertEqual(sqn.munitions_available["GBU-38"], 48)
        self.assertEqual(sqn.open_reservations, {})
        self.assert_conserved(sqn)

    def test_double_release_is_refused_rather_than_duplicating_stock(self):
        sqn = _fresh_squadron()
        sqn.reserve("p1", 2, "GBU-38", 4)
        sqn.release("p1")
        with self.assertRaises(UnknownReservation):
            sqn.release("p1")
        self.assert_conserved(sqn)

    def test_campaign_conserves_inventory_at_every_step(self):
        """The campaign-level version: check after every single frame batch."""
        campaign = Campaign()
        seen_reserved = []

        def check(camp: Campaign, _t: int) -> None:
            sqn = camp.inventories["blue"].squadron("vfa_incirlik_f16")
            self.assert_conserved(sqn)
            seen_reserved.append(sqn.airframes_reserved)

        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=SCENARIO,
            deliver_events=True,
            start=0,
            duration=SCENARIO_DURATION,
            hook=check,
        )

        sqn = campaign.inventories["blue"].squadron("vfa_incirlik_f16")
        flight_losses = [
            loss
            for loss in campaign.tracker.losses
            if loss.entity_kind == KIND_FLIGHT
        ]
        self.assertEqual(
            sqn.airframes_lost,
            len(flight_losses),
            "airframes lost must equal the losses the ledger actually recorded",
        )
        self.assertGreater(sqn.airframes_lost, 0, "scenario destroyed no aircraft")
        self.assertGreater(max(seen_reserved), 0, "no package was ever airborne")
        self.assert_conserved(sqn)

    def test_scrubbed_package_does_not_leak_inventory(self):
        """A client that rejects a spawn must not cost the campaign a jet."""
        campaign = Campaign()
        sqn = campaign.inventories["blue"].squadron("vfa_incirlik_f16")
        campaign.on_hello(
            Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        campaign.tick(0.0)
        package = next(iter(campaign.packages.values()))
        self.assertEqual(sqn.airframes_available, 10)

        # Fly on until the bubble puts the flight into DCS, then refuse it.
        spawn = None
        for t in range(0, 2000, STEP):
            frames = campaign.tick(float(t))
            frames += campaign.on_observer(
                ObserverReport(
                    seq=t + 2,
                    t=float(t),
                    observers=[Observer(id="p", pos=OBSERVER_AT_TARGET[0])],
                )
            )
            spawn = next(
                (
                    f
                    for f in frames
                    if isinstance(f, Spawn) and f.spawn_id == package.spawn_id
                ),
                None,
            )
            if spawn is not None:
                break
        self.assertIsNotNone(spawn, "the flight never entered the bubble")

        campaign.on_ack(
            Ack(
                seq=9_000,
                t=float(spawn.t),
                ref=spawn.ref,
                ok=False,
                error="unknown template: F-16C_strike_jdam",
            )
        )

        self.assertEqual(package.state, "aborted")
        self.assertEqual(sqn.airframes_available, 12)
        self.assertEqual(sqn.munitions_available["GBU-38"], 48)
        self.assert_conserved(sqn)


# ---------------------------------------------------------------------------
# Bubble hysteresis
# ---------------------------------------------------------------------------


SPAWN_R = 75_000.0
DESPAWN_R = 95_000.0


def _naive_membership(distance: float, radius: float) -> bool:
    """What a single-radius bubble would do. Here to prove the test has teeth."""
    return distance <= radius


class TestBubbleHysteresis(unittest.TestCase):
    def test_loitering_exactly_on_the_boundary_does_not_thrash(self):
        """The whole reason hysteresis exists.

        An observer sitting on the spawn radius and drifting a few metres each
        way must produce exactly one spawn and no despawn. A single-radius
        bubble produces a transition on nearly every frame, which in DCS means
        the group is destroyed and recreated over and over.
        """
        entity = (0.0, 0.0, 0.0)
        # Distances straddling the spawn radius by a metre or two, as an
        # aircraft holding a CAP station on the boundary would.
        offsets = [0.0, +2.0, -1.0, +1.0, -2.0, 0.0, +3.0, -3.0]
        distances = [SPAWN_R + off for off in offsets] * 25

        live: frozenset[str] = frozenset()
        hysteretic = []
        for distance in distances:
            observer = (distance, 3_000.0, 0.0)
            live = resolve_bubble(
                [observer],
                {"flight": entity},
                live,
                spawn_radius=SPAWN_R,
                despawn_radius=DESPAWN_R,
            )
            hysteretic.append("flight" in live)

        naive = [_naive_membership(d, SPAWN_R) for d in distances]

        self.assertTrue(hysteretic[0], "entity on the spawn radius never spawned")
        self.assertTrue(
            all(hysteretic), "hysteretic bubble despawned a loitering entity"
        )
        self.assertEqual(_transitions(hysteretic), 0)
        self.assertGreater(
            _transitions(naive),
            10,
            "the loiter pattern does not actually thrash a single-radius bubble, "
            "so this test proves nothing -- fix the pattern",
        )

    def test_entity_must_re_enter_the_spawn_radius_to_come_back(self):
        """Between the radii, membership depends on what it already was."""
        entity = (0.0, 0.0, 0.0)
        between = (SPAWN_R + 5_000.0, 0.0, 0.0)
        far = (DESPAWN_R + 1.0, 0.0, 0.0)
        close = (SPAWN_R - 1.0, 0.0, 0.0)

        live = resolve_bubble(
            [between], {"e": entity}, frozenset(),
            spawn_radius=SPAWN_R, despawn_radius=DESPAWN_R,
        )
        self.assertEqual(live, frozenset(), "spawned outside the spawn radius")

        live = resolve_bubble(
            [close], {"e": entity}, live,
            spawn_radius=SPAWN_R, despawn_radius=DESPAWN_R,
        )
        self.assertEqual(live, frozenset({"e"}))

        live = resolve_bubble(
            [between], {"e": entity}, live,
            spawn_radius=SPAWN_R, despawn_radius=DESPAWN_R,
        )
        self.assertEqual(live, frozenset({"e"}), "despawned inside the despawn radius")

        live = resolve_bubble(
            [far], {"e": entity}, live,
            spawn_radius=SPAWN_R, despawn_radius=DESPAWN_R,
        )
        self.assertEqual(live, frozenset(), "survived beyond the despawn radius")

    def test_altitude_does_not_move_the_bubble(self):
        entity = (SPAWN_R - 10.0, 0.0, 0.0)
        for altitude in (0.0, 10_000.0, 30_000.0):
            live = resolve_bubble(
                [(0.0, altitude, 0.0)], {"e": entity}, frozenset(),
                spawn_radius=SPAWN_R, despawn_radius=DESPAWN_R,
            )
            self.assertEqual(live, frozenset({"e"}), f"altitude {altitude} moved it")

    def test_no_observers_means_nothing_is_instantiated(self):
        live = resolve_bubble(
            [], {"e": (0.0, 0.0, 0.0)}, frozenset({"e"}),
            spawn_radius=SPAWN_R, despawn_radius=DESPAWN_R,
        )
        self.assertEqual(live, frozenset())

    def test_equal_radii_are_refused(self):
        with self.assertRaises(BubbleConfigError):
            resolve_bubble(
                [(0.0, 0.0, 0.0)], {}, frozenset(),
                spawn_radius=SPAWN_R, despawn_radius=SPAWN_R,
            )
        with self.assertRaises(BubbleConfigError):
            resolve_bubble(
                [(0.0, 0.0, 0.0)], {}, frozenset(),
                spawn_radius=SPAWN_R, despawn_radius=SPAWN_R - 1.0,
            )

    def test_delta_is_sorted_for_deterministic_frame_order(self):
        to_spawn, to_despawn = bubble_delta({"c", "a"}, {"a", "b"})
        self.assertEqual(to_spawn, ["b"])
        self.assertEqual(to_despawn, ["c"])


def _transitions(flags: list[bool]) -> int:
    return sum(1 for a, b in zip(flags, flags[1:]) if a != b)


# ---------------------------------------------------------------------------
# Attrition: the three things a snapshot can mean
# ---------------------------------------------------------------------------


def _tracker_with_flight(units: int = 4) -> AttritionTracker:
    tracker = AttritionTracker(vanish_grace=30.0)
    tracker.track(
        "a91f",
        entity_id="pkg0001",
        entity_kind=KIND_FLIGHT,
        coalition="blue",
        units_initial=units,
    )
    tracker.mark_instantiated("a91f", 0.0)
    return tracker


def _snapshot(t: float, alive: bool, units: int, initial: int = 4) -> StateReport:
    return StateReport(
        seq=1,
        t=t,
        groups=[
            GroupSnapshot(
                spawn_id="a91f", alive=alive, units=units, units_initial=initial
            )
        ],
    )


class TestAttrition(unittest.TestCase):
    def test_partial_attrition_is_not_a_kill(self):
        tracker = _tracker_with_flight()
        losses = tracker.ingest(_snapshot(60.0, True, 3))
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0].cause, CAUSE_ATTRITED)
        self.assertEqual(tracker.units_alive("a91f"), 3)
        self.assertTrue(tracker.is_alive("a91f"))

    def test_destruction_records_every_remaining_unit(self):
        tracker = _tracker_with_flight()
        losses = tracker.ingest(_snapshot(60.0, False, 0))
        self.assertEqual(len(losses), 4)
        self.assertTrue(all(loss.cause == CAUSE_DESTROYED for loss in losses))
        self.assertFalse(tracker.is_alive("a91f"))

    def test_a_group_that_vanishes_with_no_event_is_still_a_loss(self):
        """The case an event-counting attrition model silently loses."""
        tracker = _tracker_with_flight()
        tracker.ingest(_snapshot(30.0, True, 4))
        losses = tracker.ingest(StateReport(seq=2, t=90.0, groups=[]))
        self.assertEqual(len(losses), 4)
        self.assertTrue(all(loss.cause == CAUSE_VANISHED for loss in losses))
        self.assertTrue(
            all(loss.attribution == ATTRIBUTION_UNKNOWN for loss in losses)
        )
        self.assertEqual(tracker.units_alive("a91f"), 0)

    def test_absence_inside_the_grace_window_is_not_a_vanish(self):
        tracker = _tracker_with_flight()
        losses = tracker.ingest(StateReport(seq=2, t=10.0, groups=[]))
        self.assertEqual(losses, [])
        self.assertEqual(tracker.units_alive("a91f"), 4)

    def test_a_removed_group_is_not_accused_of_vanishing(self):
        """We despawned it. Its absence is our own doing, not a loss."""
        tracker = _tracker_with_flight()
        tracker.mark_removed("a91f")
        losses = tracker.ingest(StateReport(seq=2, t=900.0, groups=[]))
        self.assertEqual(losses, [])
        self.assertEqual(tracker.units_alive("a91f"), 4)

    def test_a_stale_census_does_not_re_arm_vanish_detection(self):
        """Only an ack says a group is live. A snapshot may not say it.

        The protocol allows the sequence outright: the engine despawns a flight
        for `left_bubble`, and a census the client had already built arrives a
        moment later still listing it. If `ingest` treated that as evidence the
        group is instantiated, every later report -- correctly omitting a group
        the engine itself removed -- would read as a vanish, and the campaign
        would write off a two-ship that is alive and on its paper track.
        """
        tracker = _tracker_with_flight()
        tracker.ingest(_snapshot(30.0, True, 4))
        tracker.mark_removed("a91f")

        self.assertEqual(tracker.ingest(_snapshot(35.0, True, 4)), [])

        for t in (90.0, 150.0, 900.0):
            self.assertEqual(tracker.ingest(StateReport(seq=3, t=t, groups=[])), [])
        self.assertEqual(tracker.losses, [])
        self.assertEqual(tracker.units_alive("a91f"), 4)
        self.assertTrue(tracker.is_alive("a91f"))

    def test_a_stale_census_may_still_report_a_real_loss(self):
        """Removal stops absence meaning anything. It does not stop truth.

        The guard above must not turn into "ignore snapshots for removed
        groups": a census generated before the despawn is still ground truth
        about what was standing when it was taken.
        """
        tracker = _tracker_with_flight()
        tracker.mark_removed("a91f")
        losses = tracker.ingest(_snapshot(35.0, True, 2))
        self.assertEqual(len(losses), 2)
        self.assertEqual(tracker.units_alive("a91f"), 2)

    def test_a_snapshot_cannot_resurrect_units(self):
        tracker = _tracker_with_flight()
        tracker.ingest(_snapshot(60.0, True, 2))
        losses = tracker.ingest(_snapshot(90.0, True, 4))
        self.assertEqual(losses, [])
        self.assertEqual(tracker.units_alive("a91f"), 2)

    def test_events_alone_record_nothing(self):
        """A thousand kill events with no snapshot must not cost one unit."""
        tracker = _tracker_with_flight()
        for i in range(1_000):
            tracker.note_event(
                Event(
                    seq=i,
                    t=float(i),
                    kind="kill",
                    initiator="sam:kub",
                    target=group_name("a91f"),
                    weapon="9M38",
                )
            )
        self.assertEqual(tracker.losses, [])
        self.assertEqual(tracker.units_alive("a91f"), 4)

    def test_events_attribute_losses_the_snapshot_records(self):
        tracker = _tracker_with_flight()
        tracker.note_event(
            Event(
                seq=1,
                t=50.0,
                kind="kill",
                initiator="sam:kub",
                target=group_name("a91f"),
                weapon="9M38",
            )
        )
        losses = tracker.ingest(_snapshot(60.0, True, 2))
        self.assertEqual(len(losses), 2)
        self.assertIn("9M38", losses[0].attribution)
        self.assertEqual(
            losses[1].attribution,
            ATTRIBUTION_UNKNOWN,
            "a loss with no explanation must still be written down",
        )

    def test_events_about_other_peoples_units_are_ignored(self):
        tracker = _tracker_with_flight()
        tracker.note_event(
            Event(seq=1, t=5.0, kind="kill", target="Ground-1", weapon="AGM-65")
        )
        self.assertEqual(tracker.hints, {})


# ---------------------------------------------------------------------------
# Reconciliation: the rule the whole engine is built around
# ---------------------------------------------------------------------------


class TestReconciliation(unittest.TestCase):
    def _run(self, deliver_events: bool) -> tuple[Campaign, FakeDCS]:
        campaign = Campaign()
        dcs = drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=SCENARIO,
            deliver_events=deliver_events,
            start=0,
            duration=SCENARIO_DURATION,
        )
        return campaign, dcs

    def test_identical_campaign_state_with_and_without_events(self):
        with_events, dcs_with = self._run(deliver_events=True)
        without_events, dcs_without = self._run(deliver_events=False)

        self.assertGreater(
            len(with_events.tracker.losses),
            0,
            "the scenario recorded no losses at all; it proves nothing",
        )
        self.assertEqual(
            len(with_events.tracker.losses),
            len(without_events.tracker.losses),
            "dropping every event frame changed how many losses were recorded",
        )
        self.assertEqual(
            fingerprint(with_events),
            fingerprint(without_events),
            "dropping every event frame changed campaign state",
        )
        self.assertEqual(
            [encode(f) for f in dcs_with.downlink],
            [encode(f) for f in dcs_without.downlink],
            "dropping every event frame changed the frames the engine sent",
        )

    def test_events_are_the_only_difference_and_they_do_show_up(self):
        """Guards the other side: the fingerprint must not be vacuous."""
        with_events, _ = self._run(deliver_events=True)
        without_events, _ = self._run(deliver_events=False)

        attributed = attributions(with_events)
        unattributed = attributions(without_events)
        self.assertEqual(len(attributed), len(unattributed))
        self.assertTrue(
            all(a == ATTRIBUTION_UNKNOWN for a in unattributed),
            "losses were attributed with no events delivered",
        )
        self.assertTrue(
            any(a != ATTRIBUTION_UNKNOWN for a in attributed),
            "no loss was ever attributed, so the comparison above is vacuous",
        )
        self.assertIn(
            ATTRIBUTION_UNKNOWN,
            attributed,
            "the scenario has unexplained losses; they must be recorded as unknown",
        )

    def test_losses_reach_the_campaign_not_just_the_ledger(self):
        campaign, _ = self._run(deliver_events=True)
        depot = campaign.theater.targets["latakia_fuel_depot"]
        sqn = campaign.inventories["blue"].squadron("vfa_incirlik_f16")

        target_losses = [
            loss
            for loss in campaign.tracker.losses
            if loss.entity_kind == KIND_TARGET
        ]
        self.assertEqual(len(target_losses), 3)
        self.assertEqual(depot.units_alive, 1, "target damage did not land")
        self.assertGreater(depot.damage_fraction, 0.0)
        self.assertEqual(sqn.airframes_lost, 2, "aircraft losses did not land")
        self.assertEqual(sqn.airframes_total, 12)

    def test_on_event_moves_neither_the_clock_nor_anything_else(self):
        """Structural guard on the rule, not on a scenario.

        The scenario tests above pass whenever frame ordering happens to be
        forgiving. This one fails the moment `on_event` is given the power to
        change anything but attribution -- including the clock, which would
        make a run with events schedule packages differently from a run
        without and turn the reconciliation rule into a coincidence.
        """
        campaign = Campaign()
        campaign.tick(100.0)
        package = next(iter(campaign.packages.values()))
        before = fingerprint(campaign)
        clock_before = campaign.clock

        frames = campaign.on_event(
            Event(
                seq=1,
                t=99_999.0,
                kind="kill",
                initiator="sam:kub",
                target=group_name(package.spawn_id),
                weapon="9M38",
            )
        )

        self.assertEqual(frames, [], "an event produced a downlink frame")
        self.assertEqual(
            campaign.clock, clock_before, "an event advanced the campaign clock"
        )
        self.assertEqual(
            fingerprint(campaign), before, "an event changed campaign state"
        )
        self.assertNotEqual(
            campaign.tracker.hints, {}, "the event was not even recorded as a hint"
        )

    def test_the_vanished_flight_was_recorded_as_unknown(self):
        campaign, _ = self._run(deliver_events=True)
        vanished = [
            loss for loss in campaign.tracker.losses if loss.cause == CAUSE_VANISHED
        ]
        self.assertEqual(len(vanished), 1)
        self.assertEqual(vanished[0].attribution, ATTRIBUTION_UNKNOWN)
        self.assertEqual(vanished[0].entity_kind, KIND_FLIGHT)


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


class TestSaveLoad(unittest.TestCase):
    def test_round_trip_continues_the_same_war(self):
        """Save mid-flight, reload, and run on. Both must stay identical."""
        split = 1800
        original = Campaign()
        client_a = drive(
            original,
            observer_positions=OBSERVER_AT_TARGET,
            damages=SCENARIO,
            deliver_events=True,
            start=0,
            duration=split,
        )

        airborne = [
            p
            for p in original.packages.values()
            if p.state in OPEN_STATES and p.spawn_id in original.live
        ]
        self.assertEqual(
            len(airborne),
            1,
            "nothing was in the air at the split, so the round trip is trivial",
        )
        self.assertGreater(
            len(original.tracker.losses), 0, "no losses to carry over"
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "campaign.json"
            original.save(path)
            reloaded = Campaign.load(path)
            self.assertEqual(original.to_dict(), reloaded.to_dict())

            rest = SCENARIO_DURATION - split
            client_b = fork_client(client_a, reloaded)
            mark = len(client_a.downlink)
            drive(
                original,
                observer_positions=OBSERVER_AT_TARGET,
                damages=SCENARIO,
                deliver_events=True,
                start=split + STEP,
                duration=rest,
                dcs=client_a,
            )
            drive(
                reloaded,
                observer_positions=OBSERVER_AT_TARGET,
                damages=SCENARIO,
                deliver_events=True,
                start=split + STEP,
                duration=rest,
                dcs=client_b,
            )

        self.assertEqual(
            [encode(f) for f in client_a.downlink[mark:]],
            [encode(f) for f in client_b.downlink],
            "a reloaded campaign sent different frames than an uninterrupted one",
        )
        self.assertEqual(
            original.to_dict(),
            reloaded.to_dict(),
            "a reloaded campaign reached different state than an uninterrupted one",
        )

    def test_save_is_json_and_carries_the_rng_and_spawn_counter(self):
        campaign = Campaign()
        campaign.tick(0.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            campaign.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn("rng_state", raw)
        self.assertEqual(raw["seed"], campaign.seed)
        self.assertEqual(raw["spawn_counter"], campaign._spawn_counter)
        self.assertGreater(raw["spawn_counter"], 0)

    def test_rng_is_restored_not_merely_reseeded(self):
        """Re-seeding would rewind chance; the stream has to carry on."""
        campaign = Campaign()
        for _ in range(7):
            campaign.rng.random()
        expected = [campaign.rng.random() for _ in range(3)]

        campaign2 = Campaign()
        for _ in range(7):
            campaign2.rng.random()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            campaign2.save(path)
            reloaded = Campaign.load(path)
        self.assertEqual([reloaded.rng.random() for _ in range(3)], expected)

    def test_spawn_ids_do_not_collide_across_a_reload(self):
        campaign = Campaign()
        campaign.tick(0.0)
        issued = {t.spawn_id for t in campaign.theater.targets.values()}
        issued |= {p.spawn_id for p in campaign.packages.values()}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            campaign.save(path)
            reloaded = Campaign.load(path)

        fresh = {reloaded._next_spawn_id() for _ in range(20)}
        self.assertEqual(fresh & issued, set(), "a reload reissued a live spawn id")


# ---------------------------------------------------------------------------
# The loop, end to end
# ---------------------------------------------------------------------------


class TestCampaignLoop(unittest.TestCase):
    def test_satisfies_the_engine_protocol(self):
        self.assertIsInstance(Campaign(), CampaignEngine)

    def test_hello_syncs_then_respawns_everything_live(self):
        """A reconnect means DCS restarted: the client has nothing at all."""
        campaign = Campaign()
        dcs = drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[],
            deliver_events=True,
            start=0,
            duration=1200,
        )
        live_before = set(campaign.live)
        self.assertGreater(len(live_before), 0, "nothing was live to re-spawn")

        campaign.on_disconnect()
        frames = campaign.on_hello(
            Hello(seq=99, t=1200.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        self.assertEqual(frames[0].type, "sync")
        self.assertEqual(frames[0].seq, 1, "a new connection restarts the seq stream")
        respawned = {f.spawn_id for f in frames if isinstance(f, Spawn)}
        self.assertEqual(respawned, live_before)
        self.assertEqual(len(dcs.downlink) > 0, True)

    def test_reconnect_restores_attrited_unit_counts(self):
        """A two-ship that lost a wingman must not come back as a two-ship."""
        campaign = Campaign()
        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[
                Damage(t=1500, category="plane", remove=1, event="kill",
                       initiator="sam:kub", weapon="9M38")
            ],
            deliver_events=True,
            start=0,
            duration=1560,
        )
        package = next(p for p in campaign.packages.values() if p.state in OPEN_STATES)
        self.assertEqual(campaign.tracker.units_alive(package.spawn_id), 1)

        campaign.on_disconnect()
        frames = campaign.on_hello(
            Hello(seq=99, t=1560.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        flight = next(
            f
            for f in frames
            if isinstance(f, Spawn) and f.spawn_id == package.spawn_id
        )
        self.assertEqual(flight.tasking["units"], 1)

    def _flight_in_the_bubble(self, dcs: FakeDCS | None = None):
        """A campaign whose two-ship is airborne, healthy and instantiated."""
        campaign = Campaign()
        if dcs is not None:
            dcs.campaign = campaign
            dcs.connect(0.0)
        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[],
            deliver_events=False,
            start=0,
            duration=1200,
            dcs=dcs,
        )
        package = next(p for p in campaign.packages.values() if p.state in OPEN_STATES)
        self.assertIn(
            package.spawn_id, campaign.live, "the flight never entered the bubble"
        )
        self.assertEqual(campaign.tracker.units_alive(package.spawn_id), 2)
        return campaign, package

    def _assert_nothing_was_lost(self, campaign: Campaign, package) -> None:
        self.assertEqual(
            [loss.to_dict() for loss in campaign.tracker.losses],
            [],
            "the engine invented losses for entities it removed itself",
        )
        self.assertEqual(campaign.tracker.units_alive(package.spawn_id), 2)
        self.assertEqual(package.state, ENROUTE)
        squadron = campaign.inventories["blue"].squadron("vfa_incirlik_f16")
        self.assertEqual(squadron.airframes_lost, 0)
        self.assertEqual(campaign.theater.targets["latakia_fuel_depot"].units_alive, 4)

    def test_a_despawn_the_client_never_acked_is_not_a_vanish(self):
        """We removed it. Its absence from every later census is our own doing.

        `_retire` stops expecting snapshots when the despawn is *sent*, not when
        it is acknowledged, precisely because the ack may never come. Without
        that, a healthy two-ship flown out of the bubble comes back as two
        `vanished` airframes and a destroyed package.
        """
        dcs = SilentDespawnDCS(campaign=Campaign(), deliver_events=False)
        campaign, package = self._flight_in_the_bubble(dcs)

        drive(
            campaign,
            observer_positions=OBSERVER_FAR_AWAY,
            damages=[],
            deliver_events=False,
            start=1200,
            duration=300,
            dcs=dcs,
        )

        self.assertNotIn(package.spawn_id, campaign.live)
        self.assertTrue(
            any(
                isinstance(f, Despawn) and f.reason == "left_bubble"
                for f in dcs.downlink
            ),
            "the flight was never despawned, so this proves nothing",
        )
        self._assert_nothing_was_lost(campaign, package)

    def test_a_disconnect_stops_the_engine_expecting_snapshots(self):
        campaign, _ = self._flight_in_the_bubble()
        self.assertTrue(
            [g for g in campaign.tracker.groups.values() if g.expects_snapshot],
            "nothing was awaiting snapshots, so this proves nothing",
        )
        campaign.on_disconnect()
        self.assertEqual(
            [g.spawn_id for g in campaign.tracker.groups.values() if g.expects_snapshot],
            [],
            "the engine still expects snapshots from a sim that is gone",
        )

    def test_a_restarted_client_is_not_indicted_for_its_empty_first_census(self):
        """A reconnect re-issues every spawn. Until they are acked, nothing is live.

        The client's first census can easily go out before it has finished
        creating what the engine just re-issued -- and measured against the old
        connection's timings, that absence is long past the vanish grace window.
        """
        campaign, package = self._flight_in_the_bubble()
        campaign.on_disconnect()

        frames = campaign.on_hello(
            Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        self.assertIn(
            package.spawn_id,
            {f.spawn_id for f in frames if isinstance(f, Spawn)},
            "the flight was not re-issued, so this proves nothing",
        )
        # Censuses arrive; the acks for those spawns do not.
        for t in (0.0, 30.0, 60.0, 120.0, 240.0):
            campaign.on_state(StateReport(seq=2, t=t, groups=[]))

        self._assert_nothing_was_lost(campaign, package)

    def test_a_campaign_reloaded_mid_sortie_does_not_start_by_losing_the_flight(self):
        """The save was written with the flight instantiated, because it was.

        A process that was killed rather than shut down never ran
        `on_disconnect`, so the save says the flight is live in a DCS that no
        longer exists. The first `hello` has to undo that belief before any
        census arrives, or the engine opens the reloaded campaign by writing off
        a two-ship that is sitting on the ramp of a mission nobody has started.
        """
        campaign, _ = self._flight_in_the_bubble()
        self.assertTrue(
            [g for g in campaign.tracker.groups.values() if g.expects_snapshot],
            "nothing was instantiated in the save, so this proves nothing",
        )
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "campaign.json"
        campaign.save(path)

        reloaded = Campaign.load(path)
        package = next(p for p in reloaded.packages.values() if p.state in OPEN_STATES)
        reloaded.on_hello(
            Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        for t in (0.0, 30.0, 60.0, 120.0, 240.0):
            reloaded.on_state(StateReport(seq=2, t=t, groups=[]))

        self._assert_nothing_was_lost(reloaded, package)

    def test_protocol_mismatch_is_fatal(self):
        from campaign.protocol import ProtocolError

        campaign = Campaign()
        with self.assertRaises(ProtocolError):
            campaign.on_hello(
                Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION + 1, theater="Syria")
            )

    def test_the_war_ends_when_the_last_target_dies(self):
        campaign = Campaign()
        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[Damage(t=1500, category="structure", remove=4, event="kill",
                            initiator="cmp_0002", weapon="GBU-38")],
            deliver_events=True,
            start=0,
            duration=SCENARIO_DURATION,
        )
        depot = campaign.theater.targets["latakia_fuel_depot"]
        self.assertTrue(depot.destroyed)
        self.assertEqual(depot.damage_fraction, 1.0)
        self.assertEqual(
            [p for p in campaign.packages.values() if p.state in OPEN_STATES],
            [],
            "the engine kept fragging packages against rubble",
        )
        self.assertNotIn(depot.spawn_id, campaign.live)

    def test_the_engine_is_deterministic_across_identical_runs(self):
        a = Campaign()
        b = Campaign()
        dcs_a = drive(a, observer_positions=OBSERVER_AT_TARGET, damages=SCENARIO,
                      deliver_events=True, start=0, duration=SCENARIO_DURATION)
        dcs_b = drive(b, observer_positions=OBSERVER_AT_TARGET, damages=SCENARIO,
                      deliver_events=True, start=0, duration=SCENARIO_DURATION)
        self.assertEqual(
            [encode(f) for f in dcs_a.downlink], [encode(f) for f in dcs_b.downlink]
        )
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_a_different_seed_changes_only_seeded_choices(self):
        a = Campaign(seed=1)
        b = Campaign(seed=2)
        a.tick(0.0)
        b.tick(0.0)
        pkg_a = next(iter(a.packages.values()))
        pkg_b = next(iter(b.packages.values()))
        self.assertEqual(pkg_a.t_tot, pkg_b.t_tot)
        self.assertEqual(pkg_a.spawn_id, pkg_b.spawn_id)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class TestPlanner(unittest.TestCase):
    def test_select_target_prefers_priority_and_skips_rubble(self):
        theater = build_slice_theater()
        depot = theater.targets["latakia_fuel_depot"]
        self.assertIs(select_target(theater, enemy_of("blue")), depot)

        depot.units_alive = 0
        self.assertIsNone(select_target(theater, enemy_of("blue")))

    def test_build_package_returns_none_when_inventory_cannot_cover_it(self):
        theater = build_slice_theater()
        blue, _ = build_slice_oob()
        sqn = blue.squadron("vfa_incirlik_f16")
        sqn.airframes_available = 1
        sqn.airframes_lost = 11

        package = build_package(
            package_id="pkg0001",
            spawn_id="0002",
            inventory=blue,
            base=theater.airbase("incirlik"),
            target=theater.targets["latakia_fuel_depot"],
            now=0.0,
            rng=random.Random(1),
        )
        self.assertIsNone(package)
        self.assertEqual(sqn.open_reservations, {})
        self.assertEqual(sqn.airframes_available, 1)

    def test_build_package_reserves_before_it_returns(self):
        theater = build_slice_theater()
        blue, _ = build_slice_oob()
        sqn = blue.squadron("vfa_incirlik_f16")
        package = build_package(
            package_id="pkg0001",
            spawn_id="0002",
            inventory=blue,
            base=theater.airbase("incirlik"),
            target=theater.targets["latakia_fuel_depot"],
            now=100.0,
            rng=random.Random(1),
        )
        assert package is not None
        self.assertIn(package.reservation_id, sqn.open_reservations)
        self.assertEqual(sqn.airframes_available, 10)
        self.assertLess(package.t_takeoff, package.t_tot)
        self.assertLess(package.t_tot, package.t_rtb)

    def test_the_paper_track_is_a_pure_function_of_time(self):
        theater = build_slice_theater()
        blue, _ = build_slice_oob()
        base = theater.airbase("incirlik")
        target = theater.targets["latakia_fuel_depot"]
        package = build_package(
            package_id="pkg0001",
            spawn_id="0002",
            inventory=blue,
            base=base,
            target=target,
            now=0.0,
            rng=random.Random(1),
        )
        assert package is not None

        self.assertEqual(package_position(package, base, target, 0.0), base.pos)
        at_tot = package_position(package, base, target, package.t_tot)
        self.assertLess(ground_distance(at_tot, target.pos), 1.0)
        self.assertEqual(
            package_position(package, base, target, package.t_rtb + 1.0), base.pos
        )

        # The interior of the track, not just its endpoints. Without this the
        # outbound leg can teleport the flight onto the target at takeoff and
        # nothing notices -- and the bubble would then instantiate a two-ship
        # over its target twenty minutes before its TOT.
        leg = ground_distance(base.pos, target.pos)
        midpoint = (package.t_takeoff + package.t_tot) / 2.0
        at_mid = package_position(package, base, target, midpoint)
        self.assertAlmostEqual(ground_distance(base.pos, at_mid) / leg, 0.5, places=2)
        self.assertAlmostEqual(ground_distance(at_mid, target.pos) / leg, 0.5, places=2)

        # And it closes on the target monotonically, rather than merely passing
        # through the halfway point on its way somewhere else.
        span = package.t_tot - package.t_takeoff
        ranges = [
            ground_distance(
                package_position(
                    package, base, target, package.t_takeoff + span * n / 10.0
                ),
                target.pos,
            )
            for n in range(11)
        ]
        self.assertEqual(ranges, sorted(ranges, reverse=True))
        self.assertAlmostEqual(ranges[0] / leg, 1.0, places=2)

        # The egress leg is the same story in reverse.
        egress = (package.t_tot + package.t_rtb) / 2.0
        at_egress = package_position(package, base, target, egress)
        self.assertAlmostEqual(
            ground_distance(target.pos, at_egress) / leg, 0.5, places=2
        )
        self.assertGreater(
            ground_distance(at_egress, target.pos),
            ground_distance(
                package_position(package, base, target, package.t_tot + 1.0), target.pos
            ),
        )

        # Cruising altitude, not ground level: a flight re-instantiated on its
        # paper track has to come back in the air.
        self.assertAlmostEqual(at_mid[1], CRUISE_ALTITUDE)
        self.assertTrue(math.isfinite(at_mid[0]))


if __name__ == "__main__":
    unittest.main()
