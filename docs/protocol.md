# Campaign wire protocol v1

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

## Frame envelope

Every frame:

| field  | type   | notes                                            |
|--------|--------|--------------------------------------------------|
| `type` | string | message type, see below                          |
| `seq`  | int    | monotonic per direction, starts at 1             |
| `t`    | number | mission time in seconds (DCS `timer.getTime()`)  |

## Uplink: mission client to engine

### `hello`
First frame after connect. The engine replies `sync` or closes the connection
on a protocol mismatch.

```json
{"type":"hello","seq":1,"t":0.0,"protocol":1,"theater":"Syria","dcs_version":"2.9.29","mission_start_epoch":1758499200}
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

Unit references in events are DCS unit or group names. Names of entities the
engine owns always carry the `cmp_` prefix; anything else is scenery, a client
aircraft, or another script's, and the engine ignores it.

### `state`
Periodic ground truth for every entity the engine owns that is currently
instantiated. Sent every `STATE_PERIOD` seconds (default 30) and always
immediately before a `despawn` is acknowledged.

```json
{"type":"state","seq":41,"t":830.0,"groups":[{"spawn_id":"a91f","alive":true,"units":2,"units_initial":4,"pos":[41000.0,3000.0,-88000.0]}]}
```

### `ack`
Response to a downlink frame that carried a `ref`.

```json
{"type":"ack","seq":42,"t":830.2,"ref":91,"ok":true}
{"type":"ack","seq":43,"t":830.2,"ref":92,"ok":false,"error":"unknown template: Su-34_strike"}
```

## Downlink: engine to mission client

### `sync`
Sent in response to `hello`. Tells the client the campaign clock and hands it
its operating parameters.

```json
{"type":"sync","seq":1,"t":0.0,"protocol":1,"campaign_time":417600,"state_period":30,"observer_period":5,"bubble_radius":75000}
```

### `spawn`
Instantiate an entity. `spawn_id` is engine-owned and stable for the life of
the entity across DCS restarts. The client maps it to the DCS group name
`cmp_<spawn_id>` and must reject a duplicate `spawn_id` with a failed `ack`.

```json
{"type":"spawn","seq":91,"t":600.0,"ref":91,"spawn_id":"a91f","coalition":"blue","category":"plane",
 "template":"F-16C_strike_jdam","position":[40000.0,4500.0,-90000.0],"heading":1.57,
 "route":[{"pos":[40000.0,4500.0,-90000.0],"alt":4500,"speed":220,"action":"turning_point"}],
 "tasking":{"kind":"strike","target":"cmp_7c02","tot":1230.0}}
```

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
may only enrich a loss, never create or withhold one.

## Reconnect

On reconnect the client sends `hello` again. The engine responds with `sync`
and then re-issues `spawn` for everything that should be live in the current
bubble. The client has no persistent state of its own: DCS restarted, so
nothing it previously spawned still exists. The engine owning all identity is
what makes a campaign survivable across sim restarts.

## Versioning

`protocol` is an integer, bumped on any breaking change. The engine closes the
connection on mismatch rather than attempting negotiation.
