# DCS campaign engine

A Falcon BMS-style dynamic campaign for DCS World, with the campaign brain
running **outside** the simulator.

This repository is the first vertical slice. Its only job is to prove that one
full loop closes: the engine picks a target, frags a package against it,
instantiates the flight when a human is near enough to see it, learns what
happened from ground-truth snapshots, charges the losses to a finite
inventory, writes the war to disk, and picks it up again from there.
Everything that would make it a *game* — a ground war, a front line, red air,
pilots — is deliberately absent, and marked with `TODO(seam):` where it will
attach.

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
  with no DCS installed. (There is no CI yet; `python -m unittest discover -s
  tests -t .` is the whole of the check.)
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
campaign state identical: 5 loss record(s)
  loss 0: attribution 'hit/red_sa6_bassel/9M33' -> 'unknown'
  loss 1: attribution 'hit/cmp_0003/GBU-38' -> 'unknown'
  ...
```

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
python -m campaign --port 7777 --save saves/campaign.json
```

In another, run the DCS stand-in. It speaks the mission-client half of the
protocol over a real socket, flies a scripted observer out of Incirlik, obeys
the spawns it is given, and resolves the strike:

```
python tools/fake_dcs.py --port 7777
```

What a healthy run looks like (abridged):

```
sync: campaign_time=0 state=30s observer=5s bubble=75000m
MSG [blue] VIPER on task: strike on Latakia Fuel Depot, TOT 1471.
spawned cmp_0003 (F-16C_strike_jdam, plane) 2 unit(s), 3 waypoint(s)
spawned cmp_0002 (SA-6_Kub_site, ground) 5 unit(s), 0 waypoint(s)
spawned cmp_0001 (fuel_depot_medium, structure) 4 unit(s), 0 waypoint(s)
strike at t=1407: cmp_0003 -> cmp_0001, target destroyed, flight 1/2 remaining
MSG [blue] Latakia Fuel Depot destroyed.
MSG [blue] VIPER off target, 1 aircraft egressing.
despawned cmp_0002 (left_bubble)
despawned cmp_0003 (mission_complete)
MSG [blue] VIPER recovered, 1 aircraft home.
MSG [blue] All assigned strategic targets destroyed.
```

(`on task` rather than `fragged` because the engine planned the package before
the client connected; a client that is already there hears it fragged live.
`cmp_0002` is the SA-6 battery covering the approach to Latakia. The observer
chasing the flight brings it into the bubble, so here DCS, not the engine,
decides what it shoots down. The harness's scripted loss stands in for that.)

and in the save afterwards: the depot at `units_alive: 0`, the
squadron at 11 of 12 airframes with 1 lost, 2 GBU-38 expended and 2 lost with
the jet that carried them, the package `complete`, and no reservation left
open. Start the engine again on the same save and it carries on from there.

### With DCS closed

The war does not wait for you. With no mission client connected, the engine
advances the campaign itself in five-second paper steps at
`--time-compression N` campaign seconds per wall second (default 1, real
time; 0 makes the war wait for DCS). Everything is outside the bubble then,
so the engine resolves everything on paper: strikes against their targets,
and the strike flights against the air defences on their route. When DCS
connects again, mission time zero is pinned to wherever the war has got to.

To catch a war up without a server, for testing or overnight:

```
$ python -m campaign --save saves/campaign.json --simulate 86400
simulated 17280 paper step(s) of 5s; campaign clock 0s -> 86400s
```

Run against a fresh save, that day is four sorties. The depot is flattened,
and one F-16 falls to the SA-6 on the way in, with its two bombs. That is the
point of section 3 of docs/design.md: an unwatched war can be lost as well as
won.

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
| `campaign/theater.py` | the map: airbases, targets, threat sites, and the one correct way to measure distance. |
| `campaign/oob.py` | order of battle. Inventory is conserved, not merely decremented. |
| `campaign/planner.py` | the minimal ATO: select a target, build a package, schedule a TOT. |
| `campaign/bubble.py` | what DCS is allowed to know about, with hysteresis so it does not thrash. |
| `campaign/attrition.py` | reconciliation. The load-bearing module. |
| `campaign/resolver.py` | what happened where nobody was looking: unobserved strikes, and flights through air defences. |
| `campaign/audit.py` | the reconciliation rule as something a machine can check. |
| `campaign/server.py` | asyncio TCP transport. Owns every socket and paces the offline clock; knows nothing about war. |
| `campaign/__main__.py` | `python -m campaign`. Wiring only. |
| `mission/campaign_client.lua` | the DCS client: socket, tick loop, spawning, snapshots. |
| `tools/fake_dcs.py` | the offline DCS stand-in. |
| `tools/diff_saves.py` | compares two saves under the reconciliation rule. |

---

## Status, honestly

**What works, and has been run end to end offline:**

- Target selection, a 2-ship package, airframes and munitions reserved up
  front, a TOT computed from distance.
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
- One threat site, an SA-6 battery under the strike route, instantiated in the
  bubble like anything else. `mission/validate_templates.lua` probes its
  template.

**What is stubbed, faked or deliberately missing:**

- **A real threat model.** Each site is one ground radius and one flat
  per-aircraft kill probability, with no altitude bands, terrain masking or
  EW. Exposure is rolled once, at the TOT, for the whole route. Seams:
  `theater.ThreatSite`, `resolver.resolve_exposure`.
- **One package at a time**, and the planner does not plan around threats:
  no SEAD or DEAD, no routing around an envelope, no escort, tanker or AWACS.
  Seams: `Campaign._plan`, `Theater.live_threats_along`, `planner`'s module
  docstring. SEAD packages are docs/design.md section 5.
- **Nothing on paper can hurt a threat site.** A site loses units only to
  snapshots, so only when DCS holds it and something in the sim shoots at it.
  That is DEAD, section 5.
- **No ground war**, front line, base capture or logistics network. Seams:
  `theater.Airbase`, `theater.Theater`, `oob.SideInventory`.
- **No red air.** Red flies nothing and plans nothing. Its only teeth are the
  SA-6. Seam: `oob.build_slice_oob`; docs/design.md section 4.
- **No pilots.** A package draws anonymous airframes; ejections and deaths are
  events nobody records. Seam: `oob.Squadron`.
- **Placeholder DCS templates.** `TEMPLATES` in `campaign_client.lua` maps
  wire template names to unit types with **empty pylons** — loadout CLSIDs are
  DCS-version specific and a wrong guess is a silently unarmed strike. Fill
  them from mission-editor exports before flying this for real.
- **Invented coordinates.** The Syria positions in `theater.py` are plausible,
  not surveyed. Swapping in extracted map coordinates is a content change.
- **The Lua client has not been run against a real DCS** in producing this
  slice — there is no DCS on the machine that built it. It is written against
  the documented scripting API and its interfaces are reconciled against the
  engine by inspection, not by execution. Everything Python has been executed.

**Known rough edges:**

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
