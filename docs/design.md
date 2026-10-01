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
current unit count -- and, for an air-defence site, which units (section 7);
an aircraft that dies in DCS is never also rolled on paper.

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
work, not built; a SEAD element's missiles can take its radar on the way
(sections 5 and 7), and a site without one cannot engage.

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
threat sites, and takes losses through exactly the same authority rule. How
many packages a side has open at once is decided by its squadrons' readiness
(section 6).

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
or stood down. A package whose strike element is lost stays open until its
SEAD element is home, so no second package goes against its target before
then (section 6); its strike squadron, with nothing open, is free for
another.

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
2. its survivors' missiles — those the sim has not already spent (below)
   — are rolled against the sites, shared round-robin in site-id order,
   each destroying the site's radar at `resolver.ARM_PK` (0.25) -- only a
   site with a radar left is fired at, and a hit never takes a launcher
   (section 7) -- through the tracker
   like any paper loss;
3. every SEAD aircraft that fires in step 2 halves the kill probability of
   each site it engaged, compounding
   (`resolver.SEAD_SUPPRESSION_PER_AIRCRAFT`), so a SEAD element that lost a
   jet only half does its job;
4. the strike element flies its exposure against the sites still standing,
   at those probabilities: a site the missiles blinded or destroyed does
   not fire;
5. the strike element's survivors release.

Each element's survivors expend there what is left of its reservation,
whoever held what: the engine books what it loaded, less what the snapshots
already showed leaving the sim (below), so every round is debited once.
`resolve_exposure` rolls the probabilities it is given, so suppression
changes its inputs and nothing downstream.

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
holds it, whether the SEAD element has a paper shot at it, or what the sim
spent of its missiles. Whether the SEAD element is *rolled* at all is still
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
holding loses units only to snapshots. A SEAD element's missiles add a third
thing to keep single: a missile the sim fired was resolved there, and must
never be fired again on paper. What SEAD does on paper is therefore bounded
by the rule this section adds:

> A SEAD element has a paper effect on a site — missiles rolled at it, and
> suppression of it for the strike element — only when the SEAD element and
> the site are both outside DCS at the TOT, and then only with the missiles
> the sim has not spent: what the engine reserved, less what the snapshots
> showed leaving the sim, and no more than its surviving aircraft carry.
> Only the aircraft that fire those missiles suppress.

| at the TOT | SEAD exposure | missiles at the site | strike's kill probability |
|---|---|---|---|
| SEAD paper, site paper, the sim spent none of its missiles | rolled\* | all its survivors carry | suppressed by every survivor |
| SEAD paper, site paper, the sim spent some | rolled\* | the rest | suppressed by the aircraft that fire the rest |
| SEAD paper, site paper, the sim spent all of them | rolled\* | none | full |
| SEAD paper, site held | rolled\* | none | full |
| SEAD held, site either | not rolled | none | full |

\* unless its missile out-ranges the site (standoff, above), in which case
that site is not rolled against it in any row. Independently of the table
the strike element is rolled only if DCS is not holding it. "Spent" is
counted per element over its whole sortie, every stretch DCS held it
included, and an element DCS never held has spent nothing. The cases the
rule settles:

* *SEAD observed, strike not.* The sim decides what the SEAD element did,
  and a snapshot can carry only site units destroyed and missiles gone,
  never suppression. So no suppression is inferred: the unwatched strikers
  meet the site at its full kill probability, less whatever the snapshots
  destroyed (a site destroyed outright fires at nobody). Crediting
  suppression as well would count one sortie's effect twice, or credit an
  effect no authority observed. It errs against the players on the rare
  edge of the bubble where the two elements, 17 km apart, are held
  differently.
* *Strike observed, SEAD not.* The SEAD element flies its exposure on paper
  to any site it cannot out-range, and what it has not spent is rolled at
  any site DCS is not holding; the strike's exposure is the sim's, so
  suppression has nothing to act on.
* *A site DCS holds* loses units only to snapshots: paper missiles cannot
  take one (the tracker refuses), and a paper SEAD element cannot have shut
  down a radar that is being simulated.
* *What the sim spent.* The client tasks a SEAD element to engage any air
  defence it meets along its route, not at a waypoint, so a SEAD element DCS
  held may have fired at a site long before the TOT, and what its missiles
  did came back by snapshot. Since protocol v3 the snapshot also says what
  is still aboard each flight, beside the count the client read when it
  built the group (docs/protocol.md, `state`). The engine reads a SEAD
  element's munition from every snapshot of it before its TOT, held or not
  — the census a client sends just before obeying a despawn is the last
  word on that instantiation — and adds every drop from one reading to the
  next to `Element.sim_spent`. The first reading of each instantiation is
  the spawn-time count, never an absolute load: the pylons are empty in DCS
  today, so a jet reports no missiles from its first snapshot, and read as
  an absolute count that would say every missile had been fired. Each
  instantiation starts a new baseline, because the client builds every
  spawn with its template's loadout and a re-spawned element is re-armed in
  the sim; what it spent before stays spent. A count the client could not
  read is taken as everything spent: the cost is a SEAD element that does
  nothing more on paper, the alternative a missile fired twice. Events,
  `shot` included, play no part, so the reconciliation equivalence of
  section 1 holds: drop every event frame and the same missiles are counted.
* *Suppression after the sim fired.* A SEAD aircraft that spent everything
  in the sim does not suppress on paper. On paper, suppression is what
  missiles in the air do to a site while the strikers cross its envelope;
  a jet with nothing left to fire buys none. Its shots were the sim's, and
  what they achieved is already in the snapshots; crediting a paper
  suppression for them too would count one missile's effect twice — the
  same reason a SEAD element DCS holds buys none. So the number of
  suppressing aircraft is the number it takes to carry the missiles fired
  on paper, never more than survived: two missiles left on a two-ship is
  one aircraft's worth, and halves the site's kill probability once. An
  element whose pylons were empty in the sim spent nothing there, fires its
  whole reservation on paper and suppresses with every survivor, exactly as
  an element DCS never held.
* *Booking every missile once.* The element's reservation is the paper's
  licence to fire, so it is kept equal to what the paper may still fire.
  Whenever either bound moves — a snapshot shows rounds gone from the sim,
  or an aircraft is lost — the excess is debited then: up to the dead
  aircraft's share (two rounds each) as lost with them, the rest as
  expended. At the TOT the survivors expend what the reservation still
  holds, and the paper fires exactly that. A summed count cannot tell
  whether rounds that left the sim in the same interval an aircraft died
  were fired or went down with it; calling the dead aircraft's share lost is
  exact whenever each jet carried its full load, and either way the round
  is in exactly one bucket and the paper never gets it. An element the sim
  never armed books precisely what the one-flight package always did, the
  rounds of each aircraft lost, and the eleven pre-SEAD wars replay
  unchanged.
* *What the sim did after its last snapshot.* If DCS goes away between
  snapshots, whatever it fired since the last one never reaches the
  campaign, and neither does anything those missiles destroyed. The paper
  may fire such a missile; it is then resolved once, by the only authority
  that ever reported on it. A DCS restart already treats losses this way
  (docs/protocol.md, Reconnect): a unit killed after the last snapshot is
  re-spawned at the count the snapshots recorded.
* *After the TOT.* An element whose part in the TOT is resolved is spawned
  with the tasking `{"kind": "egress"}`, which carries no task, and its
  ammunition is no longer read: the paper has nothing left to withhold.
  Without the egress tasking a flight that re-entered the bubble on the way
  home was tasked to attack again — the client hangs the attack on the last
  waypoint when there is no attack waypoint left — and a strike resolved on
  paper bombed the same target a second time in DCS. That was true of the
  one-flight package too, and is the one change made to it on purpose: its
  frames now differ from the recorded ones in those spawns' tasking and
  nowhere else.

A strike element has no ammunition rule: its attack hangs on the waypoint it
reaches at its TOT, so the sim resolves its bombs only if it holds the
strike then, and whoever holds it at the TOT is the one authority over them.

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
spawns. What it achieves the engine learns only from snapshots, and since
protocol v3 that includes what it fired. The pylons are as empty as the
strike's (README), so until a mission-editor export fills them these jets
carry no missiles in DCS, fire nothing there, and fire their whole load on
paper if they meet their TOT outside the bubble. The DCS weapon type names
the client reports them under are as unverified as the CLSIDs that would
load them; mission/validate_templates.lua's `ammo.*` cases record the names
DCS uses.

**What it buys, at the placeholder numbers.** Measured before section 7
made a missile hit take the radar rather than a launcher; over seeds 0 to 99
the escorted strike elements' first-sortie losses have since fallen from 7
and 10 to 3 and 4 (tests/test_sead.py). Over each side's first sortie
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

Rejected: the contact rule this replaced. Before protocol v3 the engine
could not see what the sim fired, so a SEAD element that DCS had ever held
at the same time as a site, before the TOT, had no paper effect on that site
at all. It over-triggered badly: in a harness run red's SEAD element shared
the sim with the Patriot briefly and early, fired nothing — its pylons were
empty — and then fired nothing and suppressed nothing on paper for the rest
of its sortie, and the Patriot shot down both Su-24M strikers behind it.
Ammunition is ground truth the snapshot already had authority over, like
liveness and unit counts, so the engine now knows how many missiles the sim
fired, and the paper fires the remainder.

Rejected: counting missiles from `shot` events. Events are attribution only;
a campaign that moved when an event arrived would reach a different state
with the event stream dropped.

Rejected: inferring suppression from a watched SEAD element's survivors. The
snapshot says it is alive, not that it fired or where; and a site it could
not reach in the sim was never suppressed by anybody.

Rejected: resolving the SEAD element at its own arrival, 120 s before the
package's TOT. The suppression it buys must be settled by the strike's
exposure; resolving the two at different instants would let each be judged
by a different picture of who held what, and carry state between them.

## 6. Sortie rate: readiness, not a count

Until this, each side flew exactly one package at a time, around the clock,
and never waited for anything: about 35 packages a side a day, and a Syria
war over in a median 41 hours. That was structure, not content: the "one at
a time" rule stood in for deconfliction, and nothing else limited tempo.

**Readiness bounds the planner.** A side frags a package whenever a squadron
is ready to fly it. Each pulse it goes down its targets in priority order,
skips any target it already has an open package against, and tries each
from its nearest base first, falling back to the next nearest exactly as it
does for a dry one (theater section). So a side has as many packages open as
it has ready squadrons for. Two rules stand in for the deconfliction that is
not built:

- **never two open packages against one target**, and
- **a squadron contributes to at most one open package**: a squadron with an
  open element (an open reservation) is not ready. One whose element has
  closed is free, even while the rest of its package flies on.

Left undone, as `TODO(seam)` in `Campaign._plan` and the planner's
docstring: packages are not sequenced against each other on time or route;
two packages through one envelope are each suppressed only by their own SEAD
element and each rolled against the site on its own; one package's SEAD
never covers another's strike; and a strike whose base's SEAD squadron is
busy, turning its jets round or out of sorties for the day goes in alone, as
it always did when the SEAD squadron was dry, where a planner would hold it.

Readiness and section 5's ammunition rule meet in one place and do not
change each other. A SEAD element stays open, and its squadron busy, until
it lands, however much of its reservation the snapshots have already shown
the sim firing: the reservation may hold no missiles and still be open, and
"busy" is an open reservation, not a loaded one. So a SEAD element that
emptied its rails in the sim does not free its squadron to escort another
strike; the strike it was fragged with flies on with whatever suppression
section 5 grants it, and any other strike from that base goes alone. What
comes back at landing goes through turnaround like any other airframe, and
what is left of its rounds back to stock.

**Turnaround.** Airframes that come home are not ready again until their
squadron's turnaround has passed. They sit in a fourth bucket, so
conservation is now `available + reserved + turning + lost == total`,
checked by `Squadron.check_invariant` on every move. The record is per
landing (`oob.Turnaround`: airframes, ready time), not per airframe:
airframes are anonymous, and every airframe of one element lands at the same
instant, so per-airframe timestamps would carry nothing more. An element that
took off -- home on schedule, shot down to the last jet, or scrubbed in the
air by a refused spawn -- returns its survivors through turnaround from the
moment it closes; one stood down on the ramp never flew and is ready at
once. Unspent ordnance goes straight back to stock: turning a jet round is
what the turnaround time is for.

**Daily limit.** A squadron flies at most `floor(rate x airframes on
strength)` aircraft sorties a local calendar day, on strength being total
less lost. A sortie is charged when its element is fragged, to the day it
takes off on, so the next package planned in the same pulse already sees it;
one stood down at the end of the war keeps its charge, since nobody plans
after that. Calendar days, not a rolling 24 hours, because under the
daylight rule midnight never falls inside a flying day, so the two windows
agree, and the calendar needs no history beyond one count per day
(`Squadron.sorties_by_day`).

**Daylight.** A day-only squadron is fragged only when the package's TOT is
between sunrise and sunset. The TOT, not each element's own time over the
target, because the TOT is the one instant the whole package is resolved at
(section 5); the SEAD element's 120 s lead does not leave it out of the
first strike of the day. "Daylight" is the sun's centre above -0.833 deg,
the definition almanacs publish sunrise and sunset by, computed in
`campaign.sun` with NOAA's Solar Calculator algorithm (Meeus' low-precision
solar coordinates) from the campaign's local date and time and the
theater's latitude, longitude and UTC offset. Standard library, no time zone
database, no clock read. It reproduces the published 2025 sunrise and
sunset table for Aleppo to within two minutes across June, September and
December (`tests/test_sortie_rate.py`). The theater is reckoned from one
point, Aleppo (36 deg 12' N, 37 deg 12' E), the middle of the fighting:
across the map sunrise moves by about a quarter of an hour. Its clock is
UTC+3, the DCS Syria map's (pydcs, `dcs/terrain/syria/syria.py`).

Darkness is waited out, not routed around. If a target's nearest base has a
squadron free to go but its TOT would fall in the dark, the target waits for
the sun there, and that squadron is held for it for the rest of the pulse.
Without that, every dawn a farther field's longer leg landed its TOT after
sunrise first and won the target, and less important targets with longer
legs took the nearest fields' squadrons ahead of the most important ones.
Busy, out of sorties or turning round is a fact about the squadron, and
another base may go instead.

TODO(seam): night-capable squadrons (`SortieRate.day_only` False on a
squadron with night attack kit). Both Syria types have it in reality; the
map flies by day until something tells the squadrons apart.

**The campaign has a date and a time of day.** `Campaign.start` is the local
date and time at campaign second zero, naive on purpose: it is the theater's
own clock, as a mission's editor time is. It is saved (save version 6), so a
reloaded war keeps its sun. The default is 06:00 on 22 September 2025, the
September equinox: its twelve-hour day is the year's mean, so the default
neither lengthens the flying day as summer would nor shortens it as winter
would, and 06:00 opens the campaign at first light, about twenty minutes
before sunrise over the map. `--start YYYY-MM-DDTHH:MM` (or
`Campaign(start=...)`) sets it for a new war; a save keeps its own, and the
flag is ignored with a warning, as `--theater` is.

**The parameters are published figures, never tuned.** They are the order of
battle's (`oob.SortieRate` on each squadron), labelled placeholders as the
threat model's radii are, chosen before any war was flown on them:

| | figure | source |
|---|---|---|
| F-16 turnaround | 45 min | DVIDS, "F-16 Integrated Combat Turns enable ACE at Northern Strike 24-2" (180th Fighter Wing, 20 August 2024): "The maximum allotted time for an F-16 ICT is 45 minutes", an integrated combat turn being refuel and rearm |
| Su-24M turnaround | 45 min | none found for the Su-24M; the F-16's, for the reason every site type carries the SA-6's kill probability |
| F-16 sustained rate | 1.35 sorties per airframe per day | Cordesman and Wagner, *The Lessons of Modern War, Volume IV: The Gulf War* (CSIS, 1994), chapter 7: the F-16s "had the highest use rate of any aircraft in theater -- 1.35 sorties per day" over the 43 days of Desert Storm |
| Su-24M sustained rate | 1.27 sorties per airframe per day | derived from Yermakov, "Russian Aces in Syrian Skies" (RIAC, 23 October 2015): "strike aircraft have made 669 sorties over two and a half weeks", flown by 12 Su-24M, 12 Su-25SM and 6 Su-34, so 669 / (17.5 x 30). A mixed group in which the Su-24M flew about half the sorties; no Su-24M-only figure was found |

Both rates are the same statistic -- an achieved combat average over weeks --
so the two sides' limits differ by what was measured, not by a choice of
statistic. The turnaround is a routine turn with no fault to fix; the
maintenance a sustained campaign also needs is what the daily rate carries.
Desert Storm's F-16 sorties averaged over three hours and these last 30 to
45 minutes, so on this map turnaround almost never binds and the daily rate
does: on every full day of a war each strike squadron flies 60 to 100
percent of its limit, mostly over 80 (seeds 0 to 3).

**One mechanism, two theaters.** The slice is the fixture the tests and the
recorded wars were written against. Its squadrons carry
`oob.UNCONSTRAINED` -- no turnaround, no daily limit, any hour -- and go
through the same readiness code as Syria's. With one target a side and one
strike squadron a side, the two deconfliction rules are the old "one open
package a side" exactly, so nothing in it moves: the eleven recorded wars in
`tests/fixtures/pre_sead_wars.json` replay frame for frame and die for die
(`tests/test_single_element.py`), and they fail if the slice is given a
daily limit or a turnaround.

**"Cannot task" means dry, not waiting.** A side with nothing open and
nothing it can send is told it cannot task only if no base has the stock for
a strike at all. One whose jets are busy, turning round, out of sorties for
the day or waiting for the sun is keeping the tempo of a war.

**DCS's clock is not the campaign's.** A DCS mission always starts at its
editor date and time; a persistent campaign is wherever the war has got to.
At `hello` the engine compares the mission's local time (its
`mission_start_epoch` plus `t`) with the campaign's (`start` plus the
clock) and logs a warning when they differ by more than a minute, giving
both and the difference in time of day. It does not correct it: the
protocol has no frame for setting DCS's clock, and the time is fixed when
the server loads the mission. TODO(seam): whatever generates or restarts
missions should start each one at the campaign's local time. Nothing sent
or saved depends on the check, so a replay is unaffected.

**What a war looks like now.** Measured offline over seeds 0 to 49 with the
defaults, nobody connected; not asserted anywhere. "Before" is the same
seeds on the engine as it stood before this section (commit f5143f6):

| | before | after |
|---|---|---|
| war length, median (range) | 41 h (34-50) | 54 h (50-60) |
| result | blue won 30, red 20 | blue won 13, red 37 |
| packages per war, median, blue / red | 62.5 / 57.5 | 61 / 62.5 |
| packages per day of war, median, blue / red | 36.5 / 33.6 | 27.4 / 27.5 |
| packages airborne at once, share of the war, blue | one 89%, none 11% | none 55%, one 20%, two 25% |
| packages airborne at once, share of the war, red | one 90%, none 10% | none 57%, one 14%, two 20%, three 9% |
| airframes ready / reserved / turning, mean share | 96 / 4 / 0 % | 93 / 4 / 4 % |
| airframes lost, median (range), blue | 5 (0-24) | 6 (1-14) |
| airframes lost, median (range), red | 1.5 (0-7) | 6 (2-11) |
| enemy targets destroyed, median, blue / red | 7 / 6 | 6 / 7 |
| enemy sites destroyed, median, blue / red | 6 of 7 / 5 of 5 | 6.5 of 7 / 5 of 5 |
| SEAD-escorted packages per war, mean, blue / red | 24.3 / 19.8 | 25.5 / 19.6 |

Every war ended in a victory, none in a draw. Bombs left at the end never
fell below 76 of a side's 360. The earliest TOT of any day was about 06:18
and the latest about 18:24, local.

*Does the war unfold over days?* Over three days of flying, not several. A
war starting at 06:00 is now always decided on its third day, between 08:00
and 18:00, where before it ran round the clock and ended between the second
afternoon and the third morning. The daily limit is what binds: it allows a
side about 80 aircraft sorties a day across its squadrons, which at these
figures is about 27 packages, against 35 before -- but packed into twelve
hours, two and three at a time. Concurrency gives back most of what the
nights take. That is what the published figures give with this content (104
target units a side, 44 strike airframes); nothing was adjusted to lengthen
it. The measurement was taken before protocol v3 was merged in; v3 changes
nothing offline, where no SEAD element is ever held, and seeds 0 to 4 replay
to the same length, result, package counts and losses on the merged engine.

*What it does to the cost of a war.* Red now loses a median of 6 airframes
a war, not 1.5, and the result has turned over: red wins 37 of 50, where
blue won 30. Red's extra losses are on deep strikes flown without SEAD while
blue's sites still stand: 88 airframes over seeds 0 to 19 against none
before, when every deep strike red flew alone went in after the sites on its
route were gone. With three strike squadrons red now reaches its deep
targets in parallel with its middle ones, its SEAD squadrons busy or out of
sorties for the day, where before it worked inward one package at a time
and its SEAD had eroded the Hawks first. Why red wins more is not
established; that red has three fields to blue's two, and so more packages
in the air at once, is the obvious candidate and was not tested. Standoff
SEAD still makes escorted packages cheap; that is the separate, known issue
in the theater section, not addressed here.

*Cost.* Planning tries every target and base each pulse rather than stopping
at the first open package, so a Syria day on paper takes about 2.9 s of CPU
instead of 1.0 s. The slice's is unchanged.

## 7. Air defences: typed units, blind batteries, shutdowns, repair

Measured on the Syria map (below), standoff SEAD had made the war nearly
bloodless: a median of 1.5 airframes lost by red in a whole war, 5 by blue.
Three causes stacked. Both anti-radiation missiles out-range every site, so
SEAD is never at risk -- published maximum against published maximum, kept
(section 5). Each surviving SEAD aircraft halves a site's kill probability,
compounding -- kept too: no source was found for a different figure, and
changing it for the outcome it gives would be tuning. And a destroyed unit
never came back: red's SEAD destroyed all five of blue's sites in a median
war, after which its deep strikes cost almost nothing. That third one is the
real one. Real air defences are repaired, relocated and reinforced; here a
battery killed on day one was gone for good. This section is what replaced
it.

**Units are typed.** A site's units are a radar and launchers
(`theater.SITE_UNIT_TYPES`, by the DCS unit type names the client builds:
the template's `lead_type` is the radar, its `unit_type` the launchers). A
Tor vehicle is its own radar and launcher, so every unit of an SA-15 battery
is both. The engine keeps a site's surviving units by type
(`ThreatSite.units_by_type`, and the tracker's group beside it), not as a
bare count.

> An anti-radiation missile homes on an emitter, so an ARM kill removes the
> radar, never a launcher. A site whose radar is gone cannot engage: its
> kill probability is zero until the radar is back.

On paper that means: missiles are rolled only at sites with a radar left,
each hit destroys a radar (`ARM_PK` unchanged), and a hit once the radar is
gone has nothing to home on and destroys nothing -- it is still rolled, by
section 5's dice rule. (Since "Two tiers", below, a hit mostly forces the
radar off the air instead, and seldom destroys it.) A site with no radar is not a live threat
(`Theater.live_threats_along`): it throws no exposure dice, no SEAD is
attached to a route it alone covers, and a SEAD element's missiles go to the
sites still emitting. Which sites can engage is read afresh at each step of
the TOT, so a battery the SEAD element blinded throws nothing at the
strikers behind it, as a destroyed one already did not. Launchers do not
enter the kill probability: it was already flat whatever was left of them
(TODO(threat-model)), and on paper nothing takes a launcher off a battery.

Two consequences worth knowing. A battery with a separate radar can no
longer be *destroyed* by SEAD at all -- after its radar it has nothing to
home on -- only blinded; only an all-emitter battery (the SA-15) can be
destroyed by missiles alone. And the SA-11's 9A310M1 and the Roland ADS
carry fire-control radars of their own, so the real systems could engage
without the battery's search radar; with one emitter per battery these two
are easier to blind than they should be. Per-unit emitters are threat-model
work.

*Bombs on a site.* No path aims bombs at a site: strikes are for strategic
targets, and DEAD is not built (section 3). The rule, when one does, is
that bombs take launchers first -- a radar is one vehicle among several and
is not what a bomb homes on -- and the tracker's paper entry point demands
the unit type of every typed loss, so whoever builds that path has to say
which.

Rejected: keeping units counted and making the *last* unit the radar, so a
site fires until it is gone. That is the model this replaced, and it is
what made SEAD buy only a launcher a hit.

**Typed units are ground truth when DCS holds the site.** Before this a
snapshot carried a unit count, so for a site DCS held the engine could not
tell whether the sim killed the radar or a launcher -- and section 1 forbids
guessing from the paper track. Protocol v4 (docs/protocol.md, "Changes from
v3") has the client count a ground group's living units by
`Unit.getTypeName` (`unit_types`), and the tracker reconciles each type the
way it reconciled the count: clamped per type to what it believes, so a
snapshot can neither resurrect a radar nor invent one. A snapshot that
cannot name every living unit's type is no census of that group's units:
the engine books nothing from it and waits for one that can. A ground spawn
carries a `composition` when the client's radar-first build would get the
battery wrong, so a site that lost its radar comes back into DCS without
it.

A way to keep typed ground truth without a protocol change was looked for
and not found. The client could keep building radar-first and the engine
could infer the type of each lost unit from which units the count says are
gone -- but the count does not say which, and DCS kills whichever unit the
sim decides. The client could report the dead unit's name in an event, but
events are attribution only (section 1), and the engine must reach the same
state with every event dropped. Unit names in the snapshot instead of types
would be ground truth too, but no smaller a change, and the engine would
still need each name's type. So v4.

**Air defences reconstitute.** While DCS is not holding a site, the engine
repairs it: a destroyed radar is replaced after `SiteRepair.radar_time` of
repair work, destroyed launchers one per `SiteRepair.launcher_time`, the
two jobs side by side. Repair is logistics, not combat, so it is the
engine's authority -- but only where DCS is not. A site DCS holds changes
only by snapshot and accrues no repair work while held; nor while a spawn of
it is unacknowledged (DCS will build what the frame named) or a despawn is
(its last census may still be on its way, and would read a repaired unit as
one the sim destroyed). With no client connected nothing is held. Work is
counted from the clock (`Campaign.repaired_to`), in id order, with no dice,
and saved with the site, so a reload neither repeats nor skips it. A
respawned site carries its repaired composition. Its owner is told what
came back; the enemy is not.

*A site with every unit destroyed stays gone.* Repair restores the
equipment of a battery whose organisation survives -- crews, command,
spares, the position it holds. One with nothing left has nothing to
repair: replacing it is moving a new battery in from a reserve, which is
reinforcement, an order-of-battle decision the engine has no model for.
Rebuilding from nothing would also make every site indestructible given
time, and "destroyed" would stop meaning what the players are told.
TODO(seam: logistics): repair is free. A logistics model draws spares and
crews from a supply network, can be cut, and is where reinforcement lives.

**Per theater, one mechanism.** The rates are the theater's
(`Theater.repair`). The slice keeps repair off (`REPAIR_OFF`), through the
same pass, which restores nothing; the typed rule applies everywhere, so the
slice's SEAD outcomes moved with it and its seed-pinned tests were re-found
(each says why in its docstring). The eleven strike-only wars of section 5
replay unchanged but for the `sync` frame's protocol integer: no site in
them loses anything, so no spawn carries a `composition`.

**The figures.** Every one a placeholder unless it says otherwise, and none
chosen for the war it gives:

| | figure | source |
|---|---|---|
| radar replaced after | 12 h of repair work | **placeholder**. No published repair time was found. The nearest is qualitative: Wikipedia, "AGM-45 Shrike", on Vietnam -- the warhead rarely did more than shatter the radar dish, "an easy item to replace or repair". Half a day stands for "easy, but not before the next raid" |
| launcher restored every | 24 h of repair work | **placeholder**, no source. Longer than the radar: only DCS ever destroys a launcher, with weapons that wreck a vehicle rather than a dish, and a wrecked launcher is replaced, not mended |
| ARM kill probability | 0.25 | unchanged placeholder (section 5) |
| suppression per SEAD aircraft | halves the Pk | unchanged placeholder (section 5); no source found for another |

**With the sortie rate.** Section 6 sends a strike in alone when its base's
SEAD squadron is busy, turning round or out of sorties for the day. Both
rules ask the same question of a route -- `Theater.live_threats_along`,
which leaves a blind battery out -- so a route covered only by blind
batteries is unexposed whatever the SEAD squadron is doing, and a strike
sent alone through a battery whose radar has been repaired meets it at its
full kill probability. Neither rule changes the other: readiness decides
whether a SEAD element can be attached, this section whether one is
needed.

**What it did, measured.** Taken before section 6 was merged, so on the
one-package-a-side engine. Offline Syria wars on seeds 0 to 49, nobody connected,
before this section (commit 64ca9af, re-measured on these seeds, so a little
off the table in "Theater: the Syria map") and after it. Not asserted
anywhere. Packages are those that reached their TOT; a package's losses are
every element's.

| | before | after |
|---|---|---|
| blue airframes lost per war, median (range) | 5 (0-24) | 2.5 (0-19) |
| red airframes lost per war, median (range) | 1.5 (0-7) | 0 (0-2); none at all in 26 wars |
| blue, lost per package: shallow / middle / deep | 0.00 / 0.03 / 0.15 | 0.00 / 0.03 / 0.08 |
| red, lost per package: shallow / middle / deep | 0.00 / 0.03 / 0.03 | 0.00 / 0.01 / 0.01 |
| blue, lost per package by quarter of the war | 0.02 / 0.10 / 0.09 / 0.27 | 0.03 / 0.03 / 0.04 / 0.17 |
| red, lost per package by quarter of the war | 0.03 / 0.07 / 0.01 / 0.01 | 0.01 / 0.01 / 0.00 / 0.02 |
| red's 7 sites: destroyed / blinded / radars repaired, median | 6 / - / - | 5 / 3 / 2 |
| blue's 5 sites: destroyed / blinded / radars repaired, median | 5 / - / - | 0 / 8 / 6 |
| war length, median (range) | 40.9 h (33.6-49.6) | 39.9 h (33.1-45.3) |
| wins, blue / red | 30 / 20 | 33 / 17 |

**The war is not a fight again.** Cost still rises with depth for blue, but
less than before, and red's war is bloodier for nobody: it was nearly
bloodless and is now bloodless. The typed rule made SEAD stronger, not
weaker. A hit used to take one launcher of five and leave the battery
firing at its full kill probability; now it takes the radar and the
battery's whole Pk with it. A two-ship's four missiles at 0.25, all at one
battery, blind it about two times in three. Blue's two Hawks and three Rolands cannot be
destroyed by missiles any more, only blinded -- eight times a war -- and
each is repaired after twelve hours, but the next escorted raid through it
blinds it again. Red's five SA-15s, all emitters, are still destroyed
outright and stay gone. The repair figures are placeholders; at these ones,
reconstitution does not outpace standoff SEAD that cannot be hurt.

A first version spent missiles at nothing: it kept attaching SEAD to routes
whose only batteries were blind, and the element fired its load into them.
Both sides' main SEAD squadrons were dry by the last quarter of the war, and
unescorted deep strikes into repaired batteries lost about half an aircraft
a package -- eleven airframes a war a side, a steep cost arc. That arc was
the bug, not the mechanism: an anti-radiation missile cannot be fired at a
radar that is not there. With it fixed (a blind battery is not a live
threat), the numbers are the table's.

What the mechanism is sensitive to, measured to know and not adopted
(twenty seeds each, every other figure unchanged): with the radar back after
2 hours instead of 12, each side loses a median of about 11 airframes a war
and a deep package about 0.25; after half an hour, about 23 and 0.4. The
repair period is the lever, and nothing published fixes it. The other
levers are the ones section 5 and this section already name: the
suppression and ARM placeholders, and per-unit emitters (a TELAR or Roland
fire unit that engages without the search radar).

**Two tiers: a hit mostly shuts the radar down.** The table above is why.
Typing made SEAD stronger, not weaker: a hit took the radar, and with it the
battery's whole kill probability for twelve hours, so a route stayed
unexposed long after the package that blinded it had gone home. Real
anti-radiation missiles rarely work that way. A battery that sees a missile
coming switches its radar off, and the missile loses what it was homing on;
the point of the shot is often that, not a kill.

> A missile that hits home on a battery's radar either destroys it -- and
> the radar then has to be repaired, as above -- or, far more often, forces
> the battery off the air for `resolver.ARM_SHUTDOWN_TIME`. A battery off
> the air cannot engage. When the time is up its radar is simply back: it
> was never broken, so nothing is repaired.

*The dice.* One die a missile (`resolver.resolve_arm`): below `ARM_PK` x
`ARM_DESTROY_FRACTION` it destroys a radar, below `ARM_PK` it forces the
battery off the air, above it misses. One die carries both the hit and the
kind of hit, so the draws are the missiles fired, whatever they say --
section 5's rule. A missile after a shutdown, or after the last radar is
gone, finds no emitter and does nothing, though it is still rolled.

*The shutdown.* It sets the site's `dark_until` to the TOT plus ten minutes,
and loses nothing: no unit, no repair work. A second shutdown before the
first has ended moves the end to the later of the two and never adds them,
so two packages through one envelope do not stack one blackout on another.
The site's owner is told its radar is off the air and when it will be back,
as it is told of any damage to its own sites (section 4); the enemy is not.

*Asked at the TOT.* The planner asks which sites are live along a route at
the package's TOT, not at the moment it plans (`Theater.live_threats_along`,
`at`): a battery that will still be off the air then is no reason to send
SEAD, and one that is off now but back by then is. The TOT asks at its own
instant, so a battery the SEAD element shut down throws nothing at the
strikers behind it, as a blind one already did not.

*Authority.* A shutdown is a paper state. It arises only where section 5
lets a SEAD element act on paper at all -- the element and the site both
outside DCS at the TOT -- so a battery DCS holds is never shut down on
paper; what DCS's own AI does under fire is the sim's. No snapshot can show
a shutdown, because the radar is alive, so a battery spawned into DCS while
the paper has it off the air would engage flights the paper says it cannot
see. Instead its spawn carries `emission_off_until`, the mission time the
paper has it back (protocol v4, docs/protocol.md, `spawn`), and the client
builds it with its emitters off (`Group:enableEmission(false)`) and turns
them on at that time, for that spawn only. Emission control is unverified
DCS content, so the client guards it: a call that raises is logged and the
battery left emitting, never a refused spawn or a broken tick.
mission/validate_templates.lua's `emission.SA-6_Kub_site` case settles it
in the sim. `dark_until` is saved with the site (save version 9).

*The figures.* Neither new one was chosen for the war it gives:

| | figure | source |
|---|---|---|
| a hit destroys the radar | 1.6% of hits (`ARM_DESTROY_FRACTION`); 0.4% of missiles fired | derived from the one published count of fired against destroyed found: in Allied Force "US and NATO aircraft fired at least 743 HARMs", and NATO confirmed the destruction of three of Serbia's approximately 25 mobile SA-6 batteries (Benjamin S. Lambeth, "Kosovo and the Continuing SEAD Challenge", *Aerospace Power Journal*). Three of 743 is 0.4% a missile, which at `ARM_PK` is 1.6% of hits. **A floor, not an estimate**: only SA-6 batteries are counted, against HARMs fired at every kind of emitter, only confirmed kills, and Serbia's operators were unusually disciplined about emissions. It is used as it stands rather than raised by a guess |
| off the air after a shutdown | 10 min (`ARM_SHUTDOWN_TIME`) | **game-design choice**; no published figure found (Lambeth describes Serb operators emitting for 20 seconds and then going quiet, a firing tactic, not this). It outlasts the package behind the missiles: the paper track's 140 m/s crosses the largest envelope on the map, 50 km, in six |
| ARM kill probability | 0.25, now the chance of a hit of either kind | unchanged placeholder (section 5) |

**What it does, measured.** Offline Syria wars on seeds 0 to 49, nobody
connected, every figure as shipped, on three engines: the sortie-rate model
before this section (commit f6b873c), typed units and repair (da889fb), and
two tiers. One script measured all three with the same definitions
(`python tools/measure_wars.py --seeds 0-49 --root <checkout>`; its
docstring defines every row). Not asserted anywhere. The first column
reproduces section 6's "after".

| | untyped (f6b873c) | typed (da889fb) | two tiers |
|---|---|---|---|
| war length, median (range) | 54 h (50-60) | 51 h (35-55) | 73 h (51-120) |
| wins, blue / red | 13 / 37 | 24 / 26 | 11 / 39 |
| airframes lost a war, median (range), blue | 6 (1-14) | 2 (0-10) | 20 (8-29) |
| airframes lost a war, median (range), red | 6 (2-11) | 3 (1-8) | 16 (6-28) |
| SEAD airframes lost, all fifty wars | 0 | 0 | 0 |
| blue, lost per package: shallow / middle / deep | 0.00 / 0.02 / 0.15 | 0.00 / 0.03 / 0.07 | 0.00 / 0.07 / 0.41 |
| red, lost per package: shallow / middle / deep | 0.00 / 0.05 / 0.12 | 0.00 / 0.02 / 0.08 | 0.00 / 0.10 / 0.32 |
| blue, lost per package by quarter of the war | 0.08 / 0.11 / 0.18 / 0.07 | 0.06 / 0.06 / 0.03 / 0.06 | 0.17 / 0.27 / 0.46 / 0.50 |
| red, lost per package by quarter of the war | 0.13 / 0.19 / 0.03 / 0.01 | 0.04 / 0.18 / 0.02 / 0.10 | 0.14 / 0.24 / 0.36 / 0.36 |
| blue, lost per package: sent alone / escorted | 0.12 / 0.09 | 0.04 / 0.07 | 0.52 / 0.06 |
| red, lost per package: sent alone / escorted | 0.10 / 0.09 | 0.06 / 0.05 | 0.41 / 0.07 |
| packages escorted a war, mean, blue / red | 25.5 / 19.6 | 22.8 / 13.2 | 31.7 / 31.8 |
| strike airframes left of 44, median (min), blue | 38 (30) | 42 (34) | 24 (15) |
| strike airframes left of 44, median (min), red | 38 (33) | 41 (36) | 28 (16) |
| own sites destroyed, median, blue / red | 5 of 5 / 6.5 of 7 | 0 of 5 / 5 of 7 | 0 of 5 / 0 of 7 |
| own radars destroyed a war, median, blue / red | - | 10 / 20 | 0 / 0 |
| shutdowns a war, median, of blue's sites / red's | - | - | 24.5 / 26 |
| sites able to engage, mean share of the war, blue / red | 41% / 49% | 63% / 43% | 97% / 99% |

**The war is a fight again, and what it costs is the price of a planner
gap.** A war now costs each side a median of 16 to 20 airframes. The cost
rises with depth, to 0.3 and 0.4 airframes a package against deep targets,
and through the war, from about 0.15 a package in the first quarter to 0.4
and 0.5 in the last. The air defences are still there at the end: no site
is destroyed in a median war, and they can engage almost all of it. Wars
run longer, a median of 73 hours, because strike airframes run down -- a
median 24 and 28 of each side's 44 are left. Of the fifty, 20 are decided
on their third day of flying, 26 on the fourth and 4 later, where every one
was decided on the third before.

But read the "sent alone / escorted" rows before anything else. An escorted
package loses what it lost before, about 0.06 airframes. The whole of the
new cost falls on packages sent through live batteries without SEAD, which
now lose about half an airframe each, and about half of all packages go
alone. That is section 6's seam: a strike whose base's SEAD squadron is
busy, turning round or out of sorties goes in alone, where a planner would
hold it. Before this section a destroyed radar stayed down for twelve hours,
so a strike sent alone usually met nothing; now the battery is back in ten
minutes. So the cost is what these rules produce, but it is the cost of the
planner sending strikes it should hold, not of SEAD failing: SEAD still
never loses an aircraft, and still protects the strikers behind it as well
as it ever did. A planner that held a strike for its SEAD would remove most
of it.

*Repair is almost inert on paper.* At 0.4% a missile, fifty wars destroyed
26 of blue's radars and 30 of red's, against about 1,250 shutdowns a side,
and a median war destroyed none. Repair still matters for radars DCS's
weapons destroy, and the test that flies a Syria war through repair now
destroys on every hit (`tests/test_typed_sites.py`), the war it was written
against. Were the destroy fraction raised -- the figure is a floor --
repair would matter again, and the cost would fall with it.

*Balance did not improve.* Red wins 39 of 50, as it won 37 before typing;
typing alone had brought it to 26. Why red wins is still not established
(section 6).

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
their fields by geography.

**Radii are published maxima, so blue's area sites are Hawks.** Every site
type keeps the SA-6's placeholder kill probability, for the reason the
Patriot does (section 3), so the types differ only in reach, and each reach
is the system's published maximum, for section 3's reason: SA-11 35 km,
SA-15 12 km, Roland 8 km, Hawk 50 km (`theater.py` names each source). Blue's
area sites are MIM-23 Hawks, which Turkey operates, not Patriots. At the
Patriot's published 160 km, one battery over Incirlik or Kahramanmaras covers
blue's whole side of the border, shallow targets included, and the war loses
the depth that gives it its arc; a test fails if it does. The Patriot's
radius stays its published figure. The slice and the standoff tests depend on
it, and the trade is the map's content, not a different physics.

**What a war looks like.** Measured offline over 50 seeds, with nobody
connected, with SEAD standoff and the Hawks, and before the sortie-rate
model: section 6 has the war on it. Not asserted anywhere. Each "was" is the
same batch measured before standoff, when blue's area sites were 40 km
Patriots and the point defences a shared 10 km:

| | blue | red |
|---|---|---|
| war length, median (range) | 41 h (34-50); was 44 h (36-52) | (the same wars) |
| wins | 31; was 27 | 19; was 23 |
| strike packages per war, median (range) | 62.5 (53-75); was 66 | 57.5 (47-69); was 61 |
| SEAD-escorted packages per war, mean | about 22; was 25 | about 22; was 25 |
| enemy targets destroyed, median | 7 | 6 |
| enemy sites destroyed, median | 6 of 7; was 5 | 5 of 5; was 4 |
| airframes lost, median (range) | 5.5 (0-24); was 27 (10-45) | 1.5 (0-7); was 14 (5-40) |
| bombs left of 360, median (min) | 110 (60); was 96 (48) | 130 (84); was 116 (72) |
| airframes lost per package: shallow / middle / deep | 0.00 / 0.03 / 0.15; was 0.00 / 0.22 / 0.51 | 0.00 / 0.03 / 0.03; was 0.00 / 0.24 / 0.34 |
| sortie length: shallow / middle / deep | 30 / 33 / 43 min | 31 / 43 / 45 min |

Every war ended in a victory, with no draws, and no side ever ran out of
anything it needed; in the longest wars a single field did and its targets
passed to the next nearest. The arc in *time* survives: shallow targets fall
in the first hours, and deep ones are larger and further away. The arc in
*cost* has mostly gone. Standoff makes SEAD free: both anti-radiation
missiles out-range every site on this map, AGM-88C 148 km and Kh-58U 250 km
against at most 50 km. A two-ship's suppression then cuts each site's Pk to
a quarter for the strike behind it. Red's SEAD also destroys all five of
blue's sites in a median war, so after the middle targets red strikes blue
for almost nothing: 0.03 airframes a package on the deep targets, and a
median of 1.5 lost in a whole war. Blue meets seven sites with more units
between them, so its deep strikes still cost something (0.15 a package), and
blue now wins more often. A Syria war is close to bloodless, as the slice
became.

Section 7 has since typed the sites' units and made them repairable, and,
before section 6 was merged, measured the same fifty seeds again: the war
got no bloodier. Red lost a median of none, blue 2.5.

Two things this content cannot fix. First, *a war lasted about two days,
not several*, because the engine's tempo was one package per side in the air
at all times, around the clock: about 35 packages a side a day. The sortie-rate
model (section 6) replaced that with readiness, turnaround, a daily limit and
daylight, at published figures; a war now lasts 50 to 60 hours, decided on
its third day of flying. Second, *defences on paper are cheap to beat
and erode*. Standoff SEAD flies unhurt, its suppression is strong, and a
two-ship's four missiles take about one unit off the sites on its route per
sortie (ARM Pk 0.25), while a site keeps firing at full Pk until its last
unit goes. More sites were not tried; SEAD would meet them unhurt too. What
would restore the cost arc is a SEAD-model change (weaker or partial suppression,
sites that hide their radars, missiles that miss a shut-down emitter), not a
theater one. Section 7 has since made two: typed units with repair, and
radars that a hit mostly shuts down rather than destroys.
