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
  whole loop, including the transport, runs in under three seconds in CI with
  no DCS installed.
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

This is checkable, and it is checked, both in `tests/test_e2e.py` and by hand:

```
$ python tools/diff_saves.py saves/with-events.json saves/no-events.json
campaign state identical: 5 loss record(s)
  loss 0: attribution 'hit/red_sa6_bassel/9M33' -> 'unknown'
  loss 1: attribution 'hit/cmp_0002/GBU-38'     -> 'unknown'
  ...
```

Determinism serves the same end. Nothing in campaign logic reads a wall clock
or an unseeded random source: time arrives on frames as mission time, chance
comes from one seeded `random.Random` whose state round-trips through the
save. A whole war replays from its log.

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

What a healthy run looks like:

```
sync: campaign_time=0 state=30s observer=5s bubble=75000m
MSG [blue] VIPER on task: strike on Latakia Fuel Depot, TOT 1471.
spawned cmp_0002 (F-16C_strike_jdam, plane) 2 unit(s), 3 waypoint(s)
spawned cmp_0001 (fuel_depot_medium, structure) 4 unit(s)
strike at t=1407: cmp_0002 -> cmp_0001, target destroyed, flight 1/2 remaining
MSG [blue] Latakia Fuel Depot destroyed.
MSG [blue] VIPER off target, 1 aircraft egressing.
despawned cmp_0002 (mission_complete)
MSG [blue] VIPER recovered, 1 aircraft home.
MSG [blue] All assigned strategic targets destroyed.
```

(`on task` rather than `fragged` because the engine planned the package before
the client connected; a client that is already there hears it fragged live.)

and in the save afterwards: the depot at `units_alive: 0`, the
squadron at 11 of 12 airframes with 1 lost, 2 GBU-38 expended and 2 lost with
the jet that carried them, the package `complete`, and no reservation left
open. Start the engine again on the same save and it carries on from there.

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
| `campaign/theater.py` | the map: airbases, targets, and the one correct way to measure distance. |
| `campaign/oob.py` | order of battle. Inventory is conserved, not merely decremented. |
| `campaign/planner.py` | the minimal ATO: select a target, build a package, schedule a TOT. |
| `campaign/bubble.py` | what DCS is allowed to know about, with hysteresis so it does not thrash. |
| `campaign/attrition.py` | reconciliation. The load-bearing module. |
| `campaign/audit.py` | the reconciliation rule as something a machine can check. |
| `campaign/server.py` | asyncio TCP transport. Owns every socket, knows nothing about war. |
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

**What is stubbed, faked or deliberately missing:**

- **Out-of-bubble strike resolution.** A strike nobody is near currently does
  no damage at all, because damage may only come from a snapshot. This is the
  one place the campaign's seeded RNG is meant to matter, and it is not
  written yet. Seam: `Campaign._release_weapons`.
- **One package at a time.** No multi-package deconfliction, no SEAD, escort,
  tanker or AWACS. Seams: `Campaign._plan`, `planner`'s module docstring.
- **No ground war**, front line, base capture or logistics network. Seams:
  `theater.Airbase`, `theater.Theater`, `oob.SideInventory`.
- **No red air.** Red flies nothing; blue plans against no threat. Seam:
  `oob.build_slice_oob`.
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
  mission seconds. `tick` is a pulse, and the campaign advances on the mission
  time carried by frames. See `Campaign.tick`.
