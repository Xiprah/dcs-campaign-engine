--[[----------------------------------------------------------------------
  mission/campaign_client.lua -- the DCS half of the campaign engine.

  Runs inside the DCS mission scripting environment (Lua 5.1 / LuaJIT).
  Connects out to the campaign engine on 127.0.0.1:7777, instantiates what
  the engine tells it to, and streams ground truth back. It holds no
  campaign state of its own and makes no campaign decisions: the engine
  owns identity, the client owns nothing but sockets and DCS handles.

  See docs/protocol.md for the wire contract this implements.

  ------------------------------------------------------------------------
  THE RULE THIS FILE IS SHAPED AROUND

  Every line below runs on the DCS simulation thread. A millisecond spent
  here is a millisecond DCS is not rendering, and an uncaught error is the
  rest of the mission running without a campaign.

  So, without exception:
    * every socket call is non-blocking -- settimeout(0), always;
    * every callback body sits inside pcall, logs, and carries on;
    * at most MAX_FRAMES_PER_TICK inbound frames are processed per tick.
      A backlog is always better than a frame hitch;
    * nothing iterates the whole world. State reports walk our own spawn
      registry, which is a handful of entries, not every unit in DCS.

  ------------------------------------------------------------------------
  RECONCILIATION

  The client's part in the engine's reconciliation model is narrow and
  absolute: `state` frames are ground truth, `event` frames are attribution
  only. That shapes two things here.

  1. A state report covers EVERY spawn in the registry, including ones
     whose DCS group no longer exists -- those report alive=false. A group
     that quietly vanished with no event is exactly the case the snapshot
     exists to catch, so it must never be omitted.

  2. A despawn sends a state report BEFORE Group.destroy, never after.
     After the destroy the group reads as dead, and the engine would book
     a flight that merely left the bubble as a combat loss.

  Events are best-effort throughout, and the client drops them freely --
  see the filter in on_event_body. Dropping every event must leave the
  campaign correct, and nothing below assumes otherwise.
------------------------------------------------------------------------]]

local json = _G.CAMPAIGN_JSON
if not json then
    local ok, mod = pcall(require, "json")
    if ok and type(mod) == "table" then json = mod end
end
assert(json, "campaign_client: load mission/json.lua before this file "
          .. "(it sets the global CAMPAIGN_JSON)")

-- ------------------------------------------------------------------
-- Configuration
-- ------------------------------------------------------------------

-- Define CAMPAIGN_CLIENT_CONFIG before loading this file to override.
local CONFIG = {
    host = "127.0.0.1",
    port = 7777,

    --- Tick period in seconds. 10 Hz is responsive enough for a campaign
    --- and cheap enough to disappear into the noise of a DCS frame.
    tick = 0.1,

    --- Inbound frames handled per tick. The cap the protocol names.
    max_frames_per_tick = 16,

    --- Bytes pulled off the socket per tick, so a burst cannot turn one
    --- tick into a stall.
    recv_budget = 64 * 1024,
    recv_chunk = 8 * 1024,

    --- Unsent bytes tolerated before declaring the engine wedged.
    outbox_max = 512 * 1024,

    reconnect_base = 2.0,
    reconnect_max = 30.0,

    --- How long a non-blocking connect may stay pending.
    connect_timeout = 5.0,

    --- How long after `hello` the engine has to answer with `sync`. The
    --- protocol says the engine replies sync or closes the connection on a
    --- mismatch, but a connection that is open and silent is a third outcome
    --- the client has to survive on its own: without a deadline it sits in
    --- phase "open" forever, sending nothing and never retrying.
    sync_timeout = 15.0,

    --- Seconds between repeats of a "cannot reach the engine" log line, so
    --- a dead engine does not fill dcs.log.
    log_throttle = 60.0,
}

if type(_G.CAMPAIGN_CLIENT_CONFIG) == "table" then
    for k, v in pairs(_G.CAMPAIGN_CLIENT_CONFIG) do CONFIG[k] = v end
end

local PROTOCOL_VERSION = 2
local MAX_FRAME_BYTES = 64 * 1024
local MAX_FRAMES_PER_TICK = CONFIG.max_frames_per_tick
local OWNED_PREFIX = "cmp_"

-- ------------------------------------------------------------------
-- Logging
-- ------------------------------------------------------------------

local function log(level, msg)
    if env and env[level] then
        env[level]("[campaign] " .. tostring(msg))
    end
end

local function log_info(msg) log("info", msg) end
local function log_warn(msg) log("warning", msg) end
local function log_err(msg) log("error", msg) end

--- Run `fn` so a failure is logged instead of escaping into the sim.
local function guard(what, fn, ...)
    local ok, err = pcall(fn, ...)
    if not ok then
        log_err(what .. ": " .. tostring(err))
        return false
    end
    return true
end

--- pcall a DCS accessor and return nil rather than propagating. Object
--- handles go stale between an event firing and us reading it, and a stale
--- handle raises instead of returning nil.
local function try(fn, ...)
    local ok, v = pcall(fn, ...)
    if ok then return v end
    return nil
end

-- ------------------------------------------------------------------
-- Client state
-- ------------------------------------------------------------------

local S = {
    running = false,
    sched_id = nil,

    sock = nil,
    phase = "idle",              -- idle | connecting | open
    connect_deadline = 0,
    sync_deadline = nil,
    next_connect_at = 0,
    backoff = CONFIG.reconnect_base,
    last_fail_log = -1e18,

    inbuf = "",

    -- Frame queue as explicit head/tail indices. A table with its first
    -- entries nil'd out has an undefined '#', so the length is never
    -- inferred.
    queue = {},
    queue_head = 1,
    queue_tail = 0,

    outq = {},                   -- fragments not yet handed to send()
    outcur = nil,                -- fragment currently being written
    outpos = 1,
    outbytes = 0,

    seq = 0,

    synced = false,
    state_period = nil,
    observer_period = nil,
    bubble_radius = nil,
    campaign_time = nil,
    next_state_at = nil,
    next_observer_at = nil,

    --- spawn_id -> {name, units_initial, category, coalition}
    spawns = {},

    --- unit name -> true, for units a human is sitting in.
    players = {},
}

local function now_t()
    return timer.getTime()
end

-- ------------------------------------------------------------------
-- Name helpers
-- ------------------------------------------------------------------

local function group_name(spawn_id)
    return OWNED_PREFIX .. spawn_id
end

local function is_owned(name)
    return type(name) == "string"
       and string.sub(name, 1, #OWNED_PREFIX) == OWNED_PREFIX
end

--- The name the engine should see for a DCS object.
---
--- Group name first, because that is what spawn_id maps to. Unit names
--- inside an engine-owned group are `cmp_<id>_<n>`, which the engine
--- cannot resolve back to a spawn_id; the group name it can. A static
--- object has no group, so the numbering is stripped here instead --
--- without it every event about a multi-object target would arrive under a
--- name the engine owns nothing by, and the loss it explains would be
--- recorded as unattributed.
local function object_name(obj)
    if not obj then return nil end
    if obj.getGroup then
        local grp = try(obj.getGroup, obj)
        if grp and try(grp.isExist, grp) then
            local n = try(grp.getName, grp)
            if n then return n end
        end
    end
    local name = try(obj.getName, obj)
    if type(name) ~= "string" then
        -- Scenery answers getName with its numeric object id, and the wire
        -- says these fields are strings. A number survives JSON intact and
        -- reaches the engine's attribution path, which tests it with a string
        -- prefix and raises: a traceback in the engine log per such event,
        -- and the attribution it carried lost anyway. Nameless is honest.
        return nil
    end
    local stem = string.match(name, "^(.*)_%d+$")
    if stem and is_owned(stem) then return stem end
    return name
end

--- Days from 1970-01-01 for a proleptic Gregorian date.
---
--- Hand-rolled rather than os.time(), which reads the host's timezone and
--- would make one mission produce a different mission_start_epoch on two
--- machines. A campaign has to replay identically.
local function days_from_civil(y, m, d)
    if m <= 2 then y = y - 1 end
    local era = math.floor(y / 400)
    local yoe = y - era * 400
    local mp = (m + 9) % 12
    local doy = math.floor((153 * mp + 2) / 5) + d - 1
    local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
    return era * 146097 + doe - 719468
end

-- ------------------------------------------------------------------
-- LuaSocket
-- ------------------------------------------------------------------

local socket = nil
--- The search paths below are appended once, not once per connect attempt:
--- this is retried on every backoff, and `package.path` would otherwise grow
--- for as long as the engine stays down.
local socket_path_extended = false

local function try_require_socket()
    local ok, mod = pcall(require, "socket")
    if ok and type(mod) == "table" then
        socket = mod
        return socket
    end
    return nil
end

local function load_socket()
    if socket then return socket end
    if try_require_socket() then return socket end
    if not package or socket_path_extended then return nil end
    socket_path_extended = true

    -- DCS ships LuaSocket beside the executable but does not put it on the
    -- mission environment's package.path. `lfs.currentdir()` is the reliable
    -- way to name that directory; the relative form after it is the fallback
    -- for an install where only `require` and `package` were desanitised,
    -- which is what the README used to tell people to do.
    local dir = lfs and try(lfs.currentdir)
    if dir then
        package.path = package.path .. ";" .. dir .. "/LuaSocket/?.lua"
        package.cpath = package.cpath .. ";" .. dir .. "/LuaSocket/?.dll"
    end
    package.path = package.path .. ";./LuaSocket/?.lua"
    package.cpath = package.cpath .. ";./LuaSocket/?.dll"
    return try_require_socket()
end

-- ------------------------------------------------------------------
-- Outbound frames
-- ------------------------------------------------------------------

local teardown  -- forward declaration; defined with the connection code

--- Stamp, encode and queue one uplink frame. Never blocks, never raises.
local function send_frame(frame)
    if S.phase ~= "open" then return false end

    S.seq = S.seq + 1
    frame.seq = S.seq
    frame.t = now_t()

    local text, err = json.encode(frame)
    if not text then
        -- Nothing left the client, so hand the sequence number back and
        -- keep the stream gapless.
        S.seq = S.seq - 1
        log_err("encode " .. tostring(frame.type) .. ": " .. tostring(err))
        return false
    end

    if #text + 1 > MAX_FRAME_BYTES then
        S.seq = S.seq - 1
        log_err(tostring(frame.type) .. " frame of " .. #text
                .. " bytes exceeds MAX_FRAME_BYTES; dropped")
        return false
    end

    if S.outbytes + #text + 1 > CONFIG.outbox_max then
        log_err("outbox full at " .. S.outbytes .. " bytes; engine is not reading")
        teardown("outbox overflow")
        return false
    end

    local payload = text .. "\n"
    S.outq[#S.outq + 1] = payload
    S.outbytes = S.outbytes + #payload
    return true
end

local function send_ack(ref, ok, err)
    if ref == nil or json.isnull(ref) then return end
    send_frame({type = "ack", ref = ref, ok = ok and true or false, error = err})
end

local function flush_outbox()
    if not S.outcur and #S.outq > 0 then
        S.outcur = table.concat(S.outq)
        S.outq = {}
        S.outpos = 1
    end

    while S.outcur do
        local sent, err, last = S.sock:send(S.outcur, S.outpos)

        if sent then
            S.outbytes = S.outbytes - (#S.outcur - S.outpos + 1)
            S.outcur = nil
            S.outpos = 1
            if #S.outq > 0 then
                S.outcur = table.concat(S.outq)
                S.outq = {}
            end
        elseif err == "timeout" then
            -- LuaSocket reports the index of the last byte it did write.
            local mark = last or (S.outpos - 1)
            local wrote = mark - S.outpos + 1
            if wrote > 0 then
                S.outbytes = S.outbytes - wrote
                S.outpos = mark + 1
            end
            return
        else
            teardown("send: " .. tostring(err))
            return
        end
    end
end

-- ------------------------------------------------------------------
-- DCS lookup tables
--
-- Built lazily and cached: the DCS singletons need not exist at the moment
-- this chunk is loaded, and resolving them once beats resolving them per
-- spawn.
-- ------------------------------------------------------------------

local MAPS = nil

local function dcs_maps()
    if MAPS then return MAPS end

    local m = {}

    m.side = {
        blue = coalition.side.BLUE,
        red = coalition.side.RED,
        neutral = coalition.side.NEUTRAL,
    }

    -- TODO(seam): a real order of battle picks the country per airbase.
    -- Country only decides which coalition DCS files the group under, and
    -- the mission's own coalition setup already fixes that, so a fixed map
    -- is enough for the slice.
    m.country = {
        blue = country.id.USA,
        red = country.id.RUSSIA,
        neutral = country.id.SWITZERLAND,
    }

    -- Category constants are small integers and AIRPLANE is 0, so every
    -- test against these uses `== nil`, never `not`.
    local GC = Group.Category
    m.category = {
        plane = GC.AIRPLANE,
        airplane = GC.AIRPLANE,
        helicopter = GC.HELICOPTER,
        ground = GC.GROUND or GC.GROUND_UNIT,
        vehicle = GC.GROUND or GC.GROUND_UNIT,
        ship = GC.SHIP,
    }

    MAPS = m
    return m
end

--- Protocol waypoint action -> the (type, action) pair DCS wants.
local WAYPOINT_ACTIONS = {
    turning_point = {"Turning Point", "Turning Point"},
    fly_over_point = {"Turning Point", "Fly Over Point"},
    landing = {"Land", "Landing"},
}

--- What waypoint 1 becomes when it names an airdrome: a cold start on that
--- airfield's ramp instead of an air start. Keyed off `airdrome_id` rather
--- than an action because the protocol's rule is positional -- only the
--- first waypoint is a departure -- and an `airdromeId` anywhere else is just
--- a point on that airfield. mission/validate_templates.lua mirrors this pair.
---
--- TODO(seam): runway and hot starts ("TakeOff" / "From Runway",
--- "TakeOffParkingHot") would hang off the same field once there is a reason
--- to choose between them.
local RAMP_START = {"TakeOffParking", "From Parking Area"}

-- ------------------------------------------------------------------
-- Templates
--
-- A template name on the wire resolves to DCS unit types here. This table
-- is a placeholder by design.
--
-- TODO(seam): real unit-template fidelity belongs here -- look up a
-- late-activation group in env.mission by name and deep-copy its unit
-- table, so loadouts, liveries and callsigns come from the mission editor
-- rather than from a literal in a script.
--
-- TODO(seam): pylons are empty. Loadout CLSIDs are DCS-version specific
-- and cannot be verified from outside a DCS install; an unknown CLSID
-- yields an empty pylon, so a wrong guess would be a silently unarmed
-- strike. Fill these from mission-editor exported group data:
--   pylons = {[3] = {CLSID = "<clsid>"}, [7] = {CLSID = "<clsid>"}}
--
-- TODO(seam): ground-war templates (front-line brigades, SAM sites,
-- logistics convoys) would be added here with category "ground". Out of
-- scope for the vertical slice; the engine does not ask for them yet.
-- ------------------------------------------------------------------

local DEFAULT_PAYLOAD = {
    pylons = {},
    fuel = 3249,
    flare = 60,
    chaff = 60,
    gun = 100,
}

local TEMPLATES = {
    ["F-16C_strike_jdam"] = {
        unit_type = "F-16C_50",
        count = 2,
        task = "Ground Attack",
        skill = "High",
        payload = DEFAULT_PAYLOAD,
    },
    ["F-16C_cap"] = {
        unit_type = "F-16C_50",
        count = 2,
        task = "CAP",
        skill = "High",
        payload = DEFAULT_PAYLOAD,
    },
    -- The red strategic target. Statics have no route and no task.
    --
    -- The name is the one the engine puts on the wire
    -- (campaign/theater.py: Target.template); a key that does not match it
    -- makes every spawn of the target fail its ack, which the engine treats
    -- as a bad template and never retries.
    --
    -- `count` matters just as much: DCS statics are one object each, but the
    -- engine models this depot as four units and reads unit counts from
    -- snapshots. A single object reporting `units = 1` against
    -- `units_initial = 4` would book three losses the instant the depot
    -- spawned, and the campaign would think it was three-quarters flattened
    -- before anyone dropped anything on it.
    ["fuel_depot_medium"] = {
        static = true,
        unit_type = "Tank",
        static_category = "Fortifications",
        count = 4,
        spread = 60,
    },
}

-- ------------------------------------------------------------------
-- Spawn
-- ------------------------------------------------------------------

--- Protocol positions are DCS world vec3: [x, y, z], y being altitude.
--- Mission group tables use 2D map coordinates plus a separate alt, where
--- DCS's `y` means the vec3's `z`. Getting this backwards puts the flight
--- in the sea off Cyprus, so the swap happens in exactly one place.
--- Returns map-x, map-y, altitude.
local function pos_xy(p)
    if type(p) ~= "table" or type(p[1]) ~= "number"
       or type(p[2]) ~= "number" or type(p[3]) ~= "number" then
        error("malformed position", 0)
    end
    return p[1], p[3], p[2]
end

--- A waypoint's `airdrome_id`, or nil when it has none.
---
--- Absent and JSON null both mean none. Anything else that is not a whole
--- number is a malformed payload, not an air start: guessing would put a
--- flight the engine believes is on the ground into the sky, or the reverse.
local function airdrome_of(wp)
    local id = wp.airdrome_id
    if id == nil or json.isnull(id) then return nil end
    if type(id) ~= "number" or id ~= math.floor(id) then
        error("malformed airdrome_id: " .. tostring(id), 0)
    end
    return id
end

local function build_route(spawn)
    local points = {}
    local route = spawn.route

    if type(route) ~= "table" or #route == 0 then
        -- No route: orbit the spawn point rather than fly off the map.
        local x, y, alt = pos_xy(spawn.position)
        points[1] = {
            x = x, y = y, alt = alt, alt_type = "BARO",
            type = "Turning Point", action = "Turning Point",
            speed = 200, speed_locked = true,
            ETA = 0, ETA_locked = false,
            task = {id = "ComboTask", params = {tasks = {}}},
        }
        return {points = points}
    end

    local attack_at = nil
    for i = 1, #route do
        local wp = route[i]
        local x, y, alt = pos_xy(wp.pos)
        local airdrome = airdrome_of(wp)
        local kind = WAYPOINT_ACTIONS[wp.action] or WAYPOINT_ACTIONS.turning_point
        if i == 1 and airdrome then kind = RAMP_START end
        if wp.action == "attack" and not attack_at then attack_at = i end
        points[i] = {
            x = x,
            y = y,
            alt = (type(wp.alt) == "number" and wp.alt > 0) and wp.alt or alt,
            alt_type = "BARO",
            type = kind[1],
            action = kind[2],
            speed = (type(wp.speed) == "number" and wp.speed > 0) and wp.speed or 200,
            speed_locked = true,
            ETA = 0,
            ETA_locked = false,
            -- nil when there is none, which leaves the key out entirely.
            airdromeId = airdrome,
            task = {id = "ComboTask", params = {tasks = {}}},
        }
    end

    return {points = points, attack_at = attack_at}
end

--- A Bombing task aimed at one map point. `x`/`y` are DCS map coordinates,
--- which is the vec3's x and z -- see `pos_xy`.
local function bombing_task(number, x, y)
    return {
        number = number,
        auto = false,
        id = "Bombing",
        enabled = true,
        params = {
            point = {x = x, y = y},
            attackQty = 1,
            expend = "All",
            groupAttack = true,
        },
    }
end

--- Attach a best-effort attack task to the waypoint the engine marked
--- `attack`, falling back to the last one.
---
--- Which waypoint this lands on is not cosmetic. The engine's strike route
--- is ingress, target, home, so hanging the task on the last point tasks the
--- flight to attack *after* it has landed, and nothing is ever struck.
---
--- Three ways to name the target, in descending order of fidelity: the group
--- it names, the static object it names, and -- when neither exists yet --
--- the ground under the attack waypoint. The last one is not a corner case.
--- A flight is instantiated when it is near the players, and on a strike it
--- usually is long before its target is, so without it the common case is a
--- two-ship with an empty task list flying a sightseeing tour of Latakia.
---
--- `tasking.tot` is deliberately ignored. The engine owns the schedule and
--- expresses it by choosing when to send the spawn, so honouring a TOT here
--- would mean two clocks disagreeing about the same flight. If a waypoint
--- ETA is ever wanted, it is derived from tot, not substituted for it.
---
--- TODO(seam): SEAD, escort, tanker and AWACS packages, and any richer
--- task composition, belong here. Multi-package deconfliction is the
--- engine's problem, not this function's: spawn frames arrive
--- independently and the client never reasons about two packages at once.
local function attach_tasking(route, tasking)
    if type(tasking) ~= "table" then return end
    local target = tasking.target
    if type(target) ~= "string" or #route.points == 0 then return end

    local waypoint = route.points[route.attack_at or #route.points]
    local tasks = waypoint.task.params.tasks

    local grp = try(Group.getByName, target)
    if grp and try(grp.isExist, grp) then
        tasks[#tasks + 1] = {
            number = #tasks + 1,
            auto = false,
            id = "AttackGroup",
            enabled = true,
            params = {groupId = grp:getID(), expend = "All", groupAttack = true},
        }
        return
    end

    -- Statics cannot be an AttackGroup target, so bomb the point instead.
    -- A multi-object static template numbers its objects, so the bare
    -- spawn-id name may not resolve; the first object is the aim point.
    local stat = try(StaticObject.getByName, target)
                 or try(StaticObject.getByName, target .. "_1")
    local point = stat and try(stat.getPoint, stat)
    if point then
        tasks[#tasks + 1] = bombing_task(#tasks + 1, point.x, point.z)
        return
    end

    -- Nothing resolved, which is the ordinary case rather than the odd one:
    -- the bubble instantiates whatever is nearest the players, and a package
    -- flying out to a target is usually inside it long before the target is.
    -- The protocol has no re-tasking frame and the flight is not re-spawned,
    -- so a group left with an empty ComboTask flies its route and drops
    -- nothing, ever. The attack waypoint is the target's own position, which
    -- is everything a Bombing task needs.
    local wp = route.attack_at and route.points[route.attack_at]
    if wp then
        tasks[#tasks + 1] = bombing_task(#tasks + 1, wp.x, wp.y)
    end
end

--- How many units this spawn brings: exactly `frame.units`, or a refusal.
---
--- The engine owns the airframe ledger, so the count is its call and never
--- the template's. A flight that lost a wingman is re-issued as a single-ship,
--- and building it from the template would hand the campaign back an aircraft
--- it has already written off. A count the template cannot hold is refused
--- rather than clamped: a clamp builds fewer units than the engine issued,
--- and the next census books the difference as a loss nobody caused.
--- tools/fake_dcs.py refuses the same things with the same words.
local function unit_count_for(tmpl, frame)
    local units = frame.units
    if type(units) ~= "number" or units ~= math.floor(units) or units < 1 then
        return nil, "bad units: " .. tostring(units)
                    .. " (want a positive integer)"
    end
    local capacity = tmpl.count or 1
    if units > capacity then
        return nil, "units " .. units .. " exceeds template "
                    .. tostring(frame.template) .. " capacity of " .. capacity
    end
    return units
end


local function build_group_data(spawn, tmpl, name, count)
    local units = {}
    local gx, gy, galt = pos_xy(spawn.position)
    local heading = tonumber(spawn.heading) or 0

    for i = 1, count do
        units[i] = {
            -- Unit names carry the prefix too, so anything that leaks into
            -- an event is still recognisably ours even though the engine
            -- resolves entities by group name.
            name = name .. "_" .. i,
            type = tmpl.unit_type,
            -- 50 m lateral stagger: a formation stacked on one point is a
            -- mid-air the moment it spawns.
            x = gx + (i - 1) * 50,
            y = gy + (i - 1) * 50,
            alt = galt,
            alt_type = "BARO",
            heading = heading,
            speed = 200,
            skill = tmpl.skill or "High",
            payload = tmpl.payload or DEFAULT_PAYLOAD,
        }
    end

    local route = build_route(spawn)
    -- Tasking is guarded on its own: a flight that reaches the target
    -- without a task beats a flight that never spawns.
    guard("tasking for " .. name, attach_tasking, route, spawn.tasking)

    return {
        name = name,
        task = tmpl.task or "Nothing",
        visible = false,
        uncontrolled = false,
        hidden = false,
        x = gx,
        y = gy,
        route = route,
        units = units,
    }
end

--- One object of a possibly multi-object static template.
--- `index` is 1-based; objects are laid out on a ring so they are separate
--- aim points rather than one stack a single bomb flattens.
local function build_static_data(spawn, tmpl, name, index)
    local x, y = pos_xy(spawn.position)
    local count = tmpl.count or 1
    if count > 1 then
        local spread = tmpl.spread or 60
        local angle = (2 * math.pi * (index - 1)) / count
        x = x + spread * math.cos(angle)
        y = y + spread * math.sin(angle)
    end
    return {
        name = name,
        type = tmpl.unit_type,
        category = tmpl.static_category,
        x = x,
        y = y,
        heading = tonumber(spawn.heading) or 0,
        dead = false,
    }
end

local function handle_spawn(frame)
    local sid = frame.spawn_id
    if type(sid) ~= "string" or sid == "" then
        send_ack(frame.ref, false, "missing spawn_id")
        return
    end

    if S.spawns[sid] then
        send_ack(frame.ref, false, "duplicate spawn_id: " .. sid)
        return
    end

    local tmpl = TEMPLATES[frame.template]
    if not tmpl then
        send_ack(frame.ref, false, "unknown template: " .. tostring(frame.template))
        return
    end

    local maps = dcs_maps()
    local country_id = maps.country[frame.coalition]
    if country_id == nil then
        send_ack(frame.ref, false, "unknown coalition: " .. tostring(frame.coalition))
        return
    end

    local unit_count, why = unit_count_for(tmpl, frame)
    if not unit_count then
        log_err("spawn " .. sid .. ": " .. why)
        send_ack(frame.ref, false, why)
        return
    end

    local name = group_name(sid)
    local names = {name}

    if tmpl.static then
        -- Several objects under one spawn_id: DCS has no static group, so the
        -- engine's multi-unit target becomes N objects the client counts.
        --
        -- How many is the engine's call, exactly as for an aircraft group. A
        -- target the campaign has already half-flattened leaves the bubble and
        -- comes back, and rebuilding it from the template would stand the
        -- destroyed objects back up: the next census would honestly report them
        -- and the engine would need them destroyed all over again. Do that
        -- often enough and the target can never be finished at all.
        local total = tmpl.count or 1
        local count = unit_count
        names = {}
        local failure = nil
        for i = 1, count do
            -- Suffixed off the template, not the surviving count, so an object
            -- keeps its name across bubble cycles.
            local member = (total > 1) and (name .. "_" .. i) or name
            local built, data = pcall(build_static_data, frame, tmpl, member, i)
            if not built then
                failure = "bad spawn payload: " .. tostring(data)
                break
            end
            local ok, err = pcall(coalition.addStaticObject, country_id, data)
            if not ok then
                failure = "addStaticObject failed: " .. tostring(err)
                break
            end
            -- pcall only catches a raised error. DCS routinely fails a static
            -- silently -- an unsupported category pair, a Fortifications type
            -- with no shape_name -- logging internally and returning nil. An
            -- ack of "ok" for an object that does not exist is worse than a
            -- refusal: the next census reports it destroyed.
            local obj = try(StaticObject.getByName, member)
            if not (obj and try(obj.isExist, obj)) then
                failure = "addStaticObject created nothing: " .. tostring(frame.template)
                break
            end
            names[#names + 1] = member
        end
        if failure then
            -- Unwind: a half-built target would report fewer units than the
            -- engine issued, and the engine would book the difference as a
            -- loss nobody caused.
            for j = 1, #names do
                local obj = try(StaticObject.getByName, names[j])
                if obj then guard("destroy " .. names[j], obj.destroy, obj) end
            end
            log_err("spawn " .. sid .. ": " .. failure)
            send_ack(frame.ref, false, failure)
            return
        end
    else
        if maps.category[frame.category] == nil then
            send_ack(frame.ref, false, "unknown category: " .. tostring(frame.category))
            return
        end
        local built, data = pcall(build_group_data, frame, tmpl, name, unit_count)
        if not built then
            send_ack(frame.ref, false, "bad spawn payload: " .. tostring(data))
            return
        end
        local ok, err =
            pcall(coalition.addGroup, country_id, maps.category[frame.category], data)
        if not ok then
            log_err("spawn " .. sid .. ": " .. tostring(err))
            send_ack(frame.ref, false, "addGroup failed: " .. tostring(err))
            return
        end
        -- As above: `addGroup` fails silently on a table DCS does not like --
        -- an unknown unit type, an unsupported country/category pair -- and
        -- returns without raising. Registering the spawn_id anyway is the one
        -- way to turn a content mistake into campaign corruption: the next
        -- census finds no group, reports zero units, and the engine writes off
        -- the whole flight as a combat loss before anyone has flown.
        local grp = try(Group.getByName, name)
        local members = grp and try(grp.getUnits, grp)
        if not (grp and try(grp.isExist, grp) and members and #members > 0) then
            if grp then guard("destroy " .. name, grp.destroy, grp) end
            local failure = "addGroup created nothing: " .. tostring(frame.template)
            log_err("spawn " .. sid .. ": " .. failure)
            send_ack(frame.ref, false, failure)
            return
        end
    end

    S.spawns[sid] = {
        name = name,
        names = names,
        units_initial = unit_count,
        category = tmpl.static and "static" or frame.category,
        coalition = frame.coalition,
        -- Unit names DCS has told us landed. See snapshot_of: DCS deletes AI
        -- aircraft a short while after they land, and without this the client
        -- would report a flight that made it home as a flight that died.
        landed = {},
    }

    log_info("spawned " .. name .. " from " .. tostring(frame.template)
             .. " (" .. unit_count .. " unit(s))")
    send_ack(frame.ref, true)
end

-- ------------------------------------------------------------------
-- State reports
-- ------------------------------------------------------------------

local function destroy_entity(rec)
    if rec.category == "static" then
        local names = rec.names or {rec.name}
        for i = 1, #names do
            local obj = try(StaticObject.getByName, names[i])
            if obj then guard("destroy " .. names[i], obj.destroy, obj) end
        end
    else
        local grp = try(Group.getByName, rec.name)
        if grp then guard("destroy " .. rec.name, grp.destroy, grp) end
    end
end

local function snapshot_of(sid, rec)
    local snap = {
        spawn_id = sid,
        alive = false,
        units = 0,
        units_initial = rec.units_initial,
    }

    if rec.category == "static" then
        -- Count the objects that are still there. This is the whole reason a
        -- multi-object template exists: it is what lets the engine see a
        -- target half-flattened instead of only intact or gone.
        local names = rec.names or {rec.name}
        local alive, first = 0, nil
        for i = 1, #names do
            local obj = try(StaticObject.getByName, names[i])
            if obj and try(obj.isExist, obj) then
                alive = alive + 1
                if not first then first = try(obj.getPoint, obj) end
            end
        end
        snap.units = alive
        snap.alive = alive > 0
        if first then snap.pos = json.array({first.x, first.y, first.z}) end
        return snap
    end

    -- Aircraft DCS has deleted since they landed. DCS removes AI aircraft a
    -- short while after they are down, so their handles vanish exactly like a
    -- destroyed unit's. Counting them as gone would report a flight that made
    -- it home as a flight that died -- and the engine, obeying the
    -- reconciliation rule correctly, would write off the whole package. The
    -- rule is only as good as the census feeding it, so the census has to know
    -- the difference between an aircraft that is missing and one that is
    -- parked.
    local recovered, recovered_names = 0, rec.landed or {}
    for _ in pairs(recovered_names) do recovered = recovered + 1 end

    local grp = try(Group.getByName, rec.name)
    if not (grp and try(grp.isExist, grp)) then
        -- Gone with no event is the case snapshots exist to catch. Report
        -- it as dead rather than omitting it -- unless we were told it landed,
        -- in which case it is at an airbase, not at the bottom of a crater.
        snap.units = math.min(recovered, rec.units_initial)
        snap.alive = snap.units > 0
        return snap
    end

    -- Counted from the unit list rather than Group:getSize(), which has
    -- historically lagged a frame behind a death. A miscount here writes a
    -- wrong loss into the campaign, so count what actually exists.
    local units = try(grp.getUnits, grp) or {}
    local alive, first = 0, nil
    local present = {}
    for i = 1, #units do
        local u = units[i]
        if u and try(u.isExist, u) then
            alive = alive + 1
            local uname = try(u.getName, u)
            if uname then present[uname] = true end
            if not first then first = try(u.getPoint, u) end
        end
    end

    -- A landed aircraft DCS has already deleted is still an aircraft we have.
    -- Only count the ones no longer in the unit list, or a jet that landed and
    -- is still sitting there would be counted twice.
    for uname in pairs(recovered_names) do
        if not present[uname] then alive = alive + 1 end
    end

    snap.units = math.min(alive, rec.units_initial)
    snap.alive = snap.units > 0
    if first then snap.pos = json.array({first.x, first.y, first.z}) end
    return snap
end

local function send_state()
    local ids = {}
    for sid in pairs(S.spawns) do ids[#ids + 1] = sid end
    -- Sorted so two runs of the same campaign produce the same bytes.
    table.sort(ids)

    -- json.array, not {}: with no live spawns this has to encode as [],
    -- and an untagged empty table would go out as {}.
    local groups = json.array({})
    for i = 1, #ids do
        groups[i] = snapshot_of(ids[i], S.spawns[ids[i]])
    end

    -- TODO(seam): a campaign with hundreds of live entities outgrows one
    -- 64 KiB frame and needs chunked state reports. The slice does not.
    send_frame({type = "state", groups = groups})
end

-- ------------------------------------------------------------------
-- Despawn
-- ------------------------------------------------------------------

local function handle_despawn(frame)
    local sid = frame.spawn_id
    local rec = (type(sid) == "string") and S.spawns[sid] or nil

    if not rec then
        -- An unknown spawn_id is a successful no-op, per the protocol.
        send_ack(frame.ref, true)
        return
    end

    -- Ground truth first. After the destroy this group reads as dead, and
    -- a flight recalled from the bubble would be booked as a combat loss.
    send_state()

    destroy_entity(rec)
    S.spawns[sid] = nil

    log_info("despawned " .. rec.name .. " (" .. tostring(frame.reason) .. ")")
    send_ack(frame.ref, true)
end

local function destroy_all_spawns(why)
    local n = 0
    for sid, rec in pairs(S.spawns) do
        destroy_entity(rec)
        S.spawns[sid] = nil
        n = n + 1
    end
    if n > 0 then
        log_warn("destroyed " .. n .. " engine-owned group(s): " .. why)
    end
end

-- ------------------------------------------------------------------
-- Messages
-- ------------------------------------------------------------------

local function handle_message(frame)
    local text = frame.text
    if type(text) ~= "string" then return end
    local duration = tonumber(frame.duration) or 15

    if frame.to == "all" then
        guard("outText", trigger.action.outText, text, duration, false)
        return
    end

    local side = dcs_maps().side[frame.to]
    if side == nil then
        log_warn("message to unknown recipient: " .. tostring(frame.to))
        return
    end
    guard("outTextForCoalition", trigger.action.outTextForCoalition,
          side, text, duration, false)
end

-- ------------------------------------------------------------------
-- Sync
-- ------------------------------------------------------------------

local function handle_sync(frame)
    if frame.protocol ~= PROTOCOL_VERSION then
        log_err("engine speaks protocol " .. tostring(frame.protocol)
                .. ", this client speaks " .. PROTOCOL_VERSION)
        -- The engine closes the connection on a mismatch. Back all the way
        -- off rather than reconnect-loop against a version we cannot talk.
        S.backoff = CONFIG.reconnect_max
        teardown("protocol mismatch")
        return
    end

    S.synced = true
    S.sync_deadline = nil
    S.campaign_time = frame.campaign_time
    -- Stored for diagnostics only. The engine decides what is in the bubble
    -- and says so with spawn and despawn; a client that also applied the
    -- radius would be a second opinion nobody asked for.
    S.bubble_radius = frame.bubble_radius

    -- Cadences come from the engine. There are no hardcoded periods here;
    -- the fallbacks below only cover a sync that omitted them entirely.
    S.state_period = tonumber(frame.state_period) or 30
    S.observer_period = tonumber(frame.observer_period) or 5

    local now = now_t()
    S.next_state_at = now
    S.next_observer_at = now

    S.backoff = CONFIG.reconnect_base

    log_info("synced: campaign_time=" .. tostring(S.campaign_time)
             .. " state_period=" .. S.state_period
             .. " observer_period=" .. S.observer_period
             .. " bubble_radius=" .. tostring(S.bubble_radius))
end

-- ------------------------------------------------------------------
-- Observers
-- ------------------------------------------------------------------

local function observer_of(unit, player)
    local p = try(unit.getPoint, unit)
    if not p then return nil end
    local v = try(unit.getVelocity, unit)
    local speed = 0
    if v then speed = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z) end
    return {
        id = "player:" .. player,
        pos = json.array({p.x, p.y, p.z}),
        speed = speed,
    }
end

-- TODO(seam): pilot records hang off this stream and the event stream --
-- player name plus the spawn_id they flew. Out of scope for the slice;
-- nothing here persists anything about a human.
local function send_observer()
    -- json.array, not {}: an empty server is the steady state, and the
    -- engine needs [] rather than {}.
    local list = json.array({})
    local seen = {}

    -- The birth-event registry is the primary source: no world walk, and
    -- available on every DCS build.
    for name in pairs(S.players) do
        local unit = try(Unit.getByName, name)
        local player = nil
        if unit and try(unit.isExist, unit) then
            player = try(unit.getPlayerName, unit)
        end
        if player then
            local o = observer_of(unit, player)
            if o then
                seen[name] = true
                list[#list + 1] = o
            end
        else
            S.players[name] = nil
        end
    end

    -- coalition.getPlayers is the reconciliation pass, for a client that
    -- slotted before this script loaded or whose birth event was lost. It
    -- is guarded because it is not present on every DCS build.
    if coalition.getPlayers then
        for _, side in pairs(dcs_maps().side) do
            local units = try(coalition.getPlayers, side) or {}
            for i = 1, #units do
                local unit = units[i]
                local name = unit and try(unit.getName, unit)
                if name and not seen[name] then
                    local player = try(unit.getPlayerName, unit)
                    local o = player and observer_of(unit, player) or nil
                    if o then
                        seen[name] = true
                        S.players[name] = true
                        list[#list + 1] = o
                    end
                end
            end
        end
    end

    send_frame({type = "observer", observers = list})
end

-- ------------------------------------------------------------------
-- Events
--
-- The spec's kinds are the DCS event names with S_EVENT_ stripped and
-- lowercased, so the table is derived from world.event itself rather than
-- written out by hand. Anything off the spec's list is ignored in silence.
-- ------------------------------------------------------------------

local WIRE_KINDS = {
    birth = true, takeoff = true, land = true, shot = true, hit = true,
    kill = true, dead = true, crash = true, eject = true,
    pilot_dead = true, base_captured = true,
}

--- DCS calls it S_EVENT_EJECTION; the wire calls it `eject`.
local KIND_ALIASES = {ejection = "eject"}

--- Sent even when neither party is engine-owned: a base changing hands is
--- campaign-wide news, not attribution for one entity.
local ALWAYS_SEND = {base_captured = true}

local EVENT_KIND = nil       -- DCS event id -> wire kind
local EVENT_RAW = nil        -- DCS event id -> raw lowercase name

local function build_event_tables()
    if EVENT_KIND then return end
    EVENT_KIND, EVENT_RAW = {}, {}
    for name, id in pairs(world.event) do
        if type(id) == "number" then
            local bare = string.match(name, "^S_EVENT_(.+)$")
            if bare then
                bare = string.lower(bare)
                EVENT_RAW[id] = bare
                local kind = KIND_ALIASES[bare] or bare
                if WIRE_KINDS[kind] then EVENT_KIND[id] = kind end
            end
        end
    end
end

--- Raw event names that change who the humans are, so the next observer
--- report should not wait out the cadence.
local PLAYER_GONE = {
    player_leave_unit = true, pilot_dead = true, crash = true,
    ejection = true, dead = true, unit_lost = true,
}

--- An object's own name, with no group lookup. See the filter in
--- on_event_body: this is the cheap half of object_name.
local function raw_name(obj)
    if not obj then return nil end
    return try(obj.getName, obj)
end

--- The spawn record a DCS object belongs to, plus that object's own name.
---
--- `object_name` resolves a unit to its *group* name, which is what maps to a
--- spawn_id; the unit's own name is what the landed set is keyed by.
local function owned_record(obj)
    local gname = object_name(obj)
    if not is_owned(gname) then return nil, nil end
    local rec = S.spawns[string.sub(gname, #OWNED_PREFIX + 1)]
    if not rec then return nil, nil end
    return rec, try(obj.getName, obj)
end

local function on_event_body(e)
    build_event_tables()

    local raw = EVENT_RAW[e.id]
    if not raw then return end

    local ini = e.initiator

    -- Landing bookkeeping, before any connection check: whether the engine is
    -- listening has no bearing on whether an aircraft got home, and a landing
    -- missed during a reconnect would be a flight written off on the next
    -- census. This reads DCS's own events, not the wire, so suppressing event
    -- frames to the engine cannot change what a snapshot says.
    if raw == "land" and ini then
        local rec, uname = owned_record(ini)
        if rec and uname and rec.category ~= "static" then
            rec.landed[uname] = true
        end
    elseif (raw == "dead" or raw == "crash" or raw == "unit_lost") and ini then
        -- Destroyed after landing -- on the ramp, or by the airfield being
        -- hit. It is no longer a recovered aircraft.
        local rec, uname = owned_record(ini)
        if rec and uname and rec.landed then rec.landed[uname] = nil end
    end

    -- Player bookkeeping happens whether or not there is a connection.
    if raw == "birth" and ini then
        if try(ini.getPlayerName, ini) then
            local name = try(ini.getName, ini)
            if name then
                S.players[name] = true
                S.next_observer_at = 0
            end
        end
    elseif PLAYER_GONE[raw] and ini then
        local name = try(ini.getName, ini)
        if name and S.players[name] then
            S.players[name] = nil
            S.next_observer_at = 0
        end
    end

    local kind = EVENT_KIND[e.id]
    if not kind then return end
    if S.phase ~= "open" or not S.synced then return end

    -- Traffic control, on the objects' own names. The engine ignores
    -- anything without the cmp_ prefix anyway, and a busy mission generates
    -- far more events than the campaign cares about -- AI ground combat
    -- alone fires shot and hit in the thousands per second. Dropping them
    -- here is safe precisely because events are attribution only: losses
    -- come from state snapshots.
    --
    -- Deliberately ahead of object_name, which costs up to three pcall'd
    -- trips into the DCS object model per object and runs on the sim thread.
    -- It is exactly equivalent: an engine-owned entity carries the prefix on
    -- its unit and object names too (`cmp_<id>_<n>`, see build_group_data
    -- and build_static_data), so anything object_name would resolve to an
    -- owned group name is already owned by its own name.
    if not (ALWAYS_SEND[kind]
            or is_owned(raw_name(ini))
            or is_owned(raw_name(e.target))) then
        return
    end

    local initiator = object_name(ini)
    local target = object_name(e.target)

    local weapon = nil
    if e.weapon then weapon = try(e.weapon.getTypeName, e.weapon) end
    local place = nil
    if e.place then place = try(e.place.getName, e.place) end

    send_frame({
        type = "event",
        kind = kind,
        initiator = initiator,
        target = target,
        weapon = weapon,
        place = place,
    })
end

local EVENT_HANDLER = {}

function EVENT_HANDLER:onEvent(e)
    if not S.running or not e then return end
    -- A DCS event handler that raises is removed from the world for the
    -- rest of the mission. Nothing gets out of here.
    local ok, err = pcall(on_event_body, e)
    if not ok then
        log_err("onEvent(" .. tostring(e.id) .. "): " .. tostring(err))
    end
end

-- ------------------------------------------------------------------
-- Inbound dispatch
-- ------------------------------------------------------------------

local HANDLERS = {
    sync = handle_sync,
    spawn = handle_spawn,
    despawn = handle_despawn,
    message = handle_message,
}

local function dispatch(frame)
    if type(frame) ~= "table" then return end
    local handler = HANDLERS[frame.type]
    if not handler then
        if frame.ref ~= nil then
            send_ack(frame.ref, false, "unknown frame type: " .. tostring(frame.type))
        end
        return
    end
    handler(frame)
end

--- Split whatever has arrived into whole frames. A partial tail stays in
--- the buffer for the next tick.
local function extract_frames()
    while true do
        local idx = string.find(S.inbuf, "\n", 1, true)
        if not idx then
            if #S.inbuf > MAX_FRAME_BYTES then
                teardown("inbound frame exceeds MAX_FRAME_BYTES with no newline")
            end
            return
        end
        local text = string.sub(S.inbuf, 1, idx - 1)
        S.inbuf = string.sub(S.inbuf, idx + 1)
        if #text > 0 then
            S.queue_tail = S.queue_tail + 1
            S.queue[S.queue_tail] = text
        end
    end
end

local function process_frames()
    local budget = MAX_FRAMES_PER_TICK
    while budget > 0 and S.queue_head <= S.queue_tail do
        local text = S.queue[S.queue_head]
        S.queue[S.queue_head] = nil
        S.queue_head = S.queue_head + 1
        budget = budget - 1

        local frame, err = json.decode(text)
        if not frame then
            log_err("decode: " .. tostring(err))
        else
            guard("dispatch " .. tostring(frame.type), dispatch, frame)
        end

        -- A handler may have torn the connection down under us.
        if S.phase ~= "open" then return end
    end

    if S.queue_head > S.queue_tail then
        S.queue = {}
        S.queue_head = 1
        S.queue_tail = 0
    end
end

-- ------------------------------------------------------------------
-- Connection lifecycle
-- ------------------------------------------------------------------

local function reset_stream()
    S.inbuf = ""
    S.queue = {}
    S.queue_head = 1
    S.queue_tail = 0
    S.outq = {}
    S.outcur = nil
    S.outpos = 1
    S.outbytes = 0
    -- Sequence numbers are per connection and start at 1, so hello is
    -- always seq 1.
    S.seq = 0
    S.synced = false
    S.sync_deadline = nil
    S.next_state_at = nil
    S.next_observer_at = nil
end

local function close_socket()
    if S.sock then
        pcall(function() S.sock:close() end)
        S.sock = nil
    end
end

local function back_off(now)
    S.backoff = math.min(S.backoff * 2, CONFIG.reconnect_max)
    S.next_connect_at = now + S.backoff
end

teardown = function(reason)
    local was = S.phase
    close_socket()
    S.phase = "idle"
    reset_stream()

    if was ~= "idle" then
        log_warn("disconnected: " .. tostring(reason))
    end

    -- Everything the engine asked for is now unmanaged: it has no idea
    -- these groups exist and, on reconnect, it re-issues spawn for all of
    -- them. A duplicate spawn_id is a hard failure by the protocol, so the
    -- only consistent thing to do is give the entities back.
    destroy_all_spawns("connection lost; engine re-issues spawns on reconnect")

    back_off(now_t())
end

local function send_hello()
    local theatre = ""
    local epoch = 0

    if env and env.mission then
        theatre = env.mission.theatre or ""
        local d = env.mission.date
        local start = tonumber(env.mission.start_time) or 0
        if type(d) == "table" and d.Year and d.Month and d.Day then
            epoch = days_from_civil(d.Year, d.Month, d.Day) * 86400 + start
        end
    end

    send_frame({
        type = "hello",
        protocol = PROTOCOL_VERSION,
        theater = theatre,
        -- The sanitised mission environment exposes no DCS build string,
        -- so this stays empty rather than guessing.
        dcs_version = "",
        mission_start_epoch = epoch,
    })
end

--- Is `host` an IPv4 literal LuaSocket can use without resolving it?
---
--- settimeout(0) bounds the TCP handshake and nothing else: connect() hands
--- the string to getaddrinfo first, and that call is synchronous. A name
--- that does not resolve costs Windows a DNS round trip, then LLMNR, then
--- NetBIOS -- one to three seconds of frozen sim, on every backoff, for as
--- long as the engine is unreachable. It is the one path left by which this
--- file can block, so a name is refused rather than tried. socket.tcp() is
--- IPv4 only, which makes a dotted quad the whole of what is useful here.
local function is_ip_literal(host)
    if type(host) ~= "string" then return false end
    local octets = {string.match(host, "^(%d+)%.(%d+)%.(%d+)%.(%d+)$")}
    if #octets ~= 4 then return false end
    for i = 1, 4 do
        local n = tonumber(octets[i])
        if not n or n > 255 or #octets[i] > 3 then return false end
    end
    return true
end

--- The address to dial for `host`, or nil if it would have to be resolved.
---
--- "localhost" is the one name worth honouring: it is what people write, and
--- it is answerable here without asking anything, so refusing it would be
--- friction with no safety bought. Every other name goes to the resolver, so
--- it is refused.
local function dial_address(host)
    if is_ip_literal(host) then return host end
    if type(host) == "string" and string.lower(host) == "localhost" then
        return "127.0.0.1"
    end
    return nil
end

local function begin_connect(now)
    local address = dial_address(CONFIG.host)
    if not address then
        if now - S.last_fail_log > CONFIG.log_throttle then
            S.last_fail_log = now
            log_err("CONFIG.host must be an IPv4 address or \"localhost\", not \""
                    .. tostring(CONFIG.host) .. "\": resolving a name blocks "
                    .. "the simulation thread. Use the engine's IP address.")
        end
        back_off(now)
        return
    end

    local sock_lib = load_socket()
    if not sock_lib then
        if now - S.last_fail_log > CONFIG.log_throttle then
            S.last_fail_log = now
            log_err("LuaSocket is unavailable; see mission/README.md for the "
                    .. "MissionScripting.lua change this needs")
        end
        back_off(now)
        return
    end

    local sock = try(sock_lib.tcp)
    if not sock then
        back_off(now)
        return
    end

    sock:settimeout(0)
    local ok, err = sock:connect(address, CONFIG.port)

    -- A non-blocking connect all but always reports "timeout", or an
    -- in-progress message whose text differs per platform, and finishes
    -- later. Rather than match error strings, poll for writability and
    -- confirm with getpeername, which behaves the same everywhere.
    if not ok and err == "closed" then
        pcall(function() sock:close() end)
        back_off(now)
        return
    end

    S.sock = sock
    S.phase = "connecting"
    S.connect_deadline = now + CONFIG.connect_timeout
end

local function connect_failed(now, why)
    if now - S.last_fail_log > CONFIG.log_throttle then
        S.last_fail_log = now
        log_warn(why .. " at " .. CONFIG.host .. ":" .. CONFIG.port
                 .. "; retrying in the background")
    end
    close_socket()
    S.phase = "idle"
    back_off(now)
end

local function poll_connect(now)
    local _, writable = socket.select({}, {S.sock}, 0)

    if writable and writable[1] then
        if try(S.sock.getpeername, S.sock) then
            -- Frames are small and latency-sensitive; Nagle would sit on
            -- an ack waiting for traffic that is not coming.
            pcall(function() S.sock:setoption("tcp-nodelay", true) end)
            S.phase = "open"
            reset_stream()
            S.last_fail_log = -1e18
            log_info("connected to " .. CONFIG.host .. ":" .. CONFIG.port)
            S.sync_deadline = now + CONFIG.sync_timeout
            send_hello()
            flush_outbox()
            return
        end
        -- Writable with no peer: the connect was refused.
        connect_failed(now, "engine not reachable")
        return
    end

    if now >= S.connect_deadline then
        connect_failed(now, "connect timed out")
    end
end

local function pump_recv()
    local budget = CONFIG.recv_budget

    while budget > 0 do
        local want = CONFIG.recv_chunk
        if want > budget then want = budget end

        local chunk, err, partial = S.sock:receive(want)
        local data = chunk or partial

        if data and #data > 0 then
            S.inbuf = S.inbuf .. data
            budget = budget - #data
        end

        if not chunk then
            if err ~= "timeout" then
                teardown("receive: " .. tostring(err))
                return false
            end
            break
        end
    end

    extract_frames()
    return S.phase == "open"
end

local function periodic(now)
    if not S.synced then
        if S.sync_deadline and now >= S.sync_deadline then
            -- Open, and silent. Back off and try again rather than sit here:
            -- an engine that refused this client's protocol version hangs up
            -- without answering, and so does one that is wedged.
            teardown("engine did not answer hello with sync")
        end
        return
    end

    if S.next_observer_at and now >= S.next_observer_at then
        S.next_observer_at = now + S.observer_period
        guard("observer report", send_observer)
    end

    if S.next_state_at and now >= S.next_state_at then
        S.next_state_at = now + S.state_period
        guard("state report", send_state)
    end
end

-- ------------------------------------------------------------------
-- Tick
-- ------------------------------------------------------------------

local function tick_body(now)
    if S.phase == "idle" then
        if now >= S.next_connect_at then begin_connect(now) end
        return
    end

    if S.phase == "connecting" then
        poll_connect(now)
        return
    end

    if not pump_recv() then return end
    process_frames()
    if S.phase ~= "open" then return end
    periodic(now)
    if S.phase ~= "open" then return end
    flush_outbox()
end

local function tick()
    if not S.running then return nil end

    local now = now_t()
    local ok, err = pcall(tick_body, now)
    if not ok then
        log_err("tick: " .. tostring(err))
        -- A tick that blew up may have left the socket half-consumed.
        -- Dropping the connection is recoverable; a wedged client is not.
        pcall(teardown, "tick error")
    end

    -- Scheduled off the current time rather than the scheduled time, so a
    -- stalled sim cannot leave a backlog of catch-up ticks to burn through.
    return timer.getTime() + CONFIG.tick
end

-- ------------------------------------------------------------------
-- Public surface
-- ------------------------------------------------------------------

local M = {}

function M.start()
    if S.running then return end
    S.running = true
    S.phase = "idle"
    S.backoff = CONFIG.reconnect_base
    S.next_connect_at = 0
    S.last_fail_log = -1e18
    reset_stream()

    guard("addEventHandler", world.addEventHandler, EVENT_HANDLER)
    S.sched_id = timer.scheduleFunction(tick, {}, timer.getTime() + CONFIG.tick)

    log_info("client started, engine at " .. CONFIG.host .. ":" .. CONFIG.port)
end

function M.stop()
    if not S.running then return end
    S.running = false

    if S.sched_id then
        pcall(timer.removeFunction, S.sched_id)
        S.sched_id = nil
    end

    guard("removeEventHandler", world.removeEventHandler, EVENT_HANDLER)

    close_socket()
    S.phase = "idle"
    reset_stream()

    -- Exactly what teardown() does, and for the same two reasons. The engine
    -- re-issues every spawn on the next connect, and a duplicate spawn_id is a
    -- hard failure that scrubs the package for good -- so a stop/start pair
    -- would kill whatever is in the air. And the self-reload guard at the
    -- bottom of this file calls the *old* module's stop: groups left behind
    -- there are unreachable for the rest of the mission, because the new
    -- client spawns its own under the same names and getByName resolves only
    -- one of them.
    destroy_all_spawns("client stopped")

    log_info("client stopped")
end

--- Read-only view, for poking at the client from a debug hook.
function M.status()
    local live = 0
    for _ in pairs(S.spawns) do live = live + 1 end
    return {
        running = S.running,
        phase = S.phase,
        synced = S.synced,
        seq = S.seq,
        live_spawns = live,
        queued_frames = S.queue_tail - S.queue_head + 1,
        outbox_bytes = S.outbytes,
    }
end

M.CONFIG = CONFIG
M.TEMPLATES = TEMPLATES

-- Reloading this file (a second DO SCRIPT FILE, or a mission restart
-- inside one DCS session) must not leave two clients fighting over one
-- socket and one event handler.
if _G.CampaignClient and _G.CampaignClient.stop then
    pcall(_G.CampaignClient.stop)
end
_G.CampaignClient = M

-- Guarded so the file can still be loaded outside DCS for syntax checking.
if timer and world and coalition then
    M.start()
end

return M
