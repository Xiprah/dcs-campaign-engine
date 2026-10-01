# DCS campaign engine

A Falcon BMS-style dynamic campaign for DCS World, with the campaign brain
running **outside** the simulator.

This repository is the first vertical slice. Its only job is to prove that one
full loop closes: the engine picks a target, frags a package against it —
a strike, escorted by a SEAD element when the route crosses an enemy SAM —
instantiates each flight when a human is near enough to see it, learns what
happened from ground-truth snapshots, charges the losses to a finite
inventory, writes the war to disk, and picks it up again from there. Both
sides run that loop under the same rules: red plans, strikes, suppresses and
bleeds exactly as blue does, and the war can be lost. Everything else that
would make it a *game* — a ground war, a front line, fighters, pilots — is
deliberately absent, and marked with `TODO(seam):` where it will attach.

---

## Why the brain is out of process

DCS's mission-scripting environment is a bad place to keep a campaign.

* **It dies.** DCS crashes, is restarted, is updated. A campaign that lives in
  the mission dies with it. Here, the mission client holds no state at all:
  the engine owns every identity, and after a restart it re-issues everything
  that should exist. A campaign survives the sim.
* **It must not block.** Mission scripts run on the simulation thread. A
  planner that takes 40 ms is a 40 ms frame hitch. The client here only
  drains a non-blocking socket, spawns what it is told to, and reports what it
  sees.
* **It cannot be tested.** Anything inside the mission environment needs DCS
  running to execute one line. The engine is standard-library Python: the
  whole loop, including the transport, runs in a few seconds on any machine
  with no DCS installed, and CI runs it on every push (below).
* **It is not the whole map.** The campaign simulates a theater; DCS can only
  hold the part of it a player is near. That split — a paper track everywhere,
  real units inside a bubble — only works if something outside DCS owns the
  paper track.

```
  ┌──────────────────────────────┐        TCP 127.0.0.1:7777          ┌──────────────────────┐
  │  campaign engine (Python)    │   newline-delimited JSON frames    │  DCS mission (Lua)   │
  │                              │ ◄────────────────────────────────► │                      │
  │  theater · oob · planner     │   down: sync spawn despawn message │  campaign_client.lua │
  │  bubble  · attrition         │   up:   hello observer event state │  json.lua            │
  │  campaign (the brain)        │         ack                        │                      │
  │  server  (the only sockets)  │                                    │  no campaign state   │
  └──────────────────────────────┘                                    └──────────────────────┘
        listens                                                             connects out
```

The engine listens and the client dials out, because DCS restarts many times
during one campaign and an outbound LuaSocket client is far less trouble
inside the mission environment than a listening socket.

`docs/protocol.md` is the authoritative wire spec. `campaign/protocol.py` is
its executable half: the engine and the offline harness both encode and decode
through it, so the two halves cannot drift apart quietly.

---

## The rule that shapes everything

DCS events are lossy. Kills go unreported, `dead` fires with no `kill`, units
disappear with no event at all. So the engine treats the two uplink streams
differently:

> **`state` snapshots are the only thing that may record a loss.
> `event` frames supply attribution and nothing else.**

Concretely: delete every `event` frame from a campaign log and the engine must
finish in *identical* state, with the loss ledger the same length, the same
entries, in the same order — differing only in each loss's `attribution`,
which becomes `"unknown"`. A loss is never dropped for want of an explanation.

This is checkable, and it is checked. The proof that counts is in
`tests/test_e2e.py`, which runs the real engine, the real transport and the
real harness over a real socket in **one process**, where a single event loop
orders everything — so the with-events and no-events runs are genuinely the
same war, and any difference is the rule breaking.

It can also be checked by hand across two processes:

```
$ python tools/diff_saves.py saves/with-events.json saves/no-events.json
campaign state identical: 10 loss record(s)
  loss 0: attribution 'hit/red_sa6_bassel/9M33' -> 'unknown'
  loss 1: attribution 'hit/cmp_0005/GBU-38' -> 'unknown'
  ...
```

(Losses the engine resolved on paper — here, red's raid on Incirlik, which
nobody was watching — carry `unobserved` in both saves and are not listed:
that label is not event-derived, so dropping events cannot touch it.)

But read that result with care. Two processes are not in lockstep: the
harness advances mission time while the engine's replies are still in flight,
so *when* a spawn lands depends on OS scheduling. Run unpaced (`--speed 0`)
and two runs with **identical** inputs — events on in both — disagree about
one time in three, with the strike landing twenty or thirty seconds apart.
`diff_saves` cannot tell that apart from the rule breaking, and will say
RECONCILIATION BROKEN. So before trusting a failure by hand, diff two
identical with-events runs first; if *those* disagree, the rig is racing and
the comparison means nothing. Pacing (the harness default) makes divergence
rare, not impossible. For an answer you can rely on, use the test.

Determinism serves the same end. Nothing in campaign logic reads a wall clock
or an unseeded random source, and chance comes from one seeded
`random.Random` whose state round-trips through the save. Time arrives two
ways (docs/design.md, section 2). While DCS is connected it arrives on frames
as mission time, and the engine never moves itself. While it is not, the war
carries on in fixed five-second paper steps (`Campaign.advance`). The wall
clock decides only *how many* steps the transport delivers, and never what a
step is. So a war is a pure function of its log and its step count, however
the host's scheduling chunked them, and it replays from both.

---

## Running the offline loop

Python 3.14, standard library only. No dependencies, nothing to install.

In one shell, start the engine:

```
python -m campaign --port 7777 --save saves/campaign.json --theater slice
```

`--theater` picks the map a *new* campaign starts on: `syria`, the default,
is the real Syria map (docs/design.md, "Theater: the Syria map"); `slice` is
the two-base test slice the unit tests are written against, and the one this
walkthrough and the harness's scripted observer describe. A save carries its
own theater, so the flag is ignored once the save exists. `--start
YYYY-MM-DDTHH:MM` sets a new war's local date and time on the theater's clock
(default 2025-09-22T06:00), which on Syria decides when the sun lets it
strike (docs/design.md, section 6); a save keeps its own too.

In another, run the DCS stand-in. It speaks the mission-client half of the
protocol over a real socket, flies a scripted observer out of Incirlik, obeys
the spawns it is given, and resolves any strike that reaches a target it
holds:

```
python tools/fake_dcs.py --port 7777
```

What a healthy run looks like (abridged):

```
sync: campaign_time=0 state=30s observer=5s bubble=75000m
MSG [blue] VIPER on task: strike with SEAD on Latakia Fuel Depot, TOT 1471.
spawned cmp_0001 (munitions_storage_medium, structure) 4 unit(s), 0 waypoint(s)
spawned cmp_0003 (Patriot_site, ground) 5 unit(s), 0 waypoint(s)
spawned cmp_0006 (F-16C_sead_harm, plane) 2 unit(s), 3 waypoint(s)
spawned cmp_0005 (F-16C_strike_jdam, plane) 2 unit(s), 3 waypoint(s)
spawned cmp_0008 (Su-24M_sead_kh58, plane) 2 unit(s), 3 waypoint(s)
spawned cmp_0007 (Su-24M_strike_fab, plane) 2 unit(s), 3 waypoint(s)
spawned cmp_0004 (SA-6_Kub_site, ground) 5 unit(s), 0 waypoint(s)
spawned cmp_0002 (fuel_depot_medium, structure) 4 unit(s), 0 waypoint(s)
SEAD at t=1005: cmp_0006 -> cmp_0004, 0 unit(s) destroyed, 5 left
SEAD at t=1049: cmp_0008 -> cmp_0003, 0 unit(s) destroyed, 5 left
despawned cmp_0008 (left_bubble)
despawned cmp_0007 (left_bubble)
strike at t=1407: cmp_0005 -> cmp_0002, target destroyed, flight 1/2 remaining
MSG [blue] Latakia Fuel Depot destroyed.
MSG [blue] All assigned strategic targets destroyed.
MSG [blue] Incirlik Patriot engaged: 2 enemy aircraft down.
MSG [blue] VIPER SEAD off target, 2 aircraft egressing.
MSG [blue] VIPER off target, 1 aircraft egressing.
spawned cmp_0008 (Su-24M_sead_kh58, plane) 2 unit(s), 2 waypoint(s)
despawned cmp_0006 (mission_complete)
MSG [blue] VIPER SEAD recovered, 2 aircraft home.
despawned cmp_0005 (mission_complete)
MSG [blue] VIPER recovered, 1 aircraft home.
```

(Abridged from a run against an engine started with `--time-compression 0`,
which fragged VIPER before the harness connected, so the player hears the
package briefed on connecting rather than fragged. `cmp_0001` and `cmp_0003`
are blue's own munitions storage area and Patriot battery at Incirlik, in
the bubble because the player starts there. VIPER is a package of two
elements: `cmp_0006`, its SEAD two-ship, two minutes ahead of `cmp_0005`, the
strike. `cmp_0008` and `cmp_0007` are red's SEAD element and Su-24M raid out
of Bassel al-Assad against the storage area; the player's side is never told
of them, and they pass through the bubble only where the two routes cross.
Both SEAD elements meet an enemy SAM in the bubble and engage it; the
harness's scripted SEAD pass fires every missile the element carries
(`--loadout`, two an aircraft, `--sead-shots` to fire fewer) and kills
nothing unless `--sead-kills` says so. Whatever it kills comes back by
snapshot, and so do the missiles: each snapshot carries the ammunition
aboard (docs/protocol.md, `state`). By red's time on target the observer is
chasing VIPER near Latakia, so nobody is watching Incirlik, and the engine
flies the raid on paper. Red's Kh-58Us out-range the Patriot, so its SEAD
element fires from standoff and is not shot at; but in DCS it fired all four
at the Patriot at t=1049, the snapshots said so, and the paper has none left
to fire and buys no suppression (docs/design.md, section 5). The Patriot
meets both Su-24Ms at its full kill probability and kills both, and the
storage area is untouched. Run the harness with `--loadout 0` — DCS as it
ships today, the client's pylons empty — and nothing is fired in the sim at
all: VIPER reaches the depot with no bombs and destroys nothing, and red's
SEAD element, having spent nothing in DCS, fires its whole load on paper at
its TOT, takes two Patriot launchers and suppresses what is left, and both
Su-24Ms get through to hit the storage area. `cmp_0004`
is the SA-6 covering the approach to Latakia; the observer brings it into
the bubble, so here DCS,
not the engine, decides what it shoots down, and the harness's scripted loss
stands in for that. The war ends the moment the depot does; red's raid,
already airborne, flies out its sortie. Its SEAD element passes back
through the bubble on the way home, and is spawned with two waypoints and no
task, not its SEAD task: its part was resolved at the TOT, once.)

and in the save afterwards: the depot at `units_alive: 0`, blue's strike
squadron at 11 of 12 airframes with 1 lost, 2 GBU-38 expended and 2 lost with
the jet that carried them, its SEAD squadron whole with 4 AGM-88C expended,
red's strike squadron at 10 of 12 with 4 FAB-500 lost with its jets, red's
SEAD squadron whole with 4 Kh-58U expended, both packages closed, no
reservation left open, and
`war_result` naming red as defeated. Start the engine again on the same save
and it carries on from there — which, the war being over, means it plans
nothing.

### With DCS closed

The war does not wait for you. With no mission client connected, the engine
advances the campaign itself in five-second paper steps at
`--time-compression N` campaign seconds per wall second (default 1, real
time; 0 makes the war wait for DCS). Everything is outside the bubble then,
so the engine resolves everything on paper: strikes against their targets,
SEAD missiles against the sites, and every flight against the air defences on
its route. When DCS connects again, mission time zero is pinned to wherever
the war has got to.

To catch a war up without a server, for testing or overnight:

```
$ python -m campaign --save saves/campaign.json --simulate 86400
simulated 17280 paper step(s) of 5s; campaign clock 0s -> 86400s
```

Run against a fresh save, that day is over in a little more than an hour of
it, and blue wins it. Every package on both sides flies escorted, and every
SEAD element fires from standoff, so no SEAD jet is shot at. Red's first
raid's Kh-58Us destroy two Patriot launchers, and both Su-24Ms get through
and destroy two of the storage area's four units. Blue's first sortie's
HARMs destroy one SA-6 launcher, and its strikers one of the depot's four
units. Red's second raid takes a third launcher and misses the storage
area. Blue's second takes a second SA-6 launcher and finishes the depot at
4000 s. Nobody loses an aircraft. Other seeds lose it, and bleed: at seed 20
each side's strike element loses a jet and red finishes the storage area at
3945 s. That is the point of sections 3 and 4 of docs/design.md: an
unwatched war can be lost as well as won, and the enemy is fighting it
too.

Three flags earn their keep:

| flag | what it proves |
|------|----------------|
| `--drop-events` | sends every snapshot and not one event. Diff the save against a normal run: only `attribution` may differ. |
| `--restart-at T` | hard-drops the socket at mission time `T` and reconnects, the way a DCS restart does. The engine re-syncs and re-issues every live spawn; the campaign clock does not lose a second. |
| `--seed N` | the harness's only source of randomness. |

Tests:

```
python -m unittest discover -s tests -t .
```

`tests/test_e2e.py` runs the real engine, the real asyncio transport and the
real harness over a loopback socket, and is the only test that can fail
because two layers disagreed rather than because one of them is wrong.

`tests/test_mission_client.py` executes the real `mission/campaign_client.lua`
in Lua 5.1 against a mocked DCS and a real `Campaign` over a real socket. It
needs one development dependency, and skips cleanly without it:

```
pip install -r requirements-dev.txt
```

Skipping is the dangerous default, because the Lua is the half of this system
that has never run inside DCS. Install it.

CI (`.github/workflows/ci.yml`) runs the suite on every push and pull request
to `main`, on Windows and Linux, twice: once with the standard library alone,
where the Lua-backed tests must skip, and once with `lupa`, where **nothing may
skip** — `tools/ci_tests.py` fails the run if a single test does, so a broken
`lupa` install cannot report green while testing no Lua at all. Run the same
check locally with `python tools/ci_tests.py --lua required`.

The engine also ships as a standalone Windows exe that needs no Python: see
[docs/building.md](docs/building.md). CI builds it after the tests pass and
keeps it as a run artifact for seven days.

What no test here can settle is *content* — the unit type strings, static
categories, task schemas and pylon CLSIDs the client hands to DCS. The mock
accepts any well-formed table, so the suite is green whether or not DCS agrees.
`mission/validate_templates.lua` answers that in one run inside the sim; see
[mission/VALIDATION.md](mission/VALIDATION.md).

To run inside DCS for real, see `mission/README.md` — it covers desanitising
`MissionScripting.lua`, which is required and has real consequences.

---

## Layout

| path | what it is |
|------|------------|
| `docs/protocol.md` | the wire contract. Authoritative. |
| `campaign/protocol.py` | frame types, encode/decode, framing. The executable half of the spec. |
| `campaign/api.py` | the `CampaignEngine` seam between campaign logic and sockets. |
| `campaign/campaign.py` | the brain. Owns identity, time, chance, and all state. |
| `campaign/theater.py` | the map: airbases, targets, threat sites, who has lost, and the one correct way to measure distance. |
| `campaign/oob.py` | order of battle. Inventory is conserved, not merely decremented. |
| `campaign/planner.py` | the minimal ATO: select a target, build a package of elements (strike, and SEAD when the route is exposed), schedule a TOT — for whichever side asks. |
| `campaign/sun.py` | where the sun is: NOAA's solar formula, for the daylight rule. |
| `campaign/bubble.py` | what DCS is allowed to know about, with hysteresis so it does not thrash. |
| `campaign/attrition.py` | reconciliation. The load-bearing module. |
| `campaign/resolver.py` | what happened where nobody was looking: unobserved strikes, SEAD missiles and suppression, and flights through air defences. |
| `campaign/audit.py` | the reconciliation rule as something a machine can check. |
| `campaign/server.py` | asyncio TCP transport. Owns every socket and paces the offline clock; knows nothing about war. |
| `campaign/__main__.py` | `python -m campaign`. Wiring only. |
| `mission/campaign_client.lua` | the DCS client: socket, tick loop, spawning, snapshots. |
| `tools/fake_dcs.py` | the offline DCS stand-in. |
| `tools/diff_saves.py` | compares two saves under the reconciliation rule. |

---

## Status, honestly

**What works, and has been run end to end offline:**

- Both sides plan. Every coalition with a squadron and an airbase frags
  packages against the other's strategic targets in priority order, in
  coalition-name order, under the same inventory, attrition, threat and
  authority rules. `player_coalition` decides only who is told what; the
  side nobody flies is told nothing. Red's raids are resolved, on paper or in
  DCS, exactly as blue's strikes are.
- A sortie rate (docs/design.md, section 6). A side flies as many packages at
  once as its squadrons are ready for -- never two against one target, never
  one squadron in two -- and a squadron is ready only when its jets are
  turned round, it has sorties left in its daily limit, and, if it flies by
  day, the time on target is in daylight by NOAA's solar formula. Syria's
  turnaround and daily rates are published figures; the slice's squadrons
  are unconstrained through the same code. The engine logs a warning when a
  connecting mission's time of day is not the campaign's.
- Target selection, a TOT computed from distance, and a package of
  **elements** (docs/design.md, section 5): a 2-ship strike and, when its
  route enters a live enemy SAM envelope and its base has anti-radiation
  missiles, a 2-ship SEAD element on the same track 120 s ahead. Each element
  is its own entity — spawn id, reservation, bubble membership, state — so
  the SEAD element can be shot down, scrubbed or run dry while the strike
  flies on. Both sides fly SEAD: blue's F-16Cs with AGM-88C, red's Su-24Ms
  with Kh-58U. Every element's airframes and munitions are reserved up front.
- SEAD on paper, in order at the TOT: the SEAD element flies its exposure
  to any site its missile does not out-range (one it out-ranges, it engages
  from standoff and is not shot at by), its survivors' missiles are rolled
  against the sites (and may destroy
  units), each surviving SEAD aircraft halves each engaged site's kill
  probability for the strike element, the strike flies its exposure, and its
  survivors release. Observed, the SEAD element is tasked in DCS
  (`EngageTargets` against air defences, `AttackGroup` on the named sites)
  and the sim decides. The mixed-authority rule — SEAD acts on paper only
  when it and the site are both outside DCS at the TOT, and then fires only
  the missiles the sim has not already spent, as the ammunition in the
  `state` snapshots counts them — is in section 5 and tested in every
  combination, with scripted dice that account for every draw.
- A package with only a strike element is the one-flight package exactly:
  eleven wars recorded from the engine before elements existed replay frame
  for frame and die for die (`tests/test_single_element.py`), apart from one
  deliberate fix — a flight spawned after its TOT is now sent home with no
  attack task, where before it could strike its target a second time.
- Bubble instantiation and removal, hysteretic, driven only by observers.
- Losses reconciled from `state` snapshots — partial attrition, destruction,
  and the case that matters most: a group that simply stops being reported.
- Attribution from `event` frames, and a proven-identical campaign without them.
- Target damage, airframe losses and munition expenditure charged to a
  conserved inventory that cannot leak on a scrubbed or destroyed package.
- JSON save and reload, including the RNG state and the spawn-id counter, so a
  restarted engine continues the same war rather than a similar one.
- Reconnect after a sim restart: re-sync, re-issue every live spawn at the
  unit count attrition has already recorded, and rebase the mission clock so
  the campaign loses no time.
- The war with DCS closed: the engine advances itself in fixed paper steps at
  `--time-compression`, or `--simulate` fast-forwards a save with no server.
  A client connecting afterwards joins the war where it is.
- Out-of-bubble resolution, both ways: a strike nobody is watching is rolled
  against its target, and the flight is first rolled against every live enemy
  air-defence site its route passes. Paper losses go through the same tracker
  and the same conserved inventory as observed ones. A flight DCS is holding
  is never rolled.
- One threat site a side — an SA-6 under blue's strike route, a Patriot under
  red's — instantiated in the bubble like anything else.
  `mission/validate_templates.lua` probes both templates, red's Su-24M, blue's
  storage-area statics and both sides' SEAD two-ships, for the country the
  client spawns each for, and the two tasks a SEAD element is given under
  its `SEAD` group task.
- The war ends. When one side's strategic targets are all destroyed, nobody
  plans again, packages still on the ground are stood down, and the humans
  are told they won, lost, or that nobody did (docs/design.md, section 4).
  The result is saved. The humans' side hears damage to its own targets and
  sites, and its paper kills on enemy raids, but never the enemy's tasking.

**What is stubbed, faked or deliberately missing:**

- **A real threat model.** Each site is one ground radius and one flat
  per-aircraft kill probability, with no altitude bands, terrain masking or
  EW, and each anti-radiation missile is one launch range whatever the
  release altitude. Exposure is rolled once, at the TOT, for the whole route.
  Seams: `theater.ThreatSite`, `oob.ANTI_RADIATION_LAUNCH_RANGE`,
  `resolver.resolve_exposure`.
- **No deconfliction between packages**, and SEAD is the only support
  element: no DEAD, no routing around an envelope, no escort, tanker or
  AWACS. Two rules stand in for deconfliction (one open package a target,
  one open package a squadron); packages are not sequenced against each
  other, one package's SEAD never covers another's strike, and a strike
  whose SEAD squadron is busy or out of sorties goes in alone. Seams:
  `Campaign._plan`, `Campaign._plan_for`, `planner.build_package`,
  `planner`'s module docstring.
- **Day only, and DCS's clock is not set.** Every Syria squadron strikes by
  day; night-capable squadrons are a seam (`oob.SortieRate.day_only`). A DCS
  mission starts at its editor time whatever time the campaign has reached;
  the engine only logs the mismatch. Seam: `Campaign._check_mission_clock`.
- **Nothing plans to destroy a threat site.** SEAD missiles take units off a
  site only on the way to a strike's target, and a site keeps its full kill
  probability until its last unit goes: units are counted, not typed, so a
  battery down to its radar fires as hard as a whole one. Seams:
  `resolver.ARM_PK`, `theater.ThreatSite`.
- **No ground war**, front line, base capture or logistics network. Seams:
  `theater.Airbase`, `theater.Theater`, `oob.SideInventory`.
- **Strike and SEAD only, on both sides.** Each side has one strike
  squadron, one SEAD squadron and nothing that fights in the air: no CAP, no
  escort, no intercept. A red raid and a blue strike pass each other
  mid-route without noticing. Seams: `oob.build_slice_oob`,
  `Campaign._plan_for`.
- **The content is placeholder and symmetric by choice.** Both sides have
  twelve strike airframes and forty-eight bombs, eight SEAD airframes and
  twenty-four missiles, one four-unit target and one site with the same flat
  Pk; the missile Pk (0.25) and the suppression (half the site's Pk per
  surviving SEAD aircraft) are as uncalibrated. The war is short — usually
  decided within two or three sorties a side. A SEAD element is shot at on
  paper only by a site its missile does not out-range, and the launch ranges
  and engagement radii are published maxima (AGM-88C 148 km, Kh-58U 250 km,
  SA-6 24 km, Patriot 160 km), so on this map both sides' SEAD fires from
  standoff. At these numbers SEAD cuts the strike element's losses from 0.27
  to 0.08 aircraft on a first sortie (over 400 seeds) and costs no SEAD
  aircraft on paper, so an escorted package loses fewer aircraft in total
  than one sent alone. Before standoff it cost more than it saved: the SEAD
  element flew into the unsuppressed site first. Red's standoff rests on the
  Kh-58U's published 250 km; the original Kh-58's 120 km would not out-range
  the Patriot. docs/design.md, section 5, has the figures and sources. That
  is what uncalibrated numbers give, not a model of anything.
- **No pilots.** A package draws anonymous airframes; ejections and deaths are
  events nobody records. Seam: `oob.Squadron`.
- **Placeholder DCS templates.** `TEMPLATES` in `campaign_client.lua` maps
  wire template names to unit types with **empty pylons** — loadout CLSIDs are
  DCS-version specific and a wrong guess is a silently unarmed strike. Fill
  them from mission-editor exports before flying this for real; until then
  the SEAD jets carry no missiles in DCS and suppress nothing there, and fire
  their whole load on paper if their TOT falls outside the bubble. The DCS
  weapon type names the client reports ammunition under (`WEAPON_NAMES`) are
  guesses too; `validate_templates.lua` records the real ones. The Patriot
  battery is a radar and four launchers only: a DCS Patriot is normally also
  given an ECS and power, and a template carries one lead unit type, so
  whether this one engages anything in the sim is unverified.
- **Invented coordinates, in the slice.** The slice's positions in
  `theater.py` are plausible, not surveyed. The Syria theater's airbases are
  the DCS map's own, from pydcs's terrain data; its targets and sites are
  game-design placements at stated offsets from them, not real facilities.
- **The Lua client has not been run against a real DCS** in producing this
  slice — there is no DCS on the machine that built it. It is written against
  the documented scripting API and its interfaces are reconciled against the
  engine by inspection, not by execution. Everything Python has been executed.

**Known rough edges:**

- A package already airborne when the war ends flies out its sortie, and what
  it achieves is recorded after the result is fixed (docs/design.md, section
  4, says why it is not recalled). A beaten side's last raid can still damage
  the winner.
- Both sides draw callsigns from one list, so a red and a blue package can
  share one. Nobody is ever told a red callsign, so only the save shows it.
- A SEAD element DCS is holding at the TOT buys an unwatched strike no
  suppression: a snapshot shows site units destroyed, never suppression, and
  crediting both would count one sortie twice (docs/design.md, section 5).
  It errs against the players where the bubble's edge falls between the two
  elements, 17 km apart.
- `tools/fake_dcs.py` resolves a SEAD element against a named site it holds
  in range with a scripted outcome too (`--sead-kills`, default 0, so the
  standard runs are the ones they were).
- `tools/fake_dcs.py` resolves any strike that reaches a target it holds,
  red's included, with the same scripted outcome (`--target-pk`,
  `--flight-losses`). After `--restart-at` its player respawns at Incirlik,
  under red's raid, so a restarted run can see red's strike that an
  uninterrupted one resolves on paper — a different, and correct, war.

- The engine reads any `state` frame as a complete census of what is
  instantiated, which is what `docs/protocol.md` specifies. A client that
  sends a partial one will have its missing groups written off as vanished.
  That is the correct reading, but it makes partial reports a trap.
- `CampaignEngine.tick(now)` is documented as taking monotonic wall seconds,
  and the campaign deliberately ignores the value: wall seconds are not
  mission seconds. `tick` is a pulse. The campaign advances on the mission
  time carried by frames while DCS is connected, and on `advance` while it is
  not. See `Campaign.tick`.
- When the war moves on without DCS, the players' last positions are
  forgotten, so the bubble is empty when DCS comes back until the first
  observer frame arrives, 5 s later. A restart with no paper time in between
  keeps them.
