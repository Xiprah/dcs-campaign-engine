# Design decisions

The choices that decide what *time*, *the enemy* and *a package* mean in this
engine. Each section says what was decided and why the obvious alternative was
rejected. docs/protocol.md is the wire contract; this is the model behind it.

## 1. Authority: exactly one source of truth per loss

The rule everything else rests on, in its complete form:

> A loss for an entity DCS is holding comes **only** from a `state` snapshot.
> A loss for an entity DCS is not holding comes **only** from the engine's own
> resolution, rolled on the campaign's seeded RNG.
> Events are never a source of loss, only of attribution.

The authority is decided **per entity, at the instant of resolution**, by
asking `tracker.is_instantiated(spawn_id)`. It is never both. A target hit on
paper and spawned later arrives carrying its damage, because a spawn sends its
current unit count; an aircraft that dies in DCS is never also rolled on paper.

Rejected: letting the engine "correct" a snapshot from its paper track. The
paper track is a prediction; the snapshot is an observation. An engine that
overrules observation with prediction can no longer be trusted about anything
it cannot see, and that is almost everything.

## 2. Time: two regimes, one clock

The campaign has a single non-decreasing clock in campaign seconds.

**Connected.** While a mission client is synced, *mission time is
authoritative*. The clock advances only from frame `t`, rebased onto campaign
time at each `hello`. The engine never advances itself here: if it did, the
clock would outrun the sim and the bubble would be computed against a war that
had moved on without DCS.

**Disconnected.** With no synced client, nobody is watching anything, so every
entity is outside the bubble and every outcome is the engine's to resolve on
paper (section 1). The engine advances its own clock through
`Campaign.advance(dt)`, in **fixed paper steps**.

The determinism guarantee survives this, and the reason is worth stating
exactly. The campaign only ever sees fixed-size steps. The state after N paper
steps from state S is a pure function of (S, N). The wall clock decides only
how fast N grows — that is the time-compression ratio — and never the content
of any step. Chunking, jitter and a slow machine change how far the war has
got at a given moment, never where it goes.

`advance` is a no-op while connected; the campaign enforces this itself rather
than trusting the transport to only call it at the right time. It accepts only
a whole number of steps and runs them one at a time, so `advance(3 * step)`
and three `advance(step)` calls are the same war.

**The step is 5 seconds** (`campaign.api.PAPER_STEP`), because that is the
resolution the connected war already has: the observer frame is the
campaign's heartbeat while DCS is attached, and it arrives every 5 s. Offline,
a takeoff, a TOT or an RTB lands at most one step late, the same as online. A
finer step buys precision the observed war never had; a coarser one lets a
flight overfly its TOT. A day of war is 17,280 steps, well under a second of
CPU. It is a constant, not a setting, because changing it changes what a
saved war does next.

A step **settles the present before it moves**: it pulses at the current
clock, then advances and pulses again. A pulse at an unchanged clock does
nothing the second time, so this is free whenever something already pulsed
there. When nothing has — a campaign just created or loaded — it stops the
result depending on whether the transport happened to tick before the first
step, which would otherwise decide whether the first package is planned now or
one step later.

**Observers do not survive the war moving on.** When DCS goes away the players
go with it. The last observer positions are kept through a disconnect, so a
quick restart re-issues the same bubble, but the first paper step clears them.
From then on they say where the players *were*: a bubble built from them
would keep instantiating things for nobody, and the next `hello` would
re-issue that stale picture to a mission whose players are somewhere else.
Clearing them empties the bubble through the ordinary despawn path, which
also marks every group as no longer held by DCS. That matters for a save
written by a process that was killed while connected: its groups still read
as instantiated, and without the despawn they would defer to snapshots that
can never arrive, so nothing offline could hit them or shoot at them. The
next observer frame rebuilds the bubble around where the players are now.

`connected` describes a socket in one process, so it is not saved. A campaign
loaded from disk has no client.

The transport paces offline advancement at `--time-compression N` (default 1:
the war carries on at real speed while you are away, which is the BMS feel; 0
makes the war wait for DCS, as the first slice did). It carries fractions of a
step between ticks rather than rounding them, so any chunking of the same wall
time yields the same number of steps. It runs at most 720 steps in one tick
and carries the rest, so a host waking from sleep catches up over a few ticks
instead of stalling a DCS that connects in that moment. Whatever is still owed
when a client says hello is dropped: from then on mission time owns the clock.
`python -m campaign --save war.json --simulate SECONDS` fast-forwards a save
with no server at all, for testing and for catching a war up overnight. It
runs whole steps; a remainder shorter than one step is not run.

Rejected: advancing on the wall clock directly. That makes the war's content
depend on scheduling jitter, and a campaign that cannot be replayed is a
campaign whose bugs cannot be reproduced.

Rejected: never advancing while disconnected, which is what the first slice
did. It was a determinism decision that silently traded away the single thing
that most distinguishes a BMS-style campaign: the war does not wait for you.

## 3. Threat: the enemy can hurt you where nobody is looking

Without this, an unobserved flight always comes home, and once the clock runs
offline, blue cannot lose a war it is not watching.

A **threat site** is an air-defence entity: strikable, instantiable in the
bubble like any other entity, with an engagement radius and a per-aircraft
kill probability. Threat sites are kept apart from strategic targets, so the
strike planner does not pick them; destroying them is DEAD work (section 5).

**Paper exposure.** At a package's TOT, for each flight *not* instantiated,
every live enemy threat site whose engagement radius the route enters rolls
once per aircraft. Survivors then release weapons. That order is deliberate:
an aircraft shot down on ingress does not bomb the target, which is what makes
suppressing the threat worth anything.

Every aircraft is rolled against every in-range site regardless of earlier
outcomes, for the same reason `resolve_strike` rolls every round: RNG
consumption must depend only on the situation, never on the dice, or a replay
diverges from the first early kill onward.

Paper flight losses go through the attrition tracker — decrementing the
tracked group *and* debiting the squadron reservation — so airframe
conservation holds exactly as it does for observed losses. A flight lost whole
on paper closes out the way one lost in a snapshot does. The tracker's paper
entry point refuses any group DCS is holding, so section 1 is enforced in the
tracker as well as by the caller. Paper damage to a *target* goes through the
same entry point. Before this it reached the ledger and the theater but not
the tracker, and a spawn reads its unit count from the tracker, so a target
damaged offline came back into DCS whole.

**The route** is the paper track's single leg, base to target. Egress retraces
it, so one pass through each envelope stands for the whole sortie. A site's
envelope is one ground radius around it.

**The authority is decided at the TOT**, as section 1 requires, and that has a
consequence worth knowing. A flight DCS held for part of its route but not at
its TOT is rolled for the whole route. A flight DCS holds at its TOT is not
rolled at all, even if it flew most of the way unwatched. Exposure is one
event, not a running integral along the track.

Kill probabilities are flat placeholders. A real threat model (altitude bands,
terrain masking, EW, per-type envelopes) replaces them behind the same seam:
`campaign.resolver.resolve_exposure` takes one probability per site and only
rolls them, so whatever computes those probabilities — including the
suppression a SEAD element buys (section 5) — changes nothing downstream.
`Theater.live_threats_along` is the question the SEAD planner asks ("is this
route exposed?"), and the enemy is taken from the flight's own coalition, not
from `player_coalition`, so a red flight will face blue's sites (section 4)
through the same code.

The slice has one threat site: an SA-6 battery (one radar, four launchers),
with invented coordinates like everything else on the map. It is placed about
5 km off the Incirlik–Latakia leg so the slice's only strike route runs
through its envelope.

## 4. The enemy: both sides fight

`player_coalition` means only *which side humans fly*. It decides who is told
what; it does not decide who plans.

Every coalition that has an inventory and airbases plans, in coalition-name
order so the sequence is deterministic. Red strikes blue's strategic targets,
faces blue's threat sites, and takes losses through exactly the same authority
rule. Each side may have one package open at a time until multi-package
deconfliction exists.

Rejected: a scripted red. An enemy that follows a script is a target range
with extra steps. The engine's red must be subject to the same inventory,
attrition and threat as blue, or nothing blue achieves means anything.

## 5. Packages: a set of flights with one TOT

A package is a set of **elements** — flights with a role (`strike`, `sead`) —
sharing one time on target. Each element is its own entity: its own spawn id,
its own reservation, its own bubble membership.

The planner attaches a **SEAD element** when the strike route is exposed to a
live enemy threat site and anti-radiation munitions are available. It arrives
ahead of the strikers. On paper, it rolls first: suppression cuts the site's
kill probability for the strike element, and its missiles may destroy site
units outright. Observed, it is tasked in DCS and the sim decides.

Rejected: modelling SEAD as a bonus on the strike flight. The point of a
package is that its parts can fail independently — the SEAD element can be
shot down, arrive late, or run dry — and a bonus cannot.

## Out of scope, deliberately

Multi-package deconfliction, escort and CAP, tankers and AWACS, the ground
war and front line, logistics and resupply, base capture, pilot records, and
real unit-template fidelity. Each has a `TODO(seam)` where it attaches. None
of them changes the decisions above.
