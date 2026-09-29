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
than trusting the transport to only call it at the right time.

The transport paces offline advancement at `--time-compression N` (default 1:
the war carries on at real speed while you are away, which is the BMS feel).
`python -m campaign --save war.json --simulate SECONDS` fast-forwards a save
with no server at all, for testing and for catching a war up overnight.

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
conservation holds exactly as it does for observed losses.

Kill probabilities are flat placeholders. A real threat model (altitude bands,
terrain masking, EW, per-type envelopes) replaces them behind the same seam.

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
