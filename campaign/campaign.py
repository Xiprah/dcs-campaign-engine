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
Time enters through frame `t` while a mission client is connected, and through
:meth:`Campaign.advance` in fixed paper steps while none is; chance enters
through `self.rng`, whose full state round-trips through `save`/`load`. A
campaign is therefore a pure function of its log and its paper-step count,
which is what makes a whole war replayable and a bug reproducible.
`tick(now)` is a bare pulse and its argument is deliberately unused -- see
:meth:`Campaign.tick`.

**Both sides fight.** Every coalition with squadrons and an airbase plans,
under the same inventory, attrition, threat and authority rules
(docs/design.md, section 4). `player_coalition` is only which side humans fly,
so it decides who is *told* what (:meth:`Campaign._tell`) and nothing else.

**A package is its elements.** Each flight of a package -- the strike, and a
SEAD element when one is attached -- is its own entity to the tracker, the
bubble, the inventory and the authority rule; the package ties them to one
target and one time on target (docs/design.md, section 5). Everything below
that touches a flight touches an element.

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
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from campaign.api import PAPER_STEP
from campaign.attrition import (
    KIND_FLIGHT,
    KIND_TARGET,
    KIND_THREAT,
    AttritionTracker,
    LossRecord,
)
from campaign.bubble import bubble_delta, resolve_bubble
from campaign.oob import SideInventory, Squadron, UnknownReservation, build_slice_oob
from campaign.resolver import (
    ARM_PK,
    resolve_exposure,
    resolve_strike,
    suppressed_kill_probability,
)
from campaign.planner import (
    ABORTED,
    COMPLETE,
    DESTROYED,
    ENROUTE,
    PLANNED,
    ROLE_SEAD,
    ROLE_STRIKE,
    Element,
    Package,
    build_package,
    element_heading,
    element_position,
    element_route,
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
from campaign.theater import (
    Target,
    Theater,
    ThreatSite,
    build_slice_theater,
    enemy_of,
)

#: Serialisation format of a saved campaign. Bump on any breaking change to
#: :meth:`Campaign.to_dict`.
#:
#: 3: the theater carries threat sites, and `connected` is no longer saved.
#: 4: packages carry their coalition, and `objectives_announced` is replaced
#:    by `war_result`, because the war can now be lost as well as won.
#: 5: a package is a list of elements, each with its own spawn id,
#:    reservation, schedule and state (docs/design.md, section 5).
SAVE_VERSION = 5

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
        #: A mission client has said hello and not gone away. Decides which
        #: clock regime the campaign is in (docs/design.md, section 2).
        #: Deliberately not saved: it describes a socket in this process, and
        #: a campaign loaded from disk has none. A save written mid-sortie
        #: that restored it as True would refuse every paper step until a
        #: client had come and gone -- and under `--simulate`, which never has
        #: one, for good.
        self.connected: bool = False
        #: Set once, when some side's strategic targets are all destroyed:
        #: {"t": campaign time, "defeated": [coalitions, in name order]}. Nobody
        #: plans after that. Persisted, because a war that ended must not start
        #: again on reload.
        self.war_result: dict[str, Any] | None = None
        #: Coalitions already told they cannot task anything. Not persisted;
        #: see _announce_dry.
        self._dry_announced: set[str] = set()
        #: Coalitions already told there is nothing to strike. Not persisted,
        #: for the same reason.
        self._no_targets_announced: set[str] = set()

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

    def _tell(self, audience: str | Iterable[str], text: str) -> list[Downlink]:
        """A message for `audience`, if humans fly for any side in it.

        `player_coalition` decides who hears what -- here, and in which words
        the war's end is announced -- and nothing else. A side nobody flies has
        no one to read a message, so it is sent none, and no seq is spent on
        one.
        """
        sides = {audience} if isinstance(audience, str) else set(audience)
        if self.player_coalition not in sides:
            return []
        return [
            Message(
                seq=self._seq(),
                t=self.mission_time(),
                to=self.player_coalition,
                text=text,
            )
        ]

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
            frames.extend(
                self._tell(
                    package.coalition,
                    f"{package.callsign} on task: "
                    f"{package.composition(sizes=False, open_only=True)} on "
                    f"{target.name if target else package.target_id}, "
                    f"TOT {self.mission_time(package.t_tot):.0f}.",
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
        frames = self._apply_losses(self.tracker.ingest(self._rebase(msg)))
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
                self._note_sim_contact()
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

    def advance(self, dt: float) -> list[Downlink]:
        """Move the war `dt` campaign seconds forward with nobody watching.

        The disconnected regime of docs/design.md, section 2. `dt` must be a
        whole number of :data:`campaign.api.PAPER_STEP`s, and the war moves
        one step at a time through the same pulse that frames drive, so the
        state after N steps is a function of the start state and N alone:
        `advance(3 * PAPER_STEP)` and three calls of `advance(PAPER_STEP)` are
        the same war. Anything else would let the wall clock's chunking into
        the campaign.

        A no-op while a client is connected, whoever calls it. Mission time is
        authoritative then, and a clock that also moved itself would run ahead
        of the sim and compute the bubble against a war DCS has not reached.
        """
        if dt < 0:
            raise ValueError(f"cannot advance by {dt}: time does not run backwards")
        steps = round(dt / PAPER_STEP)
        if abs(dt - steps * PAPER_STEP) > 1e-9 * max(1.0, abs(dt)):
            raise ValueError(
                f"cannot advance by {dt}: not a whole number of {PAPER_STEP}s paper steps"
            )
        if self.connected:
            return []
        frames: list[Downlink] = []
        for _ in range(steps):
            frames.extend(self._paper_step())
        return frames

    def _paper_step(self) -> list[Downlink]:
        # Nobody is connected, so nobody is anywhere. The last observer frame
        # describes where the players *were* when DCS went away; a bubble
        # built from it would keep instantiating things for no one, and the
        # next hello would re-issue that stale picture to a mission whose
        # players are somewhere else. Clearing it empties the bubble through
        # the ordinary despawn path, and the next observer frame rebuilds it.
        self.observers = []
        # Settle the present before leaving it. A pulse at an unchanged clock
        # does nothing the second time, so this costs nothing when a tick or
        # the previous step already pulsed here. When nothing has -- a
        # campaign just created or loaded -- it is what stops the result
        # depending on whether the transport happened to tick before the
        # first step: without it, that decides whether the first package is
        # planned now or one step later.
        frames = self._pulse()
        self.clock += PAPER_STEP
        frames.extend(self._pulse())
        return frames

    def _pulse(self) -> list[Downlink]:
        """Walk packages forward, task new ones, reconcile the bubble."""
        frames: list[Downlink] = []
        frames.extend(self._advance_packages())
        # A flight can now die on paper at its TOT as well as in a snapshot,
        # and it has to close out before the planner looks for an open
        # package, or its replacement waits a pulse for nothing.
        frames.extend(self._close_out_dead_packages())
        # Before planning, so no side frags a package in the pulse its war
        # ended in.
        frames.extend(self._check_war_end())
        frames.extend(self._plan())
        frames.extend(self._sync_bubble())
        return frames

    def on_disconnect(self) -> None:
        """DCS went away. Campaign belief stands; only instantiation is lost.

        `self.live` is kept on purpose: it is what the next `hello` re-issues.
        Observers are kept too, so the bubble does not collapse and rebuild in
        the seconds between the client reconnecting and its first observer
        frame -- but only until the war moves without DCS. The first paper
        step clears them (see `_paper_step`), because from then on they are
        where the players were, not where they are.
        """
        self.connected = False
        self.pending.clear()
        self.tracker.detach_all()

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------

    def _planning_coalitions(self) -> list[str]:
        """Every side that can put a package in the air, in name order.

        Name order, not dict order: the sides draw callsigns, package ids and
        spawn ids from shared sources in this order, and the order a save
        happened to list its inventories in is not something a replay may
        depend on. A side with no squadrons has nothing to plan with, and one
        with no airbase has nowhere to fly from.
        """
        return sorted(
            coalition
            for coalition, inventory in self.inventories.items()
            if inventory.squadrons and self.theater.airbases_of(coalition)
        )

    def _plan(self) -> list[Downlink]:
        """Let each side commit at most one package.

        docs/design.md, section 4: every side plans, under the same rules, and
        which side humans fly has no say in it.

        TODO(seam): multi-package deconfliction -- several packages in the air
        at once, sequenced on time and route -- replaces this "one at a time"
        rule. Out of scope for the slice.
        """
        if self.war_result is not None:
            return []
        frames: list[Downlink] = []
        for coalition in self._planning_coalitions():
            frames.extend(self._plan_for(coalition))
        return frames

    def _plan_for(self, coalition: str) -> list[Downlink]:
        """Commit one package for `coalition`, unless it has one open.

        The planner is shown the enemy sites whose envelopes the strike route
        enters, and attaches a SEAD element when there are any and the base
        has anti-radiation missiles (docs/design.md, section 5). The sites are
        the enemy of the planning side, never of `player_coalition`, so red
        escorts its raids by exactly the rule blue does.
        """
        if any(
            pkg.is_open and pkg.coalition == coalition
            for pkg in self.packages.values()
        ):
            return []
        target = select_target(self.theater, enemy_of(coalition))
        if target is None:
            return self._announce_no_targets(coalition)
        inventory = self.inventories[coalition]
        bases = sorted(self.theater.airbases_of(coalition), key=lambda b: b.id)
        for base in bases:
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
                threats=self.theater.live_threats_along(
                    enemy_of(coalition), base.pos, target.pos
                ),
                # Drawn only for an element actually attached, after the
                # strike's, so a package without one issues exactly the ids a
                # one-flight package did.
                next_spawn_id=self._next_spawn_id,
            )
            if package is None:
                # Nothing was issued, so give the ids back rather than leaving
                # a gap on every tick a stalled campaign fails to plan.
                self._package_counter -= 1
                self._spawn_counter -= 1
                continue
            self.packages[package.id] = package
            self._dry_announced.discard(coalition)
            # Tracked in the order their spawn ids were issued, strike first:
            # the tracker's insertion order is the ledger's, and a replay has
            # to write the same one.
            issued = [package.strike] + [
                e for e in package.elements if e.role != ROLE_STRIKE
            ]
            for element in issued:
                self.tracker.track(
                    element.spawn_id,
                    entity_id=package.id,
                    entity_kind=KIND_FLIGHT,
                    coalition=coalition,
                    units_initial=element.flight_size,
                )
            return self._tell(
                coalition,
                f"{package.callsign} fragged: {package.composition(sizes=True)} "
                f"on {target.name}, TOT {self.mission_time(package.t_tot):.0f}.",
            )
        return self._announce_dry(coalition, bases)

    def _announce_dry(self, coalition: str, bases: list[Any]) -> list[Downlink]:
        """Say so, once, when a side can no longer task anything.

        A campaign that quietly stops planning is indistinguishable from one
        with nothing to do. This is the difference between a war that ended and
        a war that ran out of bombs, and the save file looks identical either
        way. Deliberately not persisted: repeating it once after a reload is a
        far smaller sin than a silent stall.

        Said only after every base has been tried. Saying it at the first dry
        base and stopping there would make a side that is told plan
        differently from one that is not, and who is listening may not decide
        what gets planned.
        """
        if coalition in self._dry_announced:
            return []
        self._dry_announced.add(coalition)
        names = ", ".join(base.name for base in bases)
        verb = "has" if len(bases) == 1 else "have"
        return self._tell(
            coalition,
            f"Cannot task: {names} {verb} no aircraft or ordnance available.",
        )

    def _announce_no_targets(self, coalition: str) -> list[Downlink]:
        """A side whose enemy never held a strategic target has none to strike.

        Not the end of the war -- that is `_check_war_end`, for a side whose
        targets were all *destroyed* -- only a map with nothing on it for this
        side to do. Said once, and not persisted, like `_announce_dry`.
        """
        if coalition in self._no_targets_announced:
            return []
        self._no_targets_announced.add(coalition)
        return self._tell(coalition, "No enemy strategic targets to task.")

    def _check_war_end(self) -> list[Downlink]:
        """End the war, once, when some side's strategic targets are all gone.

        Strategic targets are what each side is fighting for, so a side left
        with none has lost -- whichever side that is, and whichever side the
        humans fly. Both sides can lose their last target in the same pulse;
        that is a war without a victor, not a win for whichever sorts first.

        Nobody plans after this, and a package still on the ground is stood
        down with its reservation returned. One already airborne flies out
        its sortie as fragged, because there is no recalling it the same way
        in both regimes: the protocol has no re-tasking frame, so a flight DCS
        is holding keeps its attack task whatever the engine decides, and a
        recall that only worked on paper would make the watched and unwatched
        wars obey different rules. What it achieves is recorded like anything
        else, but the result is fixed here, once.
        """
        if self.war_result is not None:
            return []
        defeated = self.theater.defeated_coalitions()
        if not defeated:
            return []
        self.war_result = {"t": self.clock, "defeated": defeated}
        if self.player_coalition not in defeated:
            text = "All assigned strategic targets destroyed."
        elif len(defeated) == 1:
            text = "All our strategic targets have been destroyed. The war is lost."
        else:
            text = (
                "Every strategic target on both sides has been destroyed. "
                "The war ends without a victor."
            )
        frames = self._tell(self.player_coalition, text)
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            # Element by element, for the same reason an airborne package is
            # not recalled: a SEAD element already in the air cannot be called
            # back in DCS, so it flies out its sortie while the strike still
            # on the ramp behind it is stood down.
            for element in package.elements:
                if element.state != PLANNED:
                    continue
                element.state = ABORTED
                frames.extend(self._retire(element.spawn_id, "stood_down"))
                self._settle(package, element)
                self.tracker.forget(element.spawn_id)
                frames.extend(
                    self._tell(
                        package.coalition,
                        f"{package.name_of(element)} stood down: the war is over.",
                    )
                )
            package.settle_state()
        return frames

    def _advance_packages(self) -> list[Downlink]:
        """Walk open packages forward, each element on its own paper track."""
        frames: list[Downlink] = []
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            if not package.is_open:
                continue
            for element in package.elements:
                if element.state == PLANNED and self.clock >= element.t_takeoff:
                    element.state = ENROUTE
            package.settle_state()
            if not package.weapons_released and self.clock >= package.t_tot:
                frames.extend(self._resolve_tot(package))
            for element in package.elements:
                if element.is_open and self.clock >= element.t_rtb:
                    frames.extend(self._complete_element(package, element))
            package.settle_state()
        return frames

    def _resolve_tot(self, package: Package) -> list[Downlink]:
        """Resolve every element's part in the package's time on target.

        docs/design.md, section 5, in this order: the SEAD element flies its
        exposure at the sites' full kill probability, because it goes in
        first; its survivors' missiles are resolved against the sites and
        may destroy units; the suppression they bought cuts the sites' kill
        probability for the strike element; the strike element flies its
        exposure; its survivors release. Each step reads what the one before
        it left, so a site the SEAD element destroyed does not fire at the
        strikers at all.

        Authority is decided per entity, here, exactly as section 1 says. An
        element DCS is holding is never flown through the sites on paper, and
        its missiles or bombs are resolved by the sim; a site DCS is holding
        loses units only to snapshots. The consequences for suppression are
        spelled out in `_suppress`.

        Expenditure is the engine's own fact whoever holds what: each
        element's survivors expend what they carried, here, once.
        """
        package.weapons_released = True
        frames: list[Downlink] = []
        #: Site id -> SEAD aircraft that engaged it on paper.
        suppression: dict[str, int] = {}
        sead = package.element(ROLE_SEAD)
        if sead is not None and sead.is_open:
            frames.extend(self._expose(package, sead, {}))
            survivors = self._release(package, sead)
            if survivors > 0:
                frames.extend(self._suppress(package, sead, survivors, suppression))
                frames.extend(self._off_target(package, sead, survivors))
        strike = package.element(ROLE_STRIKE)
        if strike is not None and strike.is_open:
            frames.extend(self._expose(package, strike, suppression))
            survivors = self._release(package, strike)
            if survivors > 0:
                frames.extend(self._resolve_unobserved(package, strike, survivors))
                frames.extend(self._off_target(package, strike, survivors))
        # An element with no survivors is closed out by
        # `_close_out_dead_packages`, the path every dead flight takes,
        # observed or not.
        return frames

    def _release(self, package: Package, element: Element) -> int:
        """Expend what the element's surviving aircraft carried; return them.

        Expenditure comes off the paper track, not off a snapshot, because it
        is the engine's own fact: the engine knows what it loaded and what
        reached the target. What the ordnance *achieved* is another matter,
        with exactly one authority (`_resolve_unobserved`, `_suppress`).
        """
        element.weapons_released = True
        survivors = self.tracker.units_alive(element.spawn_id)
        squadron = self._squadron_for(package, element)
        if squadron is not None and element.reservation_id in squadron.open_reservations:
            squadron.debit_munitions(
                element.reservation_id,
                survivors * element.rounds_per_aircraft,
                lost=False,
            )
        return survivors

    def _off_target(self, package: Package, element: Element, survivors: int) -> list[Downlink]:
        return self._tell(
            package.coalition,
            f"{package.name_of(element)} off target, {survivors} aircraft egressing.",
        )

    def _route_threats(self, package: Package) -> list[ThreatSite]:
        """The enemy's live sites whose envelopes the package's route enters.

        The route is the paper track's single leg, base to target, shared by
        every element; egress retraces it, so one pass through each envelope
        stands for the sortie. The enemy is the package's own coalition's,
        never `player_coalition`'s, so a red raid meets blue's air defences
        through exactly the path a blue strike meets red's.
        """
        base = self.theater.airbases.get(package.base_id)
        target = self.theater.targets.get(package.target_id)
        if base is None or target is None:
            return []
        return self.theater.live_threats_along(
            enemy_of(package.coalition), base.pos, target.pos
        )

    def _expose(
        self, package: Package, element: Element, suppression: dict[str, int]
    ) -> list[Downlink]:
        """Fly an unwatched element through the air defences on its route.

        docs/design.md, section 3. Called at the TOT, before release, and only
        for an element DCS is not holding: one it is holding is shot at by
        DCS, and its losses come from snapshots and nowhere else.

        `suppression` is what a SEAD element bought against each site, and
        only reaches the kill probability: the draws are the same whether or
        not anyone suppressed anything.
        """
        if self.tracker.is_instantiated(element.spawn_id):
            return []  # DCS has it; the snapshot is the authority.
        group = self.tracker.groups.get(element.spawn_id)
        if group is None:
            return []
        sites = self._route_threats(package)
        outcome = resolve_exposure(
            aircraft=group.units_alive,
            kill_probabilities=[
                suppressed_kill_probability(
                    site.kill_probability, suppression.get(site.id, 0)
                )
                for site in sites
            ],
            rng=self.rng,
        )
        losses = self.tracker.record_unobserved(
            element.spawn_id, outcome.aircraft_lost, self.clock
        )
        frames = self._apply_losses(losses)
        if losses:
            frames.extend(
                self._tell(
                    package.coalition,
                    f"{package.name_of(element)} lost {len(losses)} aircraft to "
                    f"air defences inbound.",
                )
            )
            # The defending side's own batteries fired, so it knows what they
            # claimed. Only on paper: a flight DCS shot down was shot down in
            # front of whoever was in the bubble, and the engine has nothing
            # but event attribution -- which must never change what is sent --
            # to say who did it.
            frames.extend(
                self._tell(
                    enemy_of(package.coalition),
                    f"{', '.join(site.name for site in sites)} engaged: "
                    f"{len(losses)} enemy aircraft down.",
                )
            )
        return frames

    def _suppress(
        self,
        package: Package,
        sead: Element,
        survivors: int,
        suppression: dict[str, int],
    ) -> list[Downlink]:
        """What a SEAD element's survivors did to the sites, on paper.

        The rule for mixed authority (docs/design.md, section 5): SEAD has a
        paper effect -- missiles rolled against a site, and suppression of
        that site for the strike element -- only when **both** the SEAD
        element and the site are outside DCS at the TOT.

        * A SEAD element DCS is holding is the sim's. Whatever it did, the
          only trace a snapshot can carry is site units destroyed, and those
          arrive through `ingest` like any other observed loss. Suppression is
          not a thing a snapshot can show, so none is inferred: an unwatched
          strike behind a watched SEAD element meets the sites at their full
          kill probability, less only the units the sim reported destroyed.
          Crediting both would count one SEAD sortie's effect twice.
        * A site DCS is holding is the sim's too. Paper missiles cannot take
          units off it -- the tracker would refuse them -- and a paper SEAD
          element cannot have shut down a radar that is being simulated, so
          it buys no suppression against that site either.
        * A strike element DCS is holding is never rolled, so suppression has
          nothing to act on; the missiles against paper sites still land.
        * A site DCS held at the same time as the SEAD element, at any moment
          before this TOT, is excluded even though both are on paper now
          (`_note_sim_contact`). The client tasks a SEAD element to engage
          whatever air defence it meets, not at a waypoint, so sharing the sim
          with the site was the sim's chance to fire those missiles, and what
          they did arrived by snapshot. Rolling them again here would resolve
          one element's missiles twice. A strike element has no such rule:
          its attack is hung on the waypoint it reaches at its TOT, so the
          sim resolves its bombs only if it holds the strike then.

        The missiles are shared out over the paper sites in id order, round
        robin, and every one is rolled even once its site has nothing left to
        lose (`resolve_strike`), so the draws depend on how many were fired
        and at how many sites -- never on how the dice fall.
        """
        if self.tracker.is_instantiated(sead.spawn_id):
            return []
        sites = [
            site
            for site in self._route_threats(package)
            if not self.tracker.is_instantiated(site.spawn_id)
            and site.id not in sead.sim_contact
        ]
        if not sites:
            return []
        rounds = survivors * sead.rounds_per_aircraft
        frames: list[Downlink] = []
        for index, site in enumerate(sites):
            share = rounds // len(sites) + (1 if index < rounds % len(sites) else 0)
            if share <= 0:
                continue
            suppression[site.id] = survivors
            outcome = resolve_strike(
                rounds=share,
                target_units_alive=site.units_alive,
                rng=self.rng,
                weapon_pk=ARM_PK,
            )
            frames.extend(
                self._apply_losses(
                    self.tracker.record_unobserved(
                        site.spawn_id, outcome.units_killed, self.clock
                    )
                )
            )
        return frames

    def _note_sim_contact(self) -> None:
        """Record every enemy site DCS holds alongside an unreleased SEAD element.

        Called whenever the client acknowledges a spawn, because that is the
        only moment two entities can begin to be held at once: `instantiated`
        is set by an ack and by nothing else. Recording there catches every
        stretch of shared sim time however short, with no sampling, and from
        frames alone, so a replay of the same log records the same contact.
        Events play no part.
        """
        held = [
            site
            for site in sorted(self.theater.threats.values(), key=lambda s: s.id)
            if site.spawn_id and self.tracker.is_instantiated(site.spawn_id)
        ]
        if not held:
            return
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            sead = package.element(ROLE_SEAD)
            if (
                sead is None
                or not sead.is_open
                or sead.weapons_released
                or not self.tracker.is_instantiated(sead.spawn_id)
            ):
                continue
            enemy = enemy_of(package.coalition)
            contact = set(sead.sim_contact)
            contact.update(site.id for site in held if site.coalition == enemy)
            sead.sim_contact = sorted(contact)

    def _resolve_unobserved(
        self, package: Package, element: Element, survivors: int
    ) -> list[Downlink]:
        """Damage a target nobody was watching. No-op if DCS holds it.

        What the ordnance achieved has two possible authorities, and exactly
        one of them applies. If DCS is holding the target at this instant, the
        sim decides and the answer arrives in a snapshot. If it is not, nobody
        is watching, and `campaign.resolver` rolls the outcome on `self.rng`.
        Choosing once, here, is what keeps a target from being killed twice.
        """
        target = self.theater.targets.get(package.target_id)
        if target is None or target.destroyed:
            return []
        if self.tracker.is_instantiated(target.spawn_id):
            return []  # DCS has it; the snapshot is the authority.
        outcome = resolve_strike(
            rounds=survivors * element.rounds_per_aircraft,
            target_units_alive=target.units_alive,
            rng=self.rng,
        )
        # Through the tracker, not straight onto the ledger: a target damaged
        # here and spawned later must come into DCS with only what survived,
        # and the spawn reads its unit count from the tracker.
        frames = self._apply_losses(
            self.tracker.record_unobserved(
                target.spawn_id, outcome.units_killed, self.clock
            )
        )
        if outcome.missed:
            frames.extend(
                self._tell(
                    package.coalition,
                    f"{package.name_of(element)} reports no effect on target.",
                )
            )
        return frames

    def _complete_element(self, package: Package, element: Element) -> list[Downlink]:
        element.state = COMPLETE
        frames = self._retire(element.spawn_id, "mission_complete")
        self._settle(package, element)
        survivors = self.tracker.units_alive(element.spawn_id)
        self.tracker.forget(element.spawn_id)
        frames.extend(
            self._tell(
                package.coalition,
                f"{package.name_of(element)} recovered, {survivors} aircraft home.",
            )
        )
        return frames

    def _close_out_dead_packages(self) -> list[Downlink]:
        """Close every element with nobody left alive in it, and say so.

        Per element: a SEAD element shot down whole is lost, and the strike
        it was escorting flies on. The package closes when its last element
        does.
        """
        frames: list[Downlink] = []
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            if not package.is_open:
                continue
            for element in package.elements:
                if not element.is_open or self.tracker.is_alive(element.spawn_id):
                    continue
                element.state = DESTROYED
                frames.extend(self._retire(element.spawn_id, "destroyed"))
                self._settle(package, element)
                self.tracker.forget(element.spawn_id)
                frames.extend(
                    self._tell(package.coalition, f"{package.name_of(element)} is lost.")
                )
            package.settle_state()
        return frames

    def _settle(self, package: Package, element: Element) -> None:
        """Close an element's reservation, returning anything still held."""
        squadron = self._squadron_for(package, element)
        if squadron is None:
            return
        try:
            squadron.release(element.reservation_id)
        except UnknownReservation:
            return

    def _squadron_for(self, package: Package, element: Element) -> Squadron | None:
        """The squadron an element draws on: its own side's, whoever flies it."""
        inventory = self.inventories.get(package.coalition)
        if inventory is None:
            return None
        return inventory.squadrons.get(element.squadron_id)

    def _find_element(self, spawn_id: str) -> tuple[Package, Element] | None:
        """The package and element a flight's spawn id belongs to, if any.

        Packages in id order: spawn ids are unique, but a lookup whose answer
        could depend on dict order is one refactor away from a replay bug.
        """
        for package in sorted(self.packages.values(), key=lambda p: p.id):
            element = package.element_for(spawn_id)
            if element is not None:
                return package, element
        return None

    # ------------------------------------------------------------------
    # loss application
    # ------------------------------------------------------------------

    def _apply_losses(self, losses: list[LossRecord]) -> list[Downlink]:
        """Apply a batch of losses, then say what it did, once per entity.

        One snapshot or one paper strike can take several units off one
        target, and its owner is told once per strike rather than once per
        bomb. Entities are reported in the order of their first loss, which
        the tracker already makes deterministic.
        """
        frames: list[Downlink] = []
        counts: dict[tuple[str, str], int] = {}
        for loss in losses:
            frames.extend(self._apply_loss(loss))
            key = (loss.entity_kind, loss.entity_id)
            counts[key] = counts.get(key, 0) + 1
        for (kind, entity_id), count in counts.items():
            frames.extend(self._report_damage(kind, entity_id, count))
        return frames

    def _report_damage(self, kind: str, entity_id: str, count: int) -> list[Downlink]:
        """Tell whoever could plausibly know what happened to a fixed entity.

        Its owner knows it was hit, whoever hit it and whether or not anyone
        was watching, and is told how much is left. The other side learns
        only what a strike's bomb damage assessment would give it: that the
        thing is gone. Neither is told who did it -- that would come from
        event attribution, and a message is a frame, so it would make the
        frame stream depend on events.

        Flights are not reported here. Their own side hears about them through
        the package's messages, and the enemy through `_expose`.
        """
        entity: Target | ThreatSite | None
        if kind == KIND_TARGET:
            entity = self.theater.targets.get(entity_id)
        elif kind == KIND_THREAT:
            entity = self.theater.threats.get(entity_id)
        else:
            return []
        if entity is None:
            return []
        if entity.destroyed:
            return self._tell(
                {entity.coalition, enemy_of(entity.coalition)},
                f"{entity.name} destroyed.",
            )
        return self._tell(
            entity.coalition,
            f"{entity.name} hit: {count} unit(s) destroyed, "
            f"{entity.units_alive} of {entity.units_initial} remaining.",
        )

    def _apply_loss(self, loss: LossRecord) -> list[Downlink]:
        if loss.entity_kind == KIND_FLIGHT:
            return self._apply_flight_loss(loss)
        if loss.entity_kind == KIND_TARGET:
            return self._apply_target_loss(loss)
        if loss.entity_kind == KIND_THREAT:
            return self._apply_threat_loss(loss)
        return []

    def _apply_flight_loss(self, loss: LossRecord) -> list[Downlink]:
        # A flight's ledger entry names its package; its spawn id names the
        # element, whose reservation the airframe comes out of.
        package = self.packages.get(loss.entity_id)
        if package is None:
            return []
        element = package.element_for(loss.spawn_id)
        if element is None:
            return []
        squadron = self._squadron_for(package, element)
        if squadron is None:
            return []
        if element.reservation_id not in squadron.open_reservations:
            return []
        squadron.debit_airframes(element.reservation_id, 1)
        if not element.weapons_released:
            # The jet took its ordnance into the ground with it. Still
            # conserved, just not the same fact about the war as bombs on a
            # target or missiles on a radar.
            squadron.debit_munitions(
                element.reservation_id, element.rounds_per_aircraft, lost=True
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
        return frames

    def _apply_threat_loss(self, loss: LossRecord) -> list[Downlink]:
        site = self.theater.threats.get(loss.entity_id)
        if site is None:
            return []
        site.units_alive = max(0, site.units_alive - 1)
        if not site.destroyed:
            return []
        frames = self._retire(site.spawn_id, "destroyed")
        self.tracker.forget(site.spawn_id)
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
            # Each element on its own: the SEAD element is in the air before
            # the strike and home before it, and may be in the bubble while
            # the strike is not.
            for element in package.elements:
                if element.spawn_id in self.blocked:
                    continue
                if not element.is_airborne(self.clock):
                    continue
                if not self.tracker.is_alive(element.spawn_id):
                    continue
                placement = self._element_placement(package, element)
                if placement is None:
                    continue
                out[element.spawn_id] = placement[0]
        for target in self.theater.targets.values():
            if target.destroyed or not target.spawn_id:
                continue
            if target.spawn_id in self.blocked:
                continue
            out[target.spawn_id] = target.pos
        for site in self.theater.threats.values():
            if site.destroyed or not site.spawn_id:
                continue
            if site.spawn_id in self.blocked:
                continue
            out[site.spawn_id] = site.pos
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
        """The client refused a spawn. Unwind rather than leak.

        Only the element whose spawn was refused is scrubbed. A bad SEAD
        template leaves the strike to fly on alone, as it would have had the
        base had no missiles; the package closes when its last element does.
        """
        self.live.discard(spawn_id)
        self.blocked.add(spawn_id)
        self.tracker.mark_removed(spawn_id)
        found = self._find_element(spawn_id)
        if found is None:
            return []
        package, element = found
        if not element.is_open:
            return []
        element.state = ABORTED
        self._settle(package, element)
        self.tracker.forget(spawn_id)
        package.settle_state()
        return self._tell(
            package.coalition,
            f"{package.name_of(element)} scrubbed: client rejected spawn.",
        )

    # ------------------------------------------------------------------
    # frame construction
    # ------------------------------------------------------------------

    def _element_placement(
        self, package: Package, element: Element
    ) -> tuple[Vec3, float] | None:
        base = self.theater.airbases.get(package.base_id)
        target = self.theater.targets.get(package.target_id)
        if base is None or target is None:
            return None
        return (
            element_position(element, base, target, self.clock),
            element_heading(element, base, target, self.clock),
        )

    def _spawn_frame(self, spawn_id: str) -> Spawn | None:
        found = self._find_element(spawn_id)
        if found is not None and found[1].is_open:
            return self._flight_spawn_frame(*found)
        for target in self.theater.targets.values():
            if target.spawn_id == spawn_id and not target.destroyed:
                return self._target_spawn_frame(target)
        for site in self.theater.threats.values():
            if site.spawn_id == spawn_id and not site.destroyed:
                return self._threat_spawn_frame(site)
        return None

    def _flight_spawn_frame(self, package: Package, element: Element) -> Spawn | None:
        base = self.theater.airbases.get(package.base_id)
        target = self.theater.targets.get(package.target_id)
        squadron = self._squadron_for(package, element)
        placement = self._element_placement(package, element)
        if base is None or target is None or squadron is None or placement is None:
            return None
        position, heading = placement
        seq = self._seq()
        self.pending[seq] = (_SPAWN, element.spawn_id)
        return Spawn(
            seq=seq,
            t=self.mission_time(),
            ref=seq,
            spawn_id=element.spawn_id,
            coalition=squadron.coalition,
            category="plane",
            template=squadron.template,
            # What attrition has recorded, not the element's fragged size: a
            # flight that re-enters the bubble after losing a wingman must not
            # come back whole.
            units=self.tracker.units_alive(element.spawn_id),
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
                for pos, alt, speed, action in element_route(
                    element, base, target, self.clock
                )
            ],
            tasking=self._tasking(package, element, target),
        )

    def _tasking(self, package: Package, element: Element, target: Target) -> dict:
        """What the element is for, in the shape docs/protocol.md gives its role.

        A SEAD element is sent after the sites its route enters as they stand
        when it spawns, by DCS group name. Any of them may not be instantiated
        -- the client then relies on its search-and-engage task alone -- and a
        site destroyed since is not named at all.

        An element whose part in the TOT is already resolved is sent home with
        no task at all. Its ordnance is spent, and wherever that was decided
        -- on paper or in the sim -- it was decided once. A flight that
        re-enters the bubble on its way home would otherwise be tasked to
        attack again: the client hangs the attack on the last waypoint when
        there is no attack waypoint left, and a strike resolved on paper at
        its TOT would bomb the same target a second time in DCS, the
        snapshot booking that second strike's damage as well. `egress` is a
        kind the client does not recognise, which docs/protocol.md says
        carries no task.
        """
        tot = self.mission_time(element.t_tot)
        callsign = package.name_of(element)
        if element.weapons_released:
            return {"kind": "egress", "callsign": callsign}
        if element.role == ROLE_SEAD:
            return {
                "kind": "sead",
                "targets": [
                    group_name(site.spawn_id)
                    for site in self._route_threats(package)
                    if site.spawn_id
                ],
                "tot": tot,
                "callsign": callsign,
            }
        return {
            "kind": "strike",
            "target": group_name(target.spawn_id) if target.spawn_id else target.id,
            "tot": tot,
            "callsign": callsign,
        }

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

    def _threat_spawn_frame(self, site: ThreatSite) -> Spawn:
        seq = self._seq()
        self.pending[seq] = (_SPAWN, site.spawn_id)
        return Spawn(
            seq=seq,
            t=self.mission_time(),
            ref=seq,
            spawn_id=site.spawn_id,
            coalition=site.coalition,
            category=site.category,
            template=site.template,
            # As for a target: a battery that lost launchers comes back without
            # them, or it would have to be suppressed twice.
            units=self.tracker.units_alive(site.spawn_id),
            position=site.pos,
            heading=0.0,
            route=[],
            tasking={"kind": "air_defence"},
        )

    def _ensure_targets_tracked(self) -> None:
        """Give every standing theater entity an identity and a tracked group.

        Targets first, then threat sites, each in id order, so spawn ids come
        out the same on every fresh campaign.
        """
        entities: list[tuple[Target | ThreatSite, str]] = [
            (target, KIND_TARGET)
            for target in sorted(self.theater.targets.values(), key=lambda t: t.id)
        ]
        entities += [
            (site, KIND_THREAT)
            for site in sorted(self.theater.threats.values(), key=lambda s: s.id)
        ]
        for entity, kind in entities:
            if entity.destroyed:
                continue
            if not entity.spawn_id:
                entity.spawn_id = self._next_spawn_id()
            if entity.spawn_id not in self.tracker.groups:
                self.tracker.track(
                    entity.spawn_id,
                    entity_id=entity.id,
                    entity_kind=kind,
                    coalition=entity.coalition,
                    units_initial=entity.units_initial,
                    units_alive=entity.units_alive,
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
            "war_result": self.war_result,
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
        result = raw["war_result"]
        campaign.war_result = (
            None
            if result is None
            else {"t": float(result["t"]), "defeated": list(result["defeated"])}
        )
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
