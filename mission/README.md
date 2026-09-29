# Mission client

The DCS half of the campaign engine. Two files:

| file                 | what it is                                                     |
|----------------------|----------------------------------------------------------------|
| `json.lua`           | a small JSON codec written for this project                    |
| `campaign_client.lua`| the client: socket, tick loop, spawning, event and state frames |

The client connects **out** to the engine on `127.0.0.1:7777`, does what the
engine tells it, and reports back. It holds no campaign state: the engine owns
identity, and after any restart the engine re-issues everything that should be
live. See `docs/protocol.md` for the wire contract.

---

## 1. Desanitise `MissionScripting.lua`

**This step is required, and it is the one with real consequences. Read
section 4 before you do it.**

The DCS mission scripting environment is sandboxed. `socket`, `require`,
`package`, `io`, `lfs` and `os` are stripped out before any mission script
runs, so without this change the client cannot open a socket at all.

Open, in your DCS **installation** directory (not Saved Games):

```
<DCS install>/Scripts/MissionScripting.lua
```

Near the top is a `do ... end` block that removes the dangerous modules. It
contains lines that sanitise `os`, `io` and `lfs`, and lines that set
`require`, `loadlib` and `package` to `nil`.

Comment out the lines for the pieces the client needs:

- `require` and `package` — **required**, this is how `socket` is loaded
- `lfs` — **required** too, despite appearances. A bare `require("socket")`
  fails, because LuaSocket is not on the mission environment's `package.path`,
  and `lfs.currentdir()` is how the client names the directory DCS ships it in.
  There is a fallback that guesses the same directory relative to the working
  one, but it only works when DCS was started from its own install directory.
  Without `lfs` the likely outcome is `LuaSocket is unavailable` on every
  attempt, forever.
- `io` and `os` — **not needed**. Leave these sanitised. The client does not
  read files and does not read a clock: `os.time()` would make the campaign's
  `mission_start_epoch` depend on the host's timezone, so it computes the date
  arithmetically instead.

Comment out a line by putting `--` in front of it. Do not delete the block;
you will want to put it back.

Then verify: start a mission with the client loaded and look in
`Saved Games/DCS/Logs/dcs.log` for

```
[campaign] client started, engine at 127.0.0.1:7777
```

If instead you see

```
[campaign] LuaSocket is unavailable; see mission/README.md ...
```

the desanitisation did not take. The usual causes are editing the copy in
Saved Games instead of the install, or a DCS update having restored the file —
**an update will silently revert it**, so re-check after every patch.

---

## 2. Install into a `.miz`

A `.miz` is a zip archive. There are two ways in; the first is easier to
iterate on, the second is what you ship.

### Option A — load from disk (development)

Keep the two `.lua` files wherever you like and load them from a trigger.

In the Mission Editor, add **one** trigger:

- **Type:** `4 MISSION START`
- **Condition:** *none*
- **Actions**, in this order:
  1. `DO SCRIPT` → `dofile("C:\\dcs-campaign-engine\\mission\\json.lua")`
  2. `DO SCRIPT` → `dofile("C:\\dcs-campaign-engine\\mission\\campaign_client.lua")`

Note the doubled backslashes. This needs nothing desanitised beyond what
Option B needs. `dofile` and `loadfile` are Lua base-library functions that go
through C stdio, and the sanitise block does not remove them: it nils the
`os`, `io` and `lfs` tables and `require`, `loadlib` and `package`, none of
which `dofile` goes through. Both options need exactly `require`, `package`
and `lfs`, and they need them for LuaSocket rather than for loading the file.
The difference between the two is only where the `.lua` lives.

### Option B — embed in the mission (shipping)

- **Type:** `4 MISSION START`
- **Condition:** *none*
- **Actions**, in this order:
  1. `DO SCRIPT FILE` → `json.lua`
  2. `DO SCRIPT FILE` → `campaign_client.lua`

`DO SCRIPT FILE` copies the file into the `.miz` at save time. Re-add the file
after every edit, or the mission keeps running the stale copy embedded earlier.

### Leave the condition empty

Both recipes above say **no condition**, and that is deliberate. A
`4 MISSION START` rule is evaluated once, at mission start, when the model
clock is still zero — so adding `TIME MORE (1)` makes it false at the only
instant it is ever tested, and the `DO SCRIPT` actions never run at all. The
symptom is not an error: it is nothing whatsoever in `dcs.log`.

If you would rather use a time condition, the other consistent pairing is
**Type** `1 ONCE` with **Condition** `TIME MORE (1)`. Pick one or the other;
combining them is the one arrangement that cannot work.

### Load order is not optional

`json.lua` must load first. It publishes itself as the global
`CAMPAIGN_JSON`, because `DO SCRIPT FILE` discards a chunk's return value and
`require` cannot see inside a `.miz`. Loading `campaign_client.lua` first fails
immediately with:

```
campaign_client: load mission/json.lua before this file (it sets the global CAMPAIGN_JSON)
```

The client starts itself as soon as it loads. There is nothing else to call.

---

## 3. Configuration

Defaults are in `CONFIG` at the top of `campaign_client.lua`. To override,
define `CAMPAIGN_CLIENT_CONFIG` in a `DO SCRIPT` action **before** loading the
client:

```lua
CAMPAIGN_CLIENT_CONFIG = {
    host = "127.0.0.1",
    port = 7777,
    tick = 0.1,                 -- seconds between client ticks
    max_frames_per_tick = 16,   -- inbound frames per tick; a backlog beats a hitch
    reconnect_max = 30.0,       -- cap on the reconnect backoff
}
```

`host` must be an IPv4 address, never a name. `settimeout(0)` bounds the TCP
handshake but not name resolution: LuaSocket hands the string to `getaddrinfo`
synchronously, and a name Windows cannot resolve costs DNS, then LLMNR, then
NetBIOS — one to three seconds of frozen sim, on every reconnect attempt, for
as long as the engine is unreachable. The client refuses a non-address `host`
rather than blocking on it, and says so once in `dcs.log`.

`state_period`, `observer_period` and `bubble_radius` are **not** configurable
here. The engine sends them in `sync` and the client obeys; that is the whole
point of `sync`.

Runtime state, from a `DO SCRIPT` action or a debug hook:

```lua
local s = CampaignClient.status()
-- {running, phase, synced, seq, live_spawns, queued_frames, outbox_bytes}
CampaignClient.stop()
CampaignClient.start()
```

---

## 4. Security warning

**Desanitising `MissionScripting.lua` removes the sandbox for every mission
that DCS install ever runs afterwards — not just this one.**

That sandbox is the only thing standing between a downloaded `.miz` and your
computer. With `require` and `package` restored, any mission you open can load
arbitrary Lua modules and native DLLs. With `lfs` or `io` also restored, it can
read, write and delete files anywhere your Windows account can reach, and reach
the network. A mission is a zip file full of executable Lua, and you generally
have no idea who wrote the one you just downloaded from a server.

The realistic consequences: a multiplayer server you join sends you its mission
file, and it runs with your privileges. Credentials in your user profile,
documents, SSH keys, your DCS install itself — all in scope.

So:

- Desanitise on a machine where you accept that risk, ideally one dedicated to
  this campaign, and not on one that holds anything you care about.
- Comment out only what you need. `require`, `package` and `lfs` are all three
  required — `lfs` is how the client reaches LuaSocket at all, so it is not the
  optional one it looks like. **Leave `io` and `os` sanitised**: this client
  does not use them, and they are the two that turn a hostile `.miz` into
  arbitrary file access.
- Re-comment the block before you play someone else's mission, or keep a
  sanitised install alongside and only run the campaign on the other.
- Check the file after every DCS update. Updates restore it, and they do it
  without telling you.
- Never hand this install a `.miz` you did not build or read.

Binding to `127.0.0.1` means the socket itself is not exposed to the network.
That is a property of the client's config, not of the sandbox you just removed:
the sandbox is gone for everything, and it stays gone.

---

## 5. What the client actually does

- **Ticks** every `CONFIG.tick` seconds on `timer.scheduleFunction`. One tick
  does everything: poll the socket, process at most `MAX_FRAMES_PER_TICK`
  inbound frames, run due reports, flush the outbox. Every socket call is
  `settimeout(0)` and every callback body is inside `pcall`, so an engine that
  is down or wedged costs the sim nothing and never stops the mission.
- **Reconnects** with exponential backoff, capped at `reconnect_max`, and sends
  `hello` on every connection. The engine answers with `sync` and re-issues
  spawns.
- **Maps** `spawn_id` to the DCS group name `cmp_<spawn_id>`. A duplicate
  `spawn_id` is rejected with a failed `ack`; a `despawn` for an unknown one is
  a successful no-op.
- **Builds exactly `units` units** — the engine's count, never the
  template's. The template's `count` is a ceiling: a spawn asking for more is
  refused with a failed `ack`, not clamped, because a clamp builds fewer units
  than the engine issued and the next census books the difference as a loss.
  `tools/fake_dcs.py` refuses the same spawns with the same words.
- **Ramp-starts** a flight whose first waypoint carries an `airdrome_id`
  (`TakeOffParking` / `From Parking Area` at that airfield); every other
  flight is an air start.
- **Reports state** on the engine's `state_period` for every spawn it is
  tracking, including ones whose DCS group has vanished — those report
  `alive=false`. This is the only thing that records a loss, so nothing is ever
  omitted from it, and a `despawn` sends a state report *before* destroying the
  group. Destroying first would turn a flight recalled from the bubble into a
  combat loss.
- **Reports events** as attribution only, and filters them hard: an event is
  sent only when an engine-owned entity is the initiator or target. Dropping
  events is safe by design — the campaign must reach identical state with every
  event frame discarded.
- **Reports observers** on the engine's `observer_period`, and immediately when
  a player is born or dies.

### On disconnect, the client destroys what it spawned

When the connection drops, everything the engine asked for is unmanaged: the
engine cannot see it, and on reconnect it re-issues `spawn` for all of it. Since
a duplicate `spawn_id` must fail, keeping the old groups would deadlock the
reconnect. So they are destroyed.

In practice this means an engine restart mid-flight makes the AI vanish and
reappear. That is visible, which is the point — it beats a campaign that quietly
disagrees with the sim about what is flying.

---

## 6. Known limits

Everything named here is *content* — strings this client hands to DCS that no
test off a DCS install can judge. `mission/validate_templates.lua` exists to
settle them in one run rather than one crash at a time: load it in any mission,
read the report with `tools/parse_validation.py`, and see which templates,
countries, task schemas and pylon CLSIDs DCS actually accepts. Start there
before debugging any of the below. See [VALIDATION.md](VALIDATION.md).

These are deliberate for the first vertical slice, not oversights:

- **Loadouts are empty.** `TEMPLATES` in `campaign_client.lua` carries no
  pylon CLSIDs, because they are DCS-version specific and an unknown one
  silently yields an empty pylon rather than an error. So the strike package
  spawns unarmed: it will fly the route and drop nothing. Copy real values from
  mission-editor exported group data before expecting anything to hit a target.
  Munitions accounting lives in the engine either way.
- **Air starts in practice.** The client ramp-starts a flight whose first
  waypoint carries an `airdrome_id`, but the engine never sends one: airdrome
  ids are per-map content nobody has validated yet. The `waypoint.ramp_start`
  case in `validate_templates.lua` is how to settle whether DCS accepts the
  pair before the engine starts using it.
- **Templates are literals.** Real fidelity means deep-copying a
  late-activation group out of `env.mission`. The seam is marked in the file.
- **One `state` frame.** Hundreds of live entities would exceed the protocol's
  64 KiB frame cap and need chunking.
- **No ground war, front line, logistics, pilot records, or package
  composition** beyond a single strike flight. Each seam is marked with a
  `TODO(seam)` comment where it belongs.

---

## 7. Troubleshooting

| symptom | cause |
|---|---|
| `LuaSocket is unavailable` in `dcs.log` | `MissionScripting.lua` not desanitised, or a DCS update reverted it — check `lfs` specifically, since desanitising only `require` and `package` produces exactly this and nothing else |
| `engine not reachable ... retrying` | the engine is not listening on 7777; the client keeps retrying, the mission is unaffected |
| `load mission/json.lua before this file` | trigger actions are in the wrong order |
| `unknown template: X` in an `ack` | the engine asked for a template that is not in `TEMPLATES` |
| `units N exceeds template X capacity of C` in an `ack` | the engine issued a bigger group than `TEMPLATES[X].count` holds; raise `count` or fix the engine's flight size — the client will not clamp |
| `bad units: ...` in an `ack` | the spawn carried no unit count, or not a positive integer; protocol 2 requires one, so this is an engine bug |
| `engine speaks protocol N` | version mismatch; the engine closes the connection and the client backs off to `reconnect_max` |
| `CONFIG.host must be an IPv4 address` | `host` was overridden with a name; resolving one would block the sim thread, so the client refuses to dial it — use the address |
| nothing at all in `dcs.log` | the trigger never fired — a `MISSION START` rule must carry **no condition**; `TIME MORE (1)` is false at mission start and the rule is never evaluated again |
