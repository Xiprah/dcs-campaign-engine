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
- `lfs` — recommended, it lets the client find the LuaSocket DLL DCS ships
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
- **Condition:** `TIME MORE (1)`
- **Actions**, in this order:
  1. `DO SCRIPT` → `dofile("C:\\dcs-campaign-engine\\mission\\json.lua")`
  2. `DO SCRIPT` → `dofile("C:\\dcs-campaign-engine\\mission\\campaign_client.lua")`

Note the doubled backslashes, and note that `dofile` needs `io`/`lfs`
desanitised — if you took the advice above and left `io` sanitised, use
Option B instead, which does not need it.

### Option B — embed in the mission (shipping)

- **Type:** `4 MISSION START`
- **Condition:** `TIME MORE (1)`
- **Actions**, in this order:
  1. `DO SCRIPT FILE` → `json.lua`
  2. `DO SCRIPT FILE` → `campaign_client.lua`

`DO SCRIPT FILE` copies the file into the `.miz` at save time. Re-add the file
after every edit, or the mission keeps running the stale copy embedded earlier.

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
- Comment out only what you need. `require` and `package` are required;
  `lfs` is convenient; **leave `io` and `os` sanitised** — this client does not
  use them.
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

These are deliberate for the first vertical slice, not oversights:

- **Loadouts are empty.** `TEMPLATES` in `campaign_client.lua` carries no
  pylon CLSIDs, because they are DCS-version specific and an unknown one
  silently yields an empty pylon rather than an error. Copy real values from
  mission-editor exported group data before expecting anything to hit a target.
  Munitions accounting lives in the engine either way.
- **Air starts only.** A ground start needs an `airdromeId` on the waypoint and
  the protocol's waypoint has no field for one.
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
| `LuaSocket is unavailable` in `dcs.log` | `MissionScripting.lua` not desanitised, or a DCS update reverted it |
| `engine not reachable ... retrying` | the engine is not listening on 7777; the client keeps retrying, the mission is unaffected |
| `load mission/json.lua before this file` | trigger actions are in the wrong order |
| `unknown template: X` in an `ack` | the engine asked for a template that is not in `TEMPLATES` |
| `engine speaks protocol N` | version mismatch; the engine closes the connection and the client backs off to `reconnect_max` |
| nothing at all in `dcs.log` | the trigger never fired — check it is `MISSION START` with `TIME MORE (1)` |
