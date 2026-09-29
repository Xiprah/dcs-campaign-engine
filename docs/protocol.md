# Campaign wire protocol v2

The contract between the **engine** (out-of-process campaign brain) and the
**mission client** (thin Lua layer running inside DCS).

## Transport

Newline-delimited JSON over TCP on `127.0.0.1:7777`.

The **engine listens**, the **mission client connects out**. This direction is
deliberate: DCS may restart many times during one campaign, and LuaSocket
client sockets are far less trouble inside the mission environment than
listening sockets.

Every frame is one JSON object followed by `\n`. No frame may exceed 64 KiB.

### The non-negotiable rule

The mission client runs on the DCS simulation thread. It must **never** block.
Sockets are set to `settimeout(0)`, drained in a `timer.scheduleFunction` tick,
and the client processes at most `MAX_FRAMES_PER_TICK` inbound frames before
yielding back to the sim. A backlog is always preferable to a frame hitch.

## Connections

Every TCP connection is a new frame stream. Nothing carries over from the
previous one.

- **`seq` restarts at 1 on every connection, in both directions.** The
  client's `hello` is always its `seq` 1; the engine's `sync` is always its
  `seq` 1.
- **`hello` is the first frame a client sends**, on every connection,
  including every reconnect.
- **The engine writes nothing on a connection until it has processed that
  connection's `hello`.** `sync` is therefore always the first frame a client
  receives. There is no frame before it.
- **Anything the campaign produces before that `hello` is dropped, not
  queued** — a spawn, a despawn or a message from a tick that lands between
  the TCP accept and the `hello`. Nothing is lost by this: the reply to
  `hello` re-issues every live spawn and re-briefs every open package, so the
  client ends up with exactly the current picture. Queuing those frames
  instead would deliver them numbered from the old stream, and a spawn
  delivered early and then re-issued by the `hello` is a duplicate `spawn_id`
  the client is obliged to refuse.
- A `ref` names a `seq` on the current connection. A downlink frame still
  unacknowledged when its connection drops is never acknowledged, and the
  engine forgets it.

## Frame envelope

Every frame:

| field  | type   | notes                                                        |
|--------|--------|--------------------------------------------------------------|
| `type` | string | message type, see below                                      |
| `seq`  | int    | monotonic per direction, starts at 1 on every connection     |
| `t`    | number | mission time in seconds (DCS `timer.getTime()`)              |

## Uplink: mission client to engine

### `hello`
First frame on every connection. The engine replies `sync`, or closes the
connection without writing anything on a protocol mismatch.

```json
{"type":"hello","seq":1,"t":0.0,"protocol":2,"theater":"Syria","dcs_version":"2.9.29","mission_start_epoch":1758499200}
```

### `observer`
Where the humans are. This is the sole input to the bubble. Sent on a slow
cadence (default 5 s) and immediately on player birth or death.

```json
{"type":"observer","seq":12,"t":305.0,"observers":[{"id":"player:Jakem","pos":[41230.0,2100.0,-88400.0],"speed":220.0}]}
```

### `event`
Something happened. Events are **hints**, never the source of truth — see
Reconciliation. `kind` is the DCS event stripped of its `S_EVENT_` prefix and
lowercased: `birth`, `takeoff`, `land`, `shot`, `hit`, `kill`, `dead`,
`crash`, `eject`, `pilot_dead`, `base_captured`.

```json
{"type":"event","seq":40,"t":812.5,"kind":"kill","initiator":"cmp_a91f","target":"cmp_7c02","weapon":"AGM-88C"}
```

The name fields — `initiator`, `target`, `weapon`, `place` — are **strings or
null**. Unit references are DCS unit or group names. Names of entities the
engine owns always carry the `cmp_` prefix; anything else is scenery, a client
aircraft, or another script's, and the engine ignores it.

**A name field that is not a string is ignored**, exactly as if it were null.
The frame is kept; only that field's attribution is lost. The engine never
closes a connection over an event's name field: an event is attribution only,
so dropping one attribution is harmless by design, and a live sortie is not
worth losing over it. (DCS scenery answers `getName` with a numeric object id.
The Lua client sends null for it rather than the number, but the engine does
not rely on that.)

### `state`
Periodic ground truth for every entity the engine owns that is currently
instantiated. Sent every `STATE_PERIOD` seconds (default 30) and always
immediately before a `despawn` is acknowledged.

```json
{"type":"state","seq":41,"t":830.0,"groups":[{"spawn_id":"a91f","alive":true,"units":2,"units_initial":4,"pos":[41000.0,3000.0,-88000.0]}]}
```

A `state` frame is a complete census: a group the engine instantiated that is
missing from it is read as gone.

**A malformed snapshot is a protocol error and closes the connection** — a
`groups` that is not an array, a member that is not an object or lacks a
field, or a `spawn_id` that is not a string. None of the event leniency
applies here. A snapshot is ground truth, and a group reported under an id
that matches nothing reads as absent from the census, which is a loss: a
guessed snapshot writes off a flight that is still flying.

### `ack`
Response to a downlink frame that carried a `ref`.

```json
{"type":"ack","seq":42,"t":830.2,"ref":91,"ok":true}
{"type":"ack","seq":43,"t":830.2,"ref":92,"ok":false,"error":"unknown template: Su-34_strike"}
```

## Downlink: engine to mission client

### `sync`
Always the first frame on a connection, in response to `hello`. Tells the
client the campaign clock and hands it its operating parameters.

```json
{"type":"sync","seq":1,"t":0.0,"protocol":2,"campaign_time":417600,"state_period":30,"observer_period":5,"bubble_radius":75000}
```

### `spawn`
Instantiate an entity. `spawn_id` is engine-owned and stable for the life of
the entity across DCS restarts. The client maps it to the DCS group name
`cmp_<spawn_id>` and must reject a duplicate `spawn_id` with a failed `ack`.

```json
{"type":"spawn","seq":91,"t":600.0,"ref":91,"spawn_id":"a91f","coalition":"blue","category":"plane",
 "template":"F-16C_strike_jdam","units":2,"position":[40000.0,4500.0,-90000.0],"heading":1.57,
 "route":[{"pos":[40000.0,4500.0,-90000.0],"alt":4500,"speed":220,"action":"turning_point","airdrome_id":null}],
 "tasking":{"kind":"strike","target":"cmp_7c02","tot":1230.0,"callsign":"VIPER"}}
```

| field        | type   | notes |
|--------------|--------|-------|
| `spawn_id`   | string | engine-owned identity; see above |
| `coalition`  | string | `blue`, `red` or `neutral` |
| `category`   | string | `plane`, `helicopter`, `ground`, `ship`, or `structure` for a static target |
| `template`   | string | a name in the client's template table |
| `units`      | int    | **required.** Exactly how many units to build. See below. |
| `position`   | vec3   | DCS world `[x, altitude, z]` |
| `heading`    | number | radians; default 0 |
| `route`      | array  | waypoints, see below; default `[]` |
| `tasking`    | object | what the entity is for; default `{}` |

**`units` is the count, not a hint.** The engine owns the airframe ledger, so
it decides how many units exist: a two-ship that has lost a wingman is
re-issued with `units` 1, and a target already half-destroyed comes back with
the half that survived. The template's own size is only a ceiling. The client
builds exactly `units` units, and refuses with a failed `ack` — never builds a
different number — when it cannot:

- `units` missing, or not a positive integer:
  `"bad units: 0 (want a positive integer)"`
- `units` larger than the template holds:
  `"units 3 exceeds template F-16C_strike_jdam capacity of 2"`

A silent clamp is how the two sides' accounting diverges: the client builds
fewer units than the engine issued, and the next census books the difference
as a loss nobody caused. A refused spawn is visible instead: the engine
scrubs the package, or stops offering the target.

Each `route` waypoint:

| field         | type        | notes |
|---------------|-------------|-------|
| `pos`         | vec3        | DCS world `[x, altitude, z]` |
| `alt`         | number      | metres; 0 means use `pos`'s altitude |
| `speed`       | number      | m/s; 0 means the client's default |
| `action`      | string      | `turning_point`, `fly_over_point`, `attack`, `landing` |
| `airdrome_id` | int or null | optional, default null. A DCS airdrome id. |

**`airdrome_id`** puts DCS's `airdromeId` on that waypoint. On the **first**
waypoint it makes the flight a ramp start: the client emits that waypoint as
type `TakeOffParking`, action `From Parking Area`, and the flight begins cold
on that airfield's parking instead of in the air. On any later waypoint it
only names the airfield, as for a landing. A value that is neither null nor an
integer is a failed `ack` (`"bad spawn payload: malformed airdrome_id: ..."`),
never quietly an air start. The engine does not set this field yet — airdrome
ids are per-map content nobody has validated — so today every flight is an
air start.

`tasking` for a strike carries `kind`, `target` (a DCS group name), `tot`
(mission time) and `callsign`. A static target's is `{"kind":"static"}`. An
air-defence site — a `ground` group, such as the `SA-6_Kub_site` template —
is `{"kind":"air_defence"}`: it has no route and no target, and the sim's own
AI decides what it shoots at. A `kind` the client does not recognise carries
no task, so adding one is not a protocol change.

A package's elements (docs/design.md, section 5) arrive as separate spawns,
each with its own `spawn_id`, and the client never reasons about two at
once. A SEAD element's tasking is

```json
{"kind":"sead","targets":["cmp_0004"],"tot":1351.0,"callsign":"VIPER SEAD"}
```

`targets` names, by DCS group name, the enemy air-defence sites the
package's route enters, as they stand when the element is spawned; any of
them may not be instantiated. `tot` is the element's own time over the
target, ahead of the strike's. The client gives the group an
`EngageTargets` task against air defences on its first waypoint and, for
each named site that exists, an `AttackGroup` on the attack waypoint.

A flight whose part in its package's time on target has already been
resolved — one re-entering the bubble on its way home — is sent
`{"kind":"egress","callsign":"VIPER"}`. The client does not recognise the
kind and attaches no task: the flight flies its route home. A strike
tasking there would have the client hang the attack on the landing waypoint
and strike the target a second time.

### `despawn`
Remove an entity. The engine is responsible for deciding this; the client just
obeys. A `despawn` for an unknown `spawn_id` is a successful no-op.

```json
{"type":"despawn","seq":92,"t":1800.0,"ref":92,"spawn_id":"a91f","reason":"left_bubble"}
```

### `message`
Pilot-facing text.

```json
{"type":"message","seq":93,"t":1801.0,"to":"blue","text":"Package COWBOY off target, RTB.","duration":20}
```

## Reconciliation: why `state` exists

DCS events are lossy and inconsistent. Kills go unreported, `dead` fires
without a matching `kill`, and units disappear with no event at all. An
attrition model built on event counting will silently drift and then produce a
campaign that is quietly wrong — which is worse than one that visibly breaks.

So the engine treats the two streams differently:

- **`state` is truth.** Entity liveness and unit counts come only from
  snapshots. If a snapshot says a group is gone, it is gone, regardless of what
  events did or did not arrive.
- **`event` is attribution.** Events answer *who* killed a thing and *with
  what*, which snapshots cannot. Attribution is best-effort and may be unknown.

The engine must be correct when every single `event` frame is dropped. Events
may only enrich a loss, never create or withhold one. That is also why the two
frames are decoded differently: a junk field costs an event one attribution,
and costs a snapshot the connection.

## Reconnect

On reconnect the client sends `hello` again, as `seq` 1 of a new stream (see
Connections). The engine responds with `sync` and then re-issues `spawn` for
everything that should be live in the current bubble, each at the unit count
attrition has already recorded. The client has no persistent state of its own:
DCS restarted, so nothing it previously spawned still exists. The engine
owning all identity is what makes a campaign survivable across sim restarts.

## Versioning

`protocol` is an integer, bumped on any breaking change; this is version 2.
There is no negotiation. The engine closes the connection on a `hello` that
carries any other version, without writing a frame. The client tears the
connection down on a `sync` that carries any other version, and backs off to
its longest reconnect delay rather than retry against an engine it cannot
talk to.

## Changes from v1

Three breaking changes, bundled into one bump so a deployment migrates once:

1. **`spawn.units` is new and required.** v1 had no unit count on the wire:
   the client took the template's size, and the engine smuggled the surviving
   count through a private `tasking.units` extension. A client that ignored
   `tasking` rebuilt every attrited flight whole, and the campaign silently
   regained airframes it had written off. `tasking.units` is gone; `units` is
   the only source of truth, and a count the template cannot hold is refused
   rather than clamped.
2. **`route[].airdrome_id` is new and optional.** On the first waypoint it
   makes a ramp start. Absent or null means what v1 always meant: an air
   start.
3. **Name fields are typed.** An event name field that is not a string is now
   ignored rather than undefined; a snapshot with a non-string `spawn_id`, a
   non-object member or a non-array `groups` is now explicitly a protocol
   error.

Also written down for the first time, though it is not a wire change: `seq`
is per connection and restarts at 1 in both directions, `sync` is always the
first frame a client receives, and the engine drops rather than queues
anything produced before a connection's `hello`. v1 never said so, and a bug
lived in that gap.
