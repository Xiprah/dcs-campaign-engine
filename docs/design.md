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
strike planner does not pick them. Destroying them for their own sake is DEAD
work, not built; a SEAD element's missiles can take units off one on the way
(section 5).

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
from `player_coalition`, so a red flight faces blue's sites (section 4)
through the same code.

The slice has one threat site a side, with invented coordinates like
everything else on the map. Red's is an SA-6 battery (one radar, four
launchers) about 5 km off the Incirlik–Latakia leg; blue's is a Patriot
battery (one radar, four launchers) about 5 km off the Bassel al-Assad–Incirlik
leg. Each side's only strike route runs through the other side's only
envelope, and both carry the same placeholder kill probability, so neither
side's air defence is tuned against the other by numbers nobody has measured.
Their radii are not the same: each is the system's published maximum range,
24 km for the SA-6 and 160 km for the Patriot (`theater.py` names the
sources), because section 5 compares them with the published maximum ranges
of the missiles fired at them, and a trimmed radius would decide that
comparison by the trim. The Patriot's envelope takes in Bassel al-Assad
itself. On paper that changes nothing for a strike, which is rolled once per
site whatever the depth of the envelope it crosses.

## 4. The enemy: both sides fight

`player_coalition` means only *which side humans fly*. It decides who is told
what; it does not decide who plans.

Every coalition that has an inventory and airbases plans, in coalition-name
order so the sequence is deterministic. "An inventory" means at least one
squadron: a side with none has nothing to plan with. The order matters beyond
tidiness, because the sides draw package ids, spawn ids and callsigns from
shared sources in it, and the order a save happens to list its inventories in
must not decide a replay. Red strikes blue's strategic targets, faces blue's
threat sites, and takes losses through exactly the same authority rule. Each
side may have one package open at a time until multi-package deconfliction
exists.

A package records the side that flies it. That, never `player_coalition`,
decides whose squadron it draws on, whose threat sites it is exposed to, and
who hears about it. Planning tries every one of a side's bases before it says
the side cannot task anything: stopping at the first dry base to announce it
would make the side that is listened to plan differently from the side that
is not.

**Who is told what.** Only the side humans fly is sent messages; a side
nobody flies has nobody to read them, and is sent none. That side hears what
it could plausibly know, and nothing that would come from event attribution
(a message is a frame, and frames may not depend on events):

- everything about its own packages, as before: fragged, on task, off
  target, losses to air defences, recovered, lost, scrubbed, stood down;
- damage to its own strategic targets and threat sites, whoever caused it
  and whether or not anyone was watching, with what is left standing — but
  not who did it;
- the destruction of an enemy target or site: its own strike's bomb damage
  assessment;
- enemy aircraft its own air defences shot down on paper. Not those DCS shot
  down: those died in front of whoever was in the bubble, and the engine has
  only event attribution to say who killed them;
- never the enemy's tasking, callsigns, launches or recoveries. There is no
  intelligence or early-warning model to learn them from (AWACS is out of
  scope).

**The end of the war.** Strategic targets are what each side fights for, so a
side that held some and has none left has lost. The war ends the first time
that is true of any side, and the result — the time and the defeated sides —
is saved, so a reloaded war stays over. Both sides losing their last target
in the same pulse is a war without a victor, not a win for whichever sorts
first. The humans are told once: that their assigned targets are all
destroyed, that their own are and the war is lost, or that both are.

After the end nobody plans. A package still on the ground is stood down and
its reservation returned. One already airborne flies out its sortie as
fragged, and what it achieves is recorded, but the result is already fixed.
It is not recalled because the two regimes could not recall it alike: the
protocol has no re-tasking frame, so a flight DCS is holding keeps its attack
task whatever the engine decides, and a recall that worked only on paper
would make the watched and unwatched wars obey different rules. Since
packages became sets of elements (section 5) this is decided element by
element, for the same reason: a SEAD element already in the air flies out
its sortie while the strike still on the ramp behind it is stood down.

A side whose enemy never held a strategic target is not at war with anyone
the map can express. It plans nothing and, if humans fly it, is told so once.

Rejected: a scripted red. An enemy that follows a script is a target range
with extra steps. The engine's red must be subject to the same inventory,
attrition and threat as blue, or nothing blue achieves means anything.

Rejected: letting the war run on once one side's targets are gone, the beaten
side raiding until the winner's are gone as well. That ends, if it ends, with
two victors, and turns a war the humans won into one they can lose afterwards
to an enemy with nothing left to fight for.

## 5. Packages: a set of flights with one TOT

A package is a set of **elements** — flights with a role (`strike`, `sead`) —
sharing one time on target. Each element is its own entity: its own spawn id,
its own reservation, its own bubble membership.

The planner attaches a **SEAD element** when the strike route is exposed to a
live enemy threat site and anti-radiation munitions are available. It arrives
ahead of the strikers. On paper, it rolls first: suppression cuts the site's
kill probability for the strike element, and its missiles may destroy site
units outright. It is shot at only by a site it cannot out-range: against
one its missile out-ranges it fires from standoff. Observed, it is tasked in
DCS and the sim decides.

**Elements.** Each element has its own spawn id, its own reservation
(`<package>-<role>`), its own schedule and its own state, and is tracked on
its own under its package's id: the ledger names the package, the spawn id
names the element. The strike element's spawn id is issued first and a
supporting element's only if one is attached, so a package without one draws
exactly the ids, and the dice, the one-flight package did; a test holds
eleven recorded pre-SEAD wars to that, frame for frame. A package is open
while any element is. When none is, it is complete if any element came home,
destroyed if none did and one was lost, and aborted if every one was scrubbed
or stood down. Each side still holds one open package at a time, so a
package whose strike element is lost stays open until its SEAD element is
home.

**When one is attached.** At planning the planner is shown the enemy sites
whose envelopes the base-to-target leg enters (`Theater.live_threats_along`,
the enemy being the planning side's, never `player_coalition`'s). It attaches
a SEAD two-ship, two missiles an aircraft, when there are any and a squadron
at the same base holds that many anti-radiation munitions
(`oob.ANTI_RADIATION_MUNITIONS`). The same base, because the two elements
share one paper track. A squadron that carries only anti-radiation missiles
never flies a strike, and a side whose SEAD squadron has run dry sends its
strikes alone. Attaching throws no dice.

**Content: red gets anti-radiation missiles.** Each side has a SEAD squadron
at its one base: F-16Cs with AGM-88C at Incirlik, Su-24Ms with Kh-58U at
Bassel al-Assad, eight airframes and twenty-four missiles each. The Su-24M
really does carry the Kh-58U, and giving blue SEAD and red none would tilt
the war by content nobody has measured, which section 4 already refused to do
with the strike squadrons.

**The lead is 120 seconds** (`planner.SEAD_LEAD`), on the same track: the SEAD
element's takeoff, time over the target and recovery are the strike's less
120 s. At the paper track's ground speed of about 140 m/s that is 17 km.
Blue's strikers cross into the SA-6's 24 km envelope 339 s before their TOT,
when the SEAD element ahead of them is 8 km from the battery and closing on
its abeam point, 293 s out, as close as it gets and with the shortest shot it
has. Red's route is inside the Patriot's 160 km envelope from takeoff, and
the same lead keeps its SEAD element 17 km ahead of the strikers all the way
in. Much more and the SEAD element is off the target
and turning for home while the strikers are still inbound; much less and the
two are one formation that each envelope meets at once. It is a constant,
not derived from each route's geometry, because on paper exposure is one
event at the TOT (section 3) and the lead does not enter that arithmetic: it
decides where the SEAD element is on its paper track, which is when the
bubble holds it and where DCS flies it.

**The paper order.** Everything is resolved once, at the package's TOT, in
this order:

1. the SEAD element flies its exposure to every site it cannot out-range
   (below), at the site's full kill probability, because it goes in first;
2. its survivors' missiles are rolled against the sites, shared round-robin
   in site-id order, each removing a unit at `resolver.ARM_PK` (0.25),
   through the tracker like any paper loss;
3. every surviving SEAD aircraft halves the kill probability of each site it
   engaged, compounding (`resolver.SEAD_SUPPRESSION_PER_AIRCRAFT`), so a SEAD
   element that lost a jet only half does its job;
4. the strike element flies its exposure against the sites still standing,
   at those probabilities: a site the missiles destroyed does not fire;
5. the strike element's survivors release.

Each element's survivors expend what they carried, there, whoever held what:
expenditure is the engine's own fact. `resolve_exposure` rolls the
probabilities it is given, so suppression changes its inputs and nothing
downstream.

**Standoff.** A SEAD element is exposed to a site on paper only if it has to
enter that site's envelope to get a shot at it: only when the site's
engagement radius is at least its missile's launch range
(`oob.ANTI_RADIATION_LAUNCH_RANGE`, `ThreatSite.outranged_by`). A site it
out-ranges it engages from outside the envelope, and takes no exposure from;
its missiles (step 2) and its suppression (step 3) still apply. A strike
element is exposed to every site its route enters, as before: it has to
reach its target, and its bombs are not aimed at the sites.

Why: an anti-radiation missile is fired from outside the envelope of the
site it targets, and that is the point of it. Without standoff the SEAD
element flew into the unsuppressed site first, at its full kill probability,
and an escorted package lost more aircraft in total than one sent alone —
the model inverted the doctrine it was meant to represent, and a commander
who valued airframes should never have flown SEAD.

The comparison is decided by content alone, never by authority: an
out-ranged site is out-ranged in every row of the table below, whether DCS
holds it, whether the SEAD element has a paper shot at it, or whether the two
shared the sim. Whether the SEAD element is *rolled* at all is still
section 1's: one DCS is holding is never rolled. Observed, nothing changes:
DCS flies the SEAD task and decides.

The ranges are placeholders from published figures. Each is the system's
published maximum, so each comparison sets one statistic against the same
statistic:

| | figure | source |
|---|---|---|
| AGM-88C launch range | 148 km | Wikipedia, "AGM-88 HARM", infobox, 80 nmi "standoff" (uncited there; no separate C-model figure; the USAF fact sheet gives "48 plus kilometers") |
| Kh-58U launch range | 250 km | Wikipedia, "Kh-58", infobox, citing the JED *International Electronic Countermeasures Handbook* (2004) |
| SA-6 engagement radius | 24 km | Wikipedia, "2K12 Kub", infobox (3M9 missile) |
| Patriot engagement radius | 160 km | Wikipedia, "MIM-104 Patriot", infobox (PAC-2 GEM, estimated) |

So both sides' SEAD out-ranges the site it faces, blue's by six times, red's
by 90 km. Red's margin rests on the Kh-58U figure: the same article gives
the original Kh-58 120 km from 10,000 m, which would *not* out-range the
Patriot, and with it red's SEAD element would fly into the Patriot as before
(over seeds 0 to 99 red's escorted first sorties would lose 44 aircraft,
exactly as without standoff, against 26 alone). None of these was chosen for
the balance it gives; a launch range really depends on release altitude and
speed, and the paper track has one altitude.

**Dice.** Each step draws a number of dice fixed by the state it starts from,
and the dice thrown within a step never change how many it throws: every
missile is rolled though the first one destroyed the site, every aircraft is
rolled against every site though it is already dead. What an earlier step did
is part of the state a later one starts from: an aircraft shot down fires
nothing, and a site destroyed throws nothing at the strikers. That is how the
one-flight package already rolled its bombs (survivors times two), and it is
kept. The stronger form, a TOT whose total draws ignore what happened within
it, would change the one-flight package's dice. Standoff takes the SEAD
element's exposure dice away for a site it out-ranges: that is a different
situation, decided by content before a die is thrown, not a different
outcome.

**Mixed authority.** Section 1 holds per entity, at the TOT: an element DCS
is holding is never flown through the sites on paper, and a site DCS is
holding loses units only to snapshots. What SEAD does on paper is therefore
bounded by the rule this section adds:

> A SEAD element has a paper effect on a site — missiles rolled at it, and
> suppression of it for the strike element — only when the SEAD element and
> the site are both outside DCS at the TOT, **and** DCS never held the two at
> the same time before it.

| at the TOT | SEAD exposure | missiles at the site | strike's kill probability |
|---|---|---|---|
| SEAD paper, site paper, never shared the sim | rolled\* | rolled | suppressed |
| SEAD paper, site paper, shared the sim earlier | rolled\* | none | full |
| SEAD paper, site held | rolled\* | none | full |
| SEAD held, site either | not rolled | none | full |

\* unless its missile out-ranges the site (standoff, above), in which case
that site is not rolled against it in any row. Independently of the table
the strike element is rolled only if DCS is not holding it. The cases the
rule settles:

* *SEAD observed, strike not.* The sim decides what the SEAD element did,
  and a snapshot can carry only site units destroyed, never suppression. So
  no suppression is inferred: the unwatched strikers meet the site at its
  full kill probability, less whatever the snapshots destroyed (a site
  destroyed outright fires at nobody). Crediting suppression as well would
  count one sortie's effect twice, or credit an effect no authority
  observed. It errs against the players on the rare edge of the bubble
  where the two elements, 17 km apart, are held differently.
* *Strike observed, SEAD not.* The SEAD element flies its exposure on paper
  to any site it cannot out-range, and its missiles are rolled at any site
  DCS is not holding; the strike's
  exposure is the sim's, so suppression has nothing to act on.
* *A site DCS holds* loses units only to snapshots: paper missiles cannot
  take one (the tracker refuses), and a paper SEAD element cannot have shut
  down a radar that is being simulated.
* *Shared sim time.* The client tasks a SEAD element to engage any air
  defence it meets along its route, not at a waypoint, so a SEAD element DCS
  held at the same time as a site had its chance to fire at it, and what it
  did came back by snapshot. If both then leave the bubble before the TOT,
  firing the same missiles on paper would resolve them twice. The engine
  records the contact (`Element.sim_contact`) whenever the client
  acknowledges a spawn, which is the only moment two entities can begin to
  be held together, so no stretch of shared time is missed and a replay
  records the same contact. A strike element has no such rule: its attack
  hangs on the waypoint it reaches at its TOT, so the sim resolves its bombs
  only if it holds the strike then.
* *After the TOT.* An element whose part in the TOT is resolved is spawned
  with the tasking `{"kind": "egress"}`, which carries no task. Without it a
  flight that re-entered the bubble on the way home was tasked to attack
  again — the client hangs the attack on the last waypoint when there is no
  attack waypoint left — and a strike resolved on paper bombed the same
  target a second time in DCS. That was true of the one-flight package too,
  and is the one change made to it on purpose: its frames now differ from
  the recorded ones in those spawns' tasking and nowhere else.

Section 3's caveat carries over per element: one held for part of its route
but not at the TOT is rolled for the whole route. It cannot lose an aircraft
twice, because the roll is of the aircraft still alive.

**Failing independently.** A SEAD element shot down, on paper or by
snapshot, suppresses nothing and the strike flies on at the full kill
probability. A spawn the client refuses scrubs that element only. At the end
of the war each element still on the ramp is stood down, and each in the air
flies out (section 4).

**Observed.** The SEAD templates fly under DCS's group task `SEAD`. The
client puts an `EngageTargets` task for `"Air Defence"` on the first
waypoint, active for the whole route, and an `AttackGroup` on the attack
waypoint against each fragged site that exists in the sim when the element
spawns. What it achieves the engine learns only from snapshots. The pylons
are as empty as the strike's (README), so until a mission-editor export
fills them these jets carry no missiles in DCS and suppress nothing there.

**What it buys, at the placeholder numbers.** Over each side's first sortie
on seeds 0 to 399, the only situation both configurations fly identically,
blue's strike elements lost 28 aircraft escorted and 106 alone, red's 32 and
111: about 0.08 a sortie against 0.27. The SEAD elements, firing from
standoff, lost none, so the package as a whole loses the same 28 and 32.
Before standoff the strike elements lost 37 and 41, and the SEAD elements,
flying into the unsuppressed sites first, lost 130 and 111 more: escorted
packages lost 167 and 152 aircraft against 106 and 111 alone. Over whole
wars (seeds 0 to 99, a day each) strike losses per sortie fall from 0.30 to
0.09 for blue and from 0.33 to 0.07 for red, no SEAD aircraft is lost, and
the wars are shorter. Red wins 68 of them against 56 without SEAD (67 before
standoff). Part of that is a race the symmetric content does not remove:
escorted strikes finish a target on the second sortie more often, for both
sides, and red's second TOT falls 52 s before blue's. In 21 of red's 45 wins
at that moment blue's strike, already airborne, flattened the depot 52 s
later (9 of 28 without SEAD). None of those numbers was tuned: the missile
and suppression values were set before any war was flown, the ranges were
taken from published figures before any was flown with them, and all are as
uncalibrated as the kill probabilities they cut. `tests/test_sead.py` holds
two claims: escorted strikers lose fewer, and, where the SEAD missile
out-ranges the site, so does the escorted package as a whole.

Rejected: modelling SEAD as a bonus on the strike flight. The point of a
package is that its parts can fail independently — the SEAD element can be
shot down, arrive late, or run dry — and a bonus cannot.

Rejected: inferring suppression from a watched SEAD element's survivors. The
snapshot says it is alive, not that it fired or where; and a site it could
not reach in the sim was never suppressed by anybody.

Rejected: resolving the SEAD element at its own arrival, 120 s before the
package's TOT. The suppression it buys must be settled by the strike's
exposure; resolving the two at different instants would let each be judged
by a different picture of who held what, and carry state between them.

## Out of scope, deliberately

Multi-package deconfliction, escort and CAP, tankers and AWACS, the ground
war and front line, logistics and resupply, base capture, pilot records, and
real unit-template fidelity. Each has a `TODO(seam)` where it attaches. None
of them changes the decisions above.

## Theater: the Syria map

The slice has one target a side, so a war is decided in two or three sorties,
and its coordinates are invented, so once distance decides exposure the
outcomes are invented too. `theater.build_syria_theater` and
`oob.build_syria_oob` are a real map sized for a war with an arc. They are
what `python -m campaign` starts by default (`--theater syria`); the slice
stays as `--theater slice`, and `Campaign()` still builds the slice, because
every other test is written against it.

**Where things are.** Airbases are the DCS Syria map's own, in DCS map
coordinates, copied as numbers from pydcs's generated terrain data
(`dcs/terrain/syria/airports.py`, LGPL-3.0). pydcs's `Point(x, y)` is
northing and easting, which are the engine's Vec3 x and z, so it is placed at
`(x, 0, y)`. The axes were checked by inverting the map's projection, and
tests pin both the raw numbers and the directions (Incirlik is north-west of
Bassel al-Assad), because distances alone cannot tell a swapped axis.
Everything else is a game-design placement, not a real facility: each target
and site is at a stated offset of at most about 7 km from a DCS airbase on the map
and is named for it so a player can find it.

**Bases follow from the planner.** A side now plans each target from its
base nearest that target, falling back to the next nearest if that base
cannot cover the strike (`Theater.airbases_nearest`). Bases used to be tried
in id order, which flew everything from whichever sorted first until it ran
dry, whatever the geography. A base therefore earns its place by being
nearest some enemy target. Blue flies from Hatay (nearest five of red's seven
targets) and Gaziantep (the eastern two). Incirlik is nearer none, so it is
not a blue base here: it is blue's rear area instead, where its deepest
targets are. Red flies from Bassel al-Assad (nearest Incirlik and Adana),
Kuweires (nearest Gaziantep, Kahramanmaras and Sanliurfa) and a detachment
at Abu al-Duhur (nearest the Hatay target).

**The arc.** Each side has seven targets that the planner takes in priority
order, and priority falls with depth. That ordering is a campaign plan that
works inward from the border, not a judgement of what the targets are worth.
The two nearest the border are small (4 units) and undefended. The next two
(12 units) have one envelope over the route to them. The last three
(24 units, about 150-210 km from the nearest enemy base) have two, except
red's deepest, Shayrat, which has three: its route passes 4.5 km from the
Hama target, so whatever defends Hama covers it too. Both sides get the same
targets by size, and the same totals of airframes and ordnance spread over
their fields by geography. Every new site
type keeps the SA-6's placeholder kill probability, for the reason the
Patriot does (section 3), so the types differ only in reach: SA-11 35 km,
SA-15 and Roland 10 km (one shared point-defence radius), Patriot 40 km as
before.

**What a war looks like, before SEAD standoff.** Measured offline over 50
seeds, with nobody connected. These are not asserted anywhere, and the
coming SEAD standoff change will move them:

| | blue | red |
|---|---|---|
| strike packages per war (median, range) | 66 (55-78) | 61 (50-72) |
| of which SEAD-escorted (mean) | about 25 | about 25 |
| enemy targets destroyed (median) | 7 | 6 |
| airframes lost (median, range) | 27 (10-45) | 14 (5-40) |
| bombs left of 360 (median, min) | 96, 48 | 116, 72 |
| airframes lost per package: shallow / middle / deep | 0.00 / 0.22 / 0.51 | 0.00 / 0.24 / 0.34 |
| sortie length: shallow / middle / deep | 30 / 33 / 44 min | 31 / 43 / 45 min |

Every war ended in a victory: blue 27, red 23, no draws. The median war
lasted 44 hours (36-52). No side ever ran out of anything it needed, though
in the longest wars a single field did and its targets passed to the next
nearest. The arc is there: the first targets fall in the first hours for
nothing, and the deep ones cost one airframe in two or three packages and
take longer to reach.

Two things this content cannot fix. First, *a war lasts about two days, not
several*, because the engine's tempo is one package per side in the air at
all times, around the clock: about 35 packages a side a day. The war's
length is then set by the target units, and two days already needs 24-object
statics. Several days needs a sortie-rate model (turnaround, crew rest,
night), which is engine work. Second, *on paper, defences erode*. A SEAD
two-ship's four missiles take about one unit off the sites on its route per
sortie (ARM Pk 0.25), and a site keeps firing at full Pk until its last unit
goes.
So by its last targets a side has often destroyed most of the other's sites:
blue kills a median of 5 of red's 7 and red 4 of blue's 5. Red's
losses per package fall in the second half of its war. Both effects are
SEAD-model questions, not theater ones.
