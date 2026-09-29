"""The campaign brain: the object the transport talks to.

`Campaign` structurally satisfies :class:`campaign.api.CampaignEngine`. It owns
identity (spawn ids), time (one non-decreasing clock), chance (one seeded RNG)
and every piece of campaign state, and it writes all of that to JSON so that a
reload continues the same war rather than a similar one.

Two rules shape the code more than anything else:

**Reconciliation.** :meth:`on_state` is the only handler that can change the
campaign. :meth:`on_event` touches the attribution hint queue and returns --
it does not even advance the clock, because a clock that moved further when
events were delivered would make package schedules diverge between a run with
events and a run without, and the whole point is that they cannot.

**Determinism.** Nothing here reads a wall clock or an unseeded random source.
Time enters through frame `t`; chance enters through `self.rng`, whose full
state round-trips through `save`/`load`. A campaign is therefore a pure
function of its log, which is what makes a whole war replayable and a bug
reproducible. `tick(now)` is a bare pulse and its argument is deliberately
unused -- see :meth:`Campaign.tick`.

**Two clocks.** Frames carry *mission* time, which DCS resets to zero on every
restart. The campaign runs on its own monotonic clock and rebases the mission
clock onto it at each `hello` (:attr:`Campaign.mission_epoch`). Without that,
a campaign 1200 seconds old meeting a freshly restarted mission would stand
still until mission time caught up, losing exactly one restart's worth of war.
"""

from __future__ import annotations

import dataclasses
import json
import random
from pathlib import Path
from typing import Any

from campaign.attrition import (
    ATTRIBUTION_UNOBSERVED,
    CAUSE_UNOBSERVED,
    KIND_FLIGHT,
    KIND_TARGET,
    AttritionTracker,
    LossRecord,
)
from campaign.bubble import bubble_delta, resolve_bubble
from campaign.oob import SideInventory, Squadron, UnknownReservation, build_slice_oob
from campaign.resolver import resolve_strike
from campaign.planner import (
    ABORTED,
    COMPLETE,
    DESTROYED,
    ENROUTE,
    Package,
    build_package,
    package_heading,
    package_position,
    package_route,
    select_target,
)
from campaign.protocol import (
    DEFAULT_OBSERVER_PERIOD,
    DEFAULT_STATE_PERIOD,
    PROTOCOL_VERSION,
    Ack,
    Despawn,
    Downlink,
    Event,
    Hello,
    Message,
    ObserverReport,
    ProtocolError,
    Spawn,
    StateReport,
    Sync,
    Vec3,
    Waypoint,
    group_name,
)
from campaign.theater import Target, Theater, build_slice_theater, enemy_of

#: Serialisation format of a saved campaign. Bump on any breaking change to
#: :meth:`Campaign.to_dict`.
SAVE_VERSION = 2

#: Default seed. Explicit, because an implicit one is an unseeded one.
DEFAULT_SEED = 20240923

#: Bubble radii. The despawn radius is strictly larger so that an observer
#: loitering on the boundary cannot thrash the client.
DEFAULT_SPAWN_RADIUS = 75_000.0
DEFAULT_DESPAWN_RADIUS = 95_000.0

_SPAWN = "spawn"
_DESPAWN = "despawn"


class Campaign:
    """The whole campaign. One instance per war."""

    def __init__(
        self,
        *,
        theater: Theater | None = None,
        inventories: dict[str, SideInventory] | None = None,
        seed: int = DEFAULT_SEED,
        player_coalition: str = "blue",
        spawn_radius: float = DEFAULT_SPAWN_RADIUS,
        despawn_radius: float = DEFAULT_DESPAWN_RADIUS,
        state_period: float = DEFAULT_STATE_PERIOD,
        observer_period: float = DEFAULT_OBSERVER_PERIOD,
    ) -> None:
        if inventories is None:
            blue, red = build_slice_oob()
            inventories = {blue.coalition: blue, red.coalition: red}
        self.theater: Theater = theater if theater is not None else build_slice_theater()
        self.inventories: dict[str, SideInventory] = inventories
        self.player_coalition = player_coalition
        self.spawn_radius = spawn_radius
        self.despawn_radius = despawn_radius
        self.state_period = state_period
        self.observer_period = observer_period

        self.seed = seed
        self.rng = random.Random(seed)
        self.tracker = AttritionTracker(vanish_grace=state_period)
        self.packages: dict[str, Package] = {}

        #: Single non-decreasing campaign clock, in campaign seconds.
        self.clock: float = 0.0
        #: Campaign time that the current connection's mission time zero maps
        #: to. Re-derived at every `hello`, so a DCS restart costs nothing.
        self.mission_epoch: float = 0.0
        self._spawn_counter: int = 0
        self._package_counter: int = 0
        self._out_seq: int = 1

        self.observers: list[Vec3] = []
        #: Spawn ids the engine believes should exist inside DCS right now.
        self.live: set[str] = set()
        #: Outstanding downlink refs: ref -> (action, spawn_id).
        self.pending: dict[int, tuple[str, str]] = {}
        #: Spawn ids the client rejected. Not retried; a rejected spawn is a
        #: bad template, and retrying it every bubble sync is a frame storm.
        self.blocked: set[str] = set()
        self.connected: bool = False
        self._objectives_announced: bool = False
        #: Not persisted; see _announce_dry.
        self._dry_announced: bool = False

        self._ensure_targets_tracked()

    # ------------------------------------------------------------------
    # identity, time, framing
    # ------------------------------------------------------------------

    def _next_spawn_id(self) -> str:
        """Short, engine-owned, and never reused -- the counter is persisted."""
        self._spawn_counter += 1
        return f"{self._spawn_counter:04x}"

    def _next_package_id(self) -> str:
        self._package_counter += 1
        return f"pkg{self._package_counter:04d}"

    def _seq(self) -> int:
        seq = self._out_seq
        self._out_seq += 1
        return seq

    def campaign_time(self, mission_t: float) -> float:
        """Campaign seconds for a mission time carried by an inbound frame."""
        return mission_t + self.mission_epoch

    def mission_time(self, campaign_t: float | None = None) -> float:
        """Mission seconds for a campaign time, for outbound frames.

        Every `t` on the wire is mission time: docs/protocol.md defines the
        envelope's `t` as `timer.getTime()`, and a client that has just
        restarted has no idea what campaign time is.
        """
        clock = self.clock if campaign_t is None else campaign_t
        return clock - self.mission_epoch

    def _advance(self, mission_t: float) -> None:
        self.clock = max(self.clock, self.campaign_time(mission_t))

    def _rebase(self, frame: Any) -> Any:
        """The same frame with its `t` moved from mission time to campaign time.

        Everything downstream of the handlers -- the attrition ledger above
        all -- reasons in campaign time. Converting once, here, keeps a DCS
        restart from writing a loss timestamped before the losses it follows.
        """
        return dataclasses.replace(frame, t=self.campaign_time(frame.t))

    def _message(self, text: str, to: str | None = None) -> Message:
        return Message(
            seq=self._seq(),
            t=self.mission_time(),
            to=to or self.player_coalition,
            text=text,
        )

    # ------------------------------------------------------------------
    # CampaignEngine
    # ------------------------------------------------------------------

    def on_hello(self, msg: Hello) -> list[Downlink]:
        """Reply with `sync`, then re-spawn everything that should be live.

        A hello always means the client has nothing: either it just started or
        DCS restarted under it. The engine owns identity, so re-issuing the
        same spawn ids restores exactly the entities that were there, with the
        unit counts attrition has already recorded.
        """
        if msg.protocol != PROTOCOL_VERSION:
            raise ProtocolError(
                f"client protocol {msg.protocol} != engine {PROTOCOL_VERSION}"
            )
        # Rebase before advancing: the mission clock this client reports from
        # is pinned to wherever the campaign has already got to, so the war
        # neither jumps forward nor stalls while a restarted mission catches up.
        self.mission_epoch = self.clock - msg.t
        self._advance(msg.t)
        self.connected = True
        # New connection, new frame stream: seq restarts and any ref still
        # outstanding on the old socket will never be acknowledged.
        self._out_seq = 1
        self.pending.clear()
        self.tracker.detach_all()

        frames: list[Downlink] = [
            Sync(
                seq=self._seq(),
                t=self.mission_time(),
                campaign_time=self.clock,
                protocol=PROTOCOL_VERSION,
                state_period=self.state_period,
                observer_period=self.observer_period,
                bubble_radius=self.spawn_radius,
            )
        ]
        for spawn_id in sorted(self.live):
            frame = self._spawn_frame(spawn_id)
            if frame is None:
                # Whatever it was is gone from the campaign; do not keep
                # claiming it is live or every hello will re-try it.
                self.live.discard(spawn_id)
                continue
            frames.append(frame)
        frames.extend(self._brief_open_packages())
        return frames

    def _brief_open_packages(self) -> list[Downlink]:
        """Tell a client that has just arrived what is already in the air.

        A package can be fragged before any client connects -- the engine runs
        whether DCS does or not -- and the message that announced it went
        nowhere, because there was no socket to write it to. Without this, the
        player's picture of the war depends on whether the engine happened to
        be started first.
        """
        frames: list[Downlink] = []
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            if not package.is_open:
                continue
            target = self.theater.targets.get(package.target_id)
            frames.append(
                self._message(
                    f"{package.callsign} on task: strike on "
                    f"{target.name if target else package.target_id}, "
                    f"TOT {self.mission_time(package.t_tot):.0f}."
                )
            )
        return frames

    def on_observer(self, msg: ObserverReport) -> list[Downlink]:
        """Bubble input, and the campaign's heartbeat.

        Observer frames are the densest thing on the wire that carries mission
        time, so this is where the campaign actually moves. `tick` cannot be:
        its cadence is wall time, and a mission running at any rate other than
        1x would get a war that runs at a different rate than it does.
        """
        self._advance(msg.t)
        self.observers = [o.pos for o in msg.observers]
        return self._pulse()

    def on_event(self, msg: Event) -> list[Downlink]:
        """Attribution only.

        Deliberately does not advance the clock and deliberately returns no
        frames. If this method could move time or emit anything, dropping the
        event stream would change the campaign, and the reconciliation rule
        would be a comment rather than a property.
        """
        self.tracker.note_event(self._rebase(msg))
        return []

    def on_state(self, msg: StateReport) -> list[Downlink]:
        """Ground truth. The only path by which the campaign may lose anything."""
        self._advance(msg.t)
        frames: list[Downlink] = []
        for loss in self.tracker.ingest(self._rebase(msg)):
            frames.extend(self._apply_loss(loss))
        frames.extend(self._close_out_dead_packages())
        frames.extend(self._pulse())
        return frames

    def on_ack(self, msg: Ack) -> list[Downlink]:
        self._advance(msg.t)
        entry = self.pending.pop(msg.ref, None)
        if entry is None:
            return []
        action, spawn_id = entry
        if action == _SPAWN:
            if msg.ok:
                self.tracker.mark_instantiated(spawn_id, self.clock)
            else:
                return self._spawn_rejected(spawn_id, msg.error or "")
        elif action == _DESPAWN and msg.ok:
            self.tracker.mark_removed(spawn_id)
        return []

    def tick(self, now: float) -> list[Downlink]:
        """Periodic work. `now` is read for nothing, on purpose.

        `api.CampaignEngine.tick` documents `now` as monotonic wall seconds,
        and the transport supplies exactly that. Wall seconds are not mission
        seconds: DCS can be paused, time-compressed, or not running at all,
        and the engine may be started long before the sim connects. Folding
        `now` into the campaign clock made the first hour of a mission run in
        the campaign's past whenever the engine was launched an hour early,
        and made a time-compressed run's schedule depend on how fast the host
        happened to be. Mission time arrives on frames; this is only the
        prompt to act on it, so it stays safe to call at any cadence.
        """
        return self._pulse()

    def _pulse(self) -> list[Downlink]:
        """Walk packages forward, task a new one, reconcile the bubble."""
        frames: list[Downlink] = []
        frames.extend(self._advance_packages())
        frames.extend(self._plan())
        frames.extend(self._sync_bubble())
        return frames

    def on_disconnect(self) -> None:
        """DCS went away. Campaign belief stands; only instantiation is lost.

        `self.live` is kept on purpose: it is what the next `hello` re-issues.
        Observers are kept too, so the bubble does not collapse and rebuild in
        the seconds between the client reconnecting and its first observer
        frame.
        """
        self.connected = False
        self.pending.clear()
        self.tracker.detach_all()

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------

    def _plan(self) -> list[Downlink]:
        """Commit at most one strike package.

        TODO(seam): multi-package deconfliction -- several packages in the air
        at once, sequenced on time and route -- replaces this "one at a time"
        rule. Out of scope for the slice.
        """
        if any(pkg.is_open for pkg in self.packages.values()):
            return []
        enemy = enemy_of(self.player_coalition)
        target = select_target(self.theater, enemy)
        if target is None:
            return self._announce_objectives_complete()
        inventory = self.inventories.get(self.player_coalition)
        if inventory is None:
            return []

        for base in sorted(
            self.theater.airbases_of(self.player_coalition), key=lambda b: b.id
        ):
            package_id = self._next_package_id()
            spawn_id = self._next_spawn_id()
            package = build_package(
                package_id=package_id,
                spawn_id=spawn_id,
                inventory=inventory,
                base=base,
                target=target,
                now=self.clock,
                rng=self.rng,
            )
            if package is None:
                # Nothing was issued, so give the ids back rather than leaving
                # a gap on every tick a stalled campaign fails to plan.
                self._package_counter -= 1
                self._spawn_counter -= 1
                dry = self._announce_dry(base)
                if dry:
                    return dry
                continue
            self.packages[package.id] = package
            self._dry_announced = False
            self.tracker.track(
                spawn_id,
                entity_id=package.id,
                entity_kind=KIND_FLIGHT,
                coalition=self.player_coalition,
                units_initial=package.flight_size,
            )
            return [
                self._message(
                    f"{package.callsign} fragged: {package.flight_size}-ship strike "
                    f"on {target.name}, TOT {self.mission_time(package.t_tot):.0f}."
                )
            ]
        return []

    def _announce_dry(self, base: Any) -> list[Downlink]:
        """Say so, once, when the campaign can no longer task anything.

        A campaign that quietly stops planning is indistinguishable from one
        with nothing to do. This is the difference between a war that ended and
        a war that ran out of bombs, and the save file looks identical either
        way. Deliberately not persisted: repeating it once after a reload is a
        far smaller sin than a silent stall.
        """
        if self._dry_announced:
            return []
        self._dry_announced = True
        return [
            self._message(
                f"Cannot task: {base.name} has no aircraft or ordnance available."
            )
        ]

    def _announce_objectives_complete(self) -> list[Downlink]:
        if self._objectives_announced:
            return []
        self._objectives_announced = True
        return [self._message("All assigned strategic targets destroyed.")]

    def _advance_packages(self) -> list[Downlink]:
        """Walk open packages forward on their paper track."""
        frames: list[Downlink] = []
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            if not package.is_open:
                continue
            if package.state != ENROUTE and self.clock >= package.t_takeoff:
                package.state = ENROUTE
            if not package.weapons_released and self.clock >= package.t_tot:
                frames.extend(self._release_weapons(package))
            if self.clock >= package.t_rtb:
                frames.extend(self._complete_package(package))
        return frames

    def _release_weapons(self, package: Package) -> list[Downlink]:
        """Expend the ordnance the surviving aircraft carried to the target.

        Expenditure comes off the paper track, not off a snapshot, because it
        is the engine's own fact: the engine knows what it loaded and what
        reached the target.

        What the ordnance *achieved* has two possible authorities, and exactly
        one of them applies. If DCS is holding the target at this instant, the
        sim decides and the answer arrives in a snapshot. If it is not, nobody
        is watching, and `campaign.resolver` rolls the outcome on `self.rng`.
        Choosing once, here, is what keeps a target from being killed twice.

        TODO(threat): unobserved strikes currently cost nothing. The flight
        always comes home, because applying a flight loss on paper means
        decrementing the tracked group as well as debiting the reservation,
        and that belongs with a real threat model rather than a flat dice
        roll. Until then an unflown strike is safer than a flown one.
        """
        package.weapons_released = True
        survivors = self.tracker.units_alive(package.spawn_id)
        squadron = self._squadron_for(package)
        if squadron is not None and package.reservation_id in squadron.open_reservations:
            squadron.debit_munitions(
                package.reservation_id,
                survivors * package.rounds_per_aircraft,
                lost=False,
            )
        if survivors <= 0:
            return []
        frames = self._resolve_unobserved(package, survivors)
        frames.append(
            self._message(
                f"{package.callsign} off target, {survivors} aircraft egressing."
            )
        )
        return frames

    def _resolve_unobserved(self, package: Package, survivors: int) -> list[Downlink]:
        """Damage a target nobody was watching. No-op if DCS holds it."""
        target = self.theater.targets.get(package.target_id)
        if target is None or target.destroyed:
            return []
        if self.tracker.is_instantiated(target.spawn_id):
            return []  # DCS has it; the snapshot is the authority.
        outcome = resolve_strike(
            rounds=survivors * package.rounds_per_aircraft,
            target_units_alive=target.units_alive,
            rng=self.rng,
        )
        frames: list[Downlink] = []
        for _ in range(outcome.units_killed):
            loss = LossRecord(
                t=self.clock,
                spawn_id=target.spawn_id,
                entity_id=target.id,
                entity_kind=KIND_TARGET,
                coalition=target.coalition,
                cause=CAUSE_UNOBSERVED,
                attribution=ATTRIBUTION_UNOBSERVED,
            )
            self.tracker.losses.append(loss)
            frames.extend(self._apply_target_loss(loss))
        if outcome.missed:
            frames.append(
                self._message(f"{package.callsign} reports no effect on target.")
            )
        return frames

    def _complete_package(self, package: Package) -> list[Downlink]:
        package.state = COMPLETE
        frames = self._retire(package.spawn_id, "mission_complete")
        self._settle(package)
        survivors = self.tracker.units_alive(package.spawn_id)
        self.tracker.forget(package.spawn_id)
        frames.append(
            self._message(f"{package.callsign} recovered, {survivors} aircraft home.")
        )
        return frames

    def _close_out_dead_packages(self) -> list[Downlink]:
        frames: list[Downlink] = []
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            if not package.is_open or self.tracker.is_alive(package.spawn_id):
                continue
            package.state = DESTROYED
            frames.extend(self._retire(package.spawn_id, "destroyed"))
            self._settle(package)
            self.tracker.forget(package.spawn_id)
            frames.append(self._message(f"{package.callsign} is lost."))
        return frames

    def _settle(self, package: Package) -> None:
        """Close the reservation, returning anything still held to stock."""
        squadron = self._squadron_for(package)
        if squadron is None:
            return
        try:
            squadron.release(package.reservation_id)
        except UnknownReservation:
            return

    def _squadron_for(self, package: Package) -> Squadron | None:
        inventory = self.inventories.get(self.player_coalition)
        if inventory is None:
            return None
        return inventory.squadrons.get(package.squadron_id)

    # ------------------------------------------------------------------
    # loss application
    # ------------------------------------------------------------------

    def _apply_loss(self, loss: LossRecord) -> list[Downlink]:
        if loss.entity_kind == KIND_FLIGHT:
            return self._apply_flight_loss(loss)
        if loss.entity_kind == KIND_TARGET:
            return self._apply_target_loss(loss)
        return []

    def _apply_flight_loss(self, loss: LossRecord) -> list[Downlink]:
        package = self.packages.get(loss.entity_id)
        if package is None:
            return []
        squadron = self._squadron_for(package)
        if squadron is None:
            return []
        if package.reservation_id not in squadron.open_reservations:
            return []
        squadron.debit_airframes(package.reservation_id, 1)
        if not package.weapons_released:
            # The jet took its bombs into the ground with it. Still conserved,
            # just not the same fact about the war as bombs on a target.
            squadron.debit_munitions(
                package.reservation_id, package.rounds_per_aircraft, lost=True
            )
        return []

    def _apply_target_loss(self, loss: LossRecord) -> list[Downlink]:
        target = self.theater.targets.get(loss.entity_id)
        if target is None:
            return []
        target.units_alive = max(0, target.units_alive - 1)
        if not target.destroyed:
            return []
        frames = self._retire(target.spawn_id, "destroyed")
        self.tracker.forget(target.spawn_id)
        frames.append(self._message(f"{target.name} destroyed."))
        return frames

    # ------------------------------------------------------------------
    # bubble
    # ------------------------------------------------------------------

    def _candidates(self) -> dict[str, Vec3]:
        """Everything that *could* be instantiated, with where it is right now.

        A pure read: anything absent here is something the campaign no longer
        wants in DCS, so the bubble delta produces its despawn with no special
        case.
        """
        out: dict[str, Vec3] = {}
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            if package.spawn_id in self.blocked:
                continue
            if not package.is_airborne(self.clock):
                continue
            if not self.tracker.is_alive(package.spawn_id):
                continue
            placement = self._package_placement(package)
            if placement is None:
                continue
            out[package.spawn_id] = placement[0]
        for target in self.theater.targets.values():
            if target.destroyed or not target.spawn_id:
                continue
            if target.spawn_id in self.blocked:
                continue
            out[target.spawn_id] = target.pos
        return out

    def _sync_bubble(self) -> list[Downlink]:
        candidates = self._candidates()
        wanted = resolve_bubble(
            self.observers,
            candidates,
            self.live,
            spawn_radius=self.spawn_radius,
            despawn_radius=self.despawn_radius,
        )
        to_spawn, to_despawn = bubble_delta(self.live, wanted)
        frames: list[Downlink] = []
        for spawn_id in to_despawn:
            reason = "left_bubble" if spawn_id in candidates else "retired"
            frames.extend(self._retire(spawn_id, reason))
        for spawn_id in to_spawn:
            frame = self._spawn_frame(spawn_id)
            if frame is None:
                continue
            frames.append(frame)
            self.live.add(spawn_id)
        return frames

    def _retire(self, spawn_id: str, reason: str) -> list[Downlink]:
        """Remove an entity from DCS. A despawn for something gone is a no-op."""
        if spawn_id not in self.live:
            self.tracker.mark_removed(spawn_id)
            return []
        self.live.discard(spawn_id)
        # Stop expecting snapshots the moment the frame is sent, not when it is
        # acknowledged: a report generated in between must not read as a vanish.
        self.tracker.mark_removed(spawn_id)
        seq = self._seq()
        self.pending[seq] = (_DESPAWN, spawn_id)
        return [
            Despawn(
                seq=seq,
                t=self.mission_time(),
                ref=seq,
                spawn_id=spawn_id,
                reason=reason,
            )
        ]

    def _spawn_rejected(self, spawn_id: str, error: str) -> list[Downlink]:
        """The client refused a spawn. Unwind rather than leak."""
        self.live.discard(spawn_id)
        self.blocked.add(spawn_id)
        self.tracker.mark_removed(spawn_id)
        for package in self.packages.values():
            if package.spawn_id != spawn_id or not package.is_open:
                continue
            package.state = ABORTED
            self._settle(package)
            self.tracker.forget(spawn_id)
            return [
                self._message(f"{package.callsign} scrubbed: client rejected spawn.")
            ]
        return []

    # ------------------------------------------------------------------
    # frame construction
    # ------------------------------------------------------------------

    def _package_placement(self, package: Package) -> tuple[Vec3, float] | None:
        base = self.theater.airbases.get(package.base_id)
        target = self.theater.targets.get(package.target_id)
        if base is None or target is None:
            return None
        return (
            package_position(package, base, target, self.clock),
            package_heading(package, base, target, self.clock),
        )

    def _spawn_frame(self, spawn_id: str) -> Spawn | None:
        for package in self.packages.values():
            if package.spawn_id == spawn_id and package.is_open:
                return self._flight_spawn_frame(package)
        for target in self.theater.targets.values():
            if target.spawn_id == spawn_id and not target.destroyed:
                return self._target_spawn_frame(target)
        return None

    def _flight_spawn_frame(self, package: Package) -> Spawn | None:
        base = self.theater.airbases.get(package.base_id)
        target = self.theater.targets.get(package.target_id)
        squadron = self._squadron_for(package)
        placement = self._package_placement(package)
        if base is None or target is None or squadron is None or placement is None:
            return None
        position, heading = placement
        seq = self._seq()
        self.pending[seq] = (_SPAWN, package.spawn_id)
        return Spawn(
            seq=seq,
            t=self.mission_time(),
            ref=seq,
            spawn_id=package.spawn_id,
            coalition=squadron.coalition,
            category="plane",
            template=squadron.template,
            # What attrition has recorded, not the package's fragged size: a
            # flight that re-enters the bubble after losing a wingman must not
            # come back whole.
            units=self.tracker.units_alive(package.spawn_id),
            position=position,
            heading=heading,
            # TODO(seam): a flight spawned before it has left the ground would
            # carry its base's DCS airdrome id on the first waypoint, making it
            # a ramp start (docs/protocol.md, Waypoint.airdrome_id). Not set
            # yet: theater.Airbase has no DCS id, and real ids are map content
            # nobody has validated -- a wrong one parks the flight at another
            # airfield. Every flight is an air start until then.
            route=[
                Waypoint(pos=pos, alt=alt, speed=speed, action=action)
                for pos, alt, speed, action in package_route(
                    package, base, target, self.clock
                )
            ],
            tasking={
                "kind": "strike",
                "target": group_name(target.spawn_id) if target.spawn_id else target.id,
                "tot": self.mission_time(package.t_tot),
                "callsign": package.callsign,
            },
        )

    def _target_spawn_frame(self, target: Target) -> Spawn:
        seq = self._seq()
        self.pending[seq] = (_SPAWN, target.spawn_id)
        return Spawn(
            seq=seq,
            t=self.mission_time(),
            ref=seq,
            spawn_id=target.spawn_id,
            coalition=target.coalition,
            category=target.category,
            template=target.template,
            # A half-flattened target comes back half-flattened, or it would
            # have to be destroyed twice.
            units=self.tracker.units_alive(target.spawn_id),
            position=target.pos,
            heading=0.0,
            route=[],
            tasking={"kind": "static"},
        )

    def _ensure_targets_tracked(self) -> None:
        for target in sorted(self.theater.targets.values(), key=lambda t: t.id):
            if target.destroyed:
                continue
            if not target.spawn_id:
                target.spawn_id = self._next_spawn_id()
            if target.spawn_id not in self.tracker.groups:
                self.tracker.track(
                    target.spawn_id,
                    entity_id=target.id,
                    entity_kind=KIND_TARGET,
                    coalition=target.coalition,
                    units_initial=target.units_initial,
                    units_alive=target.units_alive,
                )

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Everything. A reload that loses one field is a campaign that drifts."""
        version, internal, gauss = self.rng.getstate()
        return {
            "save_version": SAVE_VERSION,
            "seed": self.seed,
            "rng_state": [version, list(internal), gauss],
            "clock": self.clock,
            "mission_epoch": self.mission_epoch,
            "spawn_counter": self._spawn_counter,
            "package_counter": self._package_counter,
            "out_seq": self._out_seq,
            "player_coalition": self.player_coalition,
            "spawn_radius": self.spawn_radius,
            "despawn_radius": self.despawn_radius,
            "state_period": self.state_period,
            "observer_period": self.observer_period,
            "observers": [list(p) for p in self.observers],
            "live": sorted(self.live),
            "blocked": sorted(self.blocked),
            "pending": {str(k): list(v) for k, v in sorted(self.pending.items())},
            "connected": self.connected,
            "objectives_announced": self._objectives_announced,
            "theater": self.theater.to_dict(),
            "inventories": {k: v.to_dict() for k, v in self.inventories.items()},
            "packages": {k: v.to_dict() for k, v in sorted(self.packages.items())},
            "tracker": self.tracker.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Campaign:
        if raw.get("save_version") != SAVE_VERSION:
            raise ValueError(
                f"save_version {raw.get('save_version')} != {SAVE_VERSION}"
            )
        campaign = cls(
            theater=Theater.from_dict(raw["theater"]),
            inventories={
                k: SideInventory.from_dict(v) for k, v in raw["inventories"].items()
            },
            seed=int(raw["seed"]),
            player_coalition=raw["player_coalition"],
            spawn_radius=float(raw["spawn_radius"]),
            despawn_radius=float(raw["despawn_radius"]),
            state_period=float(raw["state_period"]),
            observer_period=float(raw["observer_period"]),
        )
        version, internal, gauss = raw["rng_state"]
        campaign.rng.setstate((version, tuple(internal), gauss))
        campaign.clock = float(raw["clock"])
        campaign.mission_epoch = float(raw["mission_epoch"])
        campaign._spawn_counter = int(raw["spawn_counter"])
        campaign._package_counter = int(raw["package_counter"])
        campaign._out_seq = int(raw["out_seq"])
        campaign.observers = [
            (float(p[0]), float(p[1]), float(p[2])) for p in raw["observers"]
        ]
        campaign.live = set(raw["live"])
        campaign.blocked = set(raw["blocked"])
        campaign.pending = {
            int(k): (v[0], v[1]) for k, v in raw["pending"].items()
        }
        campaign.connected = bool(raw["connected"])
        campaign._objectives_announced = bool(raw["objectives_announced"])
        campaign.packages = {
            k: Package.from_dict(v) for k, v in raw["packages"].items()
        }
        # Replaces the tracker the constructor built, including the spawn ids
        # it allocated for targets; the saved counters above already account
        # for those, so nothing is reissued.
        campaign.tracker = AttritionTracker.from_dict(raw["tracker"])
        return campaign

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> Campaign:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
