# Template validation

`campaign_client.lua` names a lot of DCS **content** that nothing outside a
DCS install can check: the unit types `F-16C_50` and `Su-24M`, the SA-6
battery's `Kub 1S91 str` radar and `Kub 2P25 ln` launchers and the Patriot
battery's `Patriot str` radar and `Patriot ln` launchers under a
`Ground Nothing` group task, the static type/category pairs `Tank` /
`Fortifications` and `Warehouse` / `Warehouses`, three
`country.id` values, the `Bombing` and
`AttackGroup` task schemas, the `SEAD` group task and the `EngageTargets`
task a SEAD element searches with, the waypoint `type`/`action`/`alt_type`
strings, and a payload whose `pylons` table is empty. The offline suite is green
whether or not DCS would accept any of them, because the mock in
`tests/dcsmock/` accepts any well-formed table.

`validate_templates.lua` is the other half. Drop it into a mission, fly
nothing, and it asks DCS about all of them in one run — instead of finding
out one crash at a time, a week apart, in the middle of a sortie.

| file | what it is |
|---|---|
| `mission/validate_templates.lua` | the validator. Standalone; needs neither the client nor the engine. |
| `tools/parse_validation.py` | reads its output and exits non-zero if something required failed. |
| `tests/test_validation.py` | proves the instrument works, under the same mock the client is tested in. |

---

## 1. What it can and cannot tell you

**It can tell you:**

- whether `coalition.addGroup` / `coalition.addStaticObject` *refuse* each
  template, with the error text;
- whether they accept it and then **create nothing** — the silent failure,
  where DCS logs internally and returns without raising. This is reported
  separately (`ORPHAN`), because an instrument that called it a pass would be
  worse than no instrument;
- whether the object is still retrievable one tick later, as well as
  immediately. The client checks immediately, and if those two ever disagree
  its check is looking a frame too early;
- whether each `country.id` the client maps a coalition onto exists in this
  build, and whether a group filed under it spawns;
- whether the SA-6 site spawns as the client builds it: a red (`RUSSIA`)
  ground group on the ground, radar as unit 1, launchers after, no payload;
  and the same of the Patriot site, as a blue (`USA`) one, and of the Syria
  theater's SA-11 and SA-15 (red) and Hawk and Roland (blue) sites. Its four static
  templates are probed as one object each, like the slice's;
- whether each of the other templates spawns for the country the client
  spawns it for: red's Su-24M strike two-ship for `RUSSIA`, red's fuel depot
  for `RUSSIA`, blue's munitions-storage statics for `USA`, and each side's
  SEAD two-ship (`F-16C_sead_harm` for `USA`, `Su-24M_sead_kh58` for
  `RUSSIA`) under the group task `SEAD`. DCS can refuse a type to one country
  and accept it for another, so a probe filed under the wrong side proves
  nothing;
- whether the two tasks the client gives a SEAD element are accepted under
  that group task: `EngageTargets` against `"Air Defence"`
  (`task.EngageTargets_SEAD`), and `AttackGroup` against a live SA-6-built
  ground group (`task.AttackGroup_SEAD`). Accepted is not the same as
  engaging, as below;
- whether the waypoint `type`/`action` pairs, the `alt_type` strings, the
  group-level task strings, and the `Bombing` and `AttackGroup` task tables
  are accepted — including the ramp-start pair (`TakeOffParking` /
  `From Parking Area` with an `airdromeId`) the client puts on waypoint 1
  when the engine names an airdrome;
- **whether a named weapon CLSID actually loads**, by reading `Unit.getAmmo`
  off the spawned jet rather than trusting that the spawn succeeded. An
  unknown CLSID is not an error in DCS — it yields an empty pylon, which is
  exactly how a strike package ends up unarmed with nobody noticing.

**It cannot tell you:**

- whether a template is *right* — only whether DCS accepts it. A `CAP` flight
  that spawns with the wrong livery, the wrong fuel state or the wrong
  radio preset passes every case here;
- whether the AI will actually prosecute the task. `AttackGroup` being
  accepted is not the same as bombs falling on the target; that needs a
  flight flown, or at least watched;
- which CLSID you *should* use. The probe list is candidates, and a miss is
  reported as `UNKNOWN`, not as a failure. Supply real values (§4);
- anything about terrain. Statics and ground groups need land: a sea origin
  rejects every static case and both SAM cases, and the report would blame the
  template. Read the `origin` line before believing either failure (§3);
- whether either SAM site actually engages anything — the Patriot least of
  all, since it is built without the ECS and power units a DCS Patriot
  battery is usually given. Each is spawned and destroyed a
  tick later; that it would shoot is a question for a flown mission.

---

## 2. Running it

Add **one** trigger to any mission — a scratch mission is fine, and it is
cleaner than using the campaign mission:

- **Type:** `4 MISSION START`
- **Condition:** *none* (a `MISSION START` rule with `TIME MORE (1)` never
  runs at all — see `mission/README.md` §2)
- **Action:** `DO SCRIPT FILE` → `validate_templates.lua`

That is everything. It does not need `json.lua`, it does not need
`campaign_client.lua`, it does not need the engine running, and it does not
need LuaSocket. It needs no `MissionScripting.lua` change **for the log
output** — `env.info` always works.

Start the mission, wait about ten seconds, and stop it. Then read
`Saved Games/DCS/Logs/dcs.log`:

```
grep campaign-validate dcs.log > validation.txt
python tools/parse_validation.py validation.txt
```

or point the parser straight at `dcs.log`.

### Loading it beside the campaign client

Harmless, and sometimes what you want: with `campaign_client.lua` already
loaded, the validator cross-checks its own copy of the template values
against the live `CampaignClient.TEMPLATES` and reports any difference as
`DRIFT` (§6). Its objects are named `cmpval_*`, not `cmp_*`, so the running
client neither owns them nor reports them to the engine.

### The JSON file

If `io` is desanitised, the validator also writes a machine-readable report to
`<Saved Games>/DCS/Logs/campaign_validation.json`, and the parser reads that
form too. **`mission/README.md` tells you to leave `io` sanitised**, and that
advice stands — the JSON is a convenience, not the result. Its absence is not
a failure; the `SUMMARY` line says `json_written=0` and why.

---

## 3. Configuration

Define `CAMPAIGN_VALIDATE_CONFIG` in a `DO SCRIPT` action **before** loading
the file:

```lua
CAMPAIGN_VALIDATE_CONFIG = {
    -- Where probe objects are created, in DCS map coordinates (x, z).
    -- PICK LAND. Statics fail over water, and the report cannot tell that
    -- from a bad template.
    origin = {x = -3000, y = 41000},

    altitude = 5000,      -- air start height for probe flights, metres
    spacing  = 250,       -- metres between one case and the next
    tick     = 0.1,       -- seconds between ticks; two ticks per case
    cases_per_tick = 1,   -- raise only if you are not flying anything

    out_path = "C:/temp/campaign_validation.json",
    auto_start = true,

    clsids = { ... },     -- see below

    -- The airfield the ramp-start case parks on. Airdrome ids are per map.
    airdrome_id = nil,    -- nil: the lowest-id blue or neutral airdrome
}
```

The `waypoint.ramp_start` case ignores `origin`: a ramp start is only
meaningful on a real airdrome, so it parks on `airdrome_id`, or on the
lowest-id airdrome blue or neutral holds (the probe flies for USA, and a red
field would read as the pair being rejected). With none available it reports
`SKIP` and names the setting.

With no `origin`, the validator picks one and says which in the `BEGIN` line:
the first airbase it can find (offset 10 km, so nothing spawns on the ramp),
else a player's position, else `(0,0)` — and `(0,0)` is very possibly the sea,
which is why it labels itself `SET AN ORIGIN`.

---

## 4. The pylon probe

This is the part worth reading twice, because it is the one that fails
silently in production.

The client's `DEFAULT_PAYLOAD` has `pylons = {}`. A flight spawned from it
carries the gun and nothing else, so the strike package the engine frags
arrives over the target **unarmed** — and nothing in the log, the ack stream
or the campaign state says so. The engine still books the munitions as
expended, because expenditure is the engine's own accounting.

The probe therefore ignores the spawn result and reads `Unit.getAmmo` off the
jet a tick later, counting only munitions whose `desc.category` is not
`SHELL` — the gun is on every one of these aircraft whether a pylon loaded or
not, so counting it would call an unarmed jet armed.

Three kinds of pylon case:

| id | what it is | required |
|---|---|---|
| `pylon.baseline_empty` | the client's payload exactly as it ships. Expected to carry **nothing**. | yes |
| `pylon.1` | a control: a CLSID that cannot exist. Must come back empty — if a nonsense CLSID reports ammunition, the probe cannot tell loaded from unarmed and no other pylon line means anything. | yes |
| `pylon.2` … | candidate CLSIDs. A hit is `OK` with the type name; a miss is `UNKNOWN`, not a failure. | no |

The built-in candidates are **guesses, not knowledge**. Get real ones from
the mission editor: build an F-16C with the loadout you want, save the
mission, open the `.miz` (it is a zip), read `mission`, and copy the `pylons`
table out of the group's unit. Then:

```lua
CAMPAIGN_VALIDATE_CONFIG = {
    clsids = {
        {clsid = "{CLSID-FROM-YOUR-EXPORT}", pylon = 3, label = "GBU-38"},
        {clsid = "{ANOTHER-ONE}",            pylon = 7, label = "AIM-120C"},
    },
}
```

A CLSID that comes back `OK` here is one you can paste into
`campaign_client.lua`'s `TEMPLATES` with confidence. That is the point of the
exercise.

---

## 5. Reading the result

Every line is prefixed `[campaign-validate] `. One `BEGIN`, one `RESULT` per
case, one `SUMMARY`, one `END`.

```
[campaign-validate] RESULT status=OK required=1 id=template.F-16C_cap kind=group
    cleaned="1" created="1" retrievable="1" retrievable_next="1" units="2"
    wanted="2" label="F-16C_cap -> F-16C_50 x2 task 'CAP' skill 'High'" error=""
```

| status | meaning | failure? |
|---|---|---|
| `OK` | DCS accepted it and the object was really there. | no |
| `REJECTED` | the DCS call raised. `error=` carries the text. | **yes** |
| `ORPHAN` | accepted without raising, but `getByName` found nothing live. DCS took the table and made nothing. | **yes** |
| `DRIFT` | the validator's copy of a template no longer matches the client's. It is testing something the client does not use. | **yes** |
| `ERROR` | the case itself blew up, or an object could not be destroyed. | **yes** |
| `UNKNOWN` | could not be determined — an unrecognised CLSID, no `getAmmo` in this environment. | only with `--strict` |
| `SKIP` | not attempted — a missing prerequisite, or no client loaded to cross-check against. | only with `--strict` |

`required=1` marks the cases the campaign actually depends on. Alternatives
(`alt_type.RADIO`, the spare static type/category pairs, the candidate
CLSIDs) are `required=0`: they are there to give you a working replacement
when a required case fails.

`tools/parse_validation.py` prints the table and exits:

```
0   every required case passed
1   at least one required case failed
2   nothing readable in the file, or the run did not finish
```

`--strict` also fails on `UNKNOWN` and `SKIP`, which is what you want once
the run is supposed to have settled every question.

### After a run

Every object the validator creates is destroyed in the tick after it is
checked, and the `SUMMARY` line carries `leaked=N`. Anything other than
`leaked=0` is a bug in the validator; the mission is left clean otherwise, and
`tests/test_validation.py` asserts that — including on a run where every
single spawn failed.

---

## 6. Where the values come from, and the one refactor that would fix it

The validator must test *what the client actually asks DCS for*. Copying the
strings into a second list would make it drift, and a green drifted run is
worse than no run.

`campaign_client.lua` exports exactly one of its content tables:
`CampaignClient.TEMPLATES`. So:

- the validator's `SPEC.templates` is **cross-checked** against that live
  table whenever the client is loaded, field by field (`unit_type`, `count`,
  `task`, `skill`, `static`, `static_category`, `spread`, and the number of
  pylon entries), and any difference is a `DRIFT` failure;
- the client also exports `CampaignClient.SEAD_TARGET_TYPES`, the attribute
  names in a SEAD element's `EngageTargets` task, and `SPEC.sead_target_types`
  is cross-checked against it the same way (`drift.sead_target_types`);
- `tests/test_validation.py` asserts the same equality offline, so drift
  turns the test suite red rather than quietly invalidating a DCS run;
- the country map, the group task strings, the waypoint action table, the
  ramp-start pair and the `alt_type` constants are **locals** in
  `campaign_client.lua`. They are not
  reachable from another file at all, so `SPEC` mirrors them, and nothing can
  currently detect drift in them.

**The refactor that would remove the mirror entirely** (deliberately not
made — another agent owns that file):

1. In `campaign_client.lua`, move `DEFAULT_PAYLOAD`, `TEMPLATES`, the
   `m.country` and `m.category` name maps (as *names*, e.g. `blue = "USA"`,
   resolved to `country.id[...]` at use), and `WAYPOINT_ACTIONS` into one
   table declared near the top:

   ```lua
   local CONTENT = _G.CAMPAIGN_CONTENT or { templates = {...}, country = {...},
                                            category = {...},
                                            waypoint_actions = {...},
                                            group_task_default = "Nothing" }
   _G.CAMPAIGN_CONTENT = CONTENT
   ```

2. Have `dcs_maps()` and `TEMPLATES` read from `CONTENT` instead of their own
   literals, and export it as `M.CONTENT` alongside `M.TEMPLATES`.
3. Split it into `mission/campaign_content.lua`, loaded by a `DO SCRIPT FILE`
   before both `campaign_client.lua` and `validate_templates.lua`, exactly as
   `json.lua` already is. Both files then read one declaration and the mirror
   disappears.

Until then, `SPEC` in `validate_templates.lua` is the mirror, the cross-check
is the guard rail on the half that is exported, and the other half — country,
category, waypoint actions — is guarded only by the fact that both files are
reviewed together.

---

## 7. Discipline

Same rules as the client, for the same reasons:

- **Lua 5.1 only.** No `goto`, no bitwise operators, no 5.3 library calls.
- **Nothing blocks the sim thread.** One case is started per tick and
  finished on the next, on `timer.scheduleFunction`. Every DCS call is inside
  `pcall`; a case that raises is recorded as `ERROR` and the run carries on,
  because the whole point is one run instead of one crash at a time.
- **Deterministic.** No wall clock, no randomness. Case order is fixed, spawn
  positions are `origin + index * spacing`, and two runs of the same mission
  produce the same lines in the same order.
- **It cleans up after itself**, including after a run in which everything
  failed.
