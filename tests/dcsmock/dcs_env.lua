--[[----------------------------------------------------------------------
  tests/dcsmock/dcs_env.lua -- a stand-in for the DCS mission environment.

  Installs the globals mission/campaign_client.lua actually touches, and
  nothing else. Every entry point here was found by reading the client, not
  by guessing at the DCS API: `env`, `timer`, `coalition`, `country`,
  `Group`, `StaticObject`, `Unit`, `world`, `trigger.action`.

  Ammunition is modelled only as far as Unit:getAmmo goes: a unit answers
  with what its type was given in `ammo_by_type` when it was built (nil, as
  DCS answers for an empty jet, by default), `fire_weapon` takes rounds off
  it, and `ammo_raises` makes one unit's getAmmo raise. Nothing here is a
  weapons model; a test decides what was fired.

  Two things this models properly, because the client's correctness turns on
  them:

  * a scheduled function really is called on its schedule, with the DCS
    contract that its return value is the next absolute time and a nil
    unschedules it;
  * a spawned group is a live object. It keeps the data table the client
    built -- including the route and the tasks hung off it -- so a test can
    assert what was tasked, not merely what was named. Destroying it makes
    Group.getByName stop resolving, exactly as in DCS, while the handle the
    caller already holds goes stale and raises on access.

  Returns the control table. Everything the harness pokes at goes through
  it; the globals it installs are the only thing the client sees.
------------------------------------------------------------------------]]

local M = {
    time = 0.0,

    --- id -> {fn, arg, at}
    sched = {},
    next_sched_id = 0,
    --- Errors raised out of a scheduled function. DCS drops the function;
    --- so do we, and the harness fails the test on a non-empty list.
    sched_errors = {},

    --- name -> group object (only while it exists, as with getByName)
    groups = {},
    --- name -> static object
    statics = {},
    --- name -> unit object (group members and player slots alike)
    units = {},

    --- Ordered record of every addGroup / addStaticObject call, including
    --- the ones this mock was told to fail.
    spawn_calls = {},

    --- {to = "blue"|"red"|"neutral"|"all", text, duration}
    messages = {},
    --- {level = "info"|"warning"|"error", msg}
    logs = {},

    handlers = {},

    next_group_id = 1000,

    --- Failure injection. DCS fails a bad group table *silently*: it logs
    --- internally and returns nil. That is the case the client guards, so
    --- the mock has to be able to reproduce it.
    fail_add_group = false,
    fail_add_static = false,
    fail_add_group_silently = true,
    fail_add_static_silently = true,
    --- Fail exactly one static, by name. Half a multi-object target is the
    --- case the client's unwind exists for.
    fail_static_named = nil,

    --- unit type -> list of {count, desc = {typeName, category}} each unit
    --- of that type is built carrying. Absent means nil from getAmmo.
    ammo_by_type = {},
    --- unit name -> true: that unit's getAmmo raises.
    ammo_raises = {},
    --- Build units with no getAmmo at all, as an environment without the
    --- call would.
    no_get_ammo = false,

    --- What coalition.getAirbases lists. Kinds and sides are mixed and ids are
    --- out of order on purpose: a caller after an airdrome has to filter for
    --- one and choose deterministically, not take the first entry it sees.
    --- category: 0 airdrome, 1 helipad, 2 ship. side: 0 neutral, 1 red, 2 blue.
    airbases = {
        {id = 3, name = "Incirlik FARP", side = 2, category = 1,
         x = 141000, alt = 60, z = -37000},
        {id = 22, name = "Adana Sakirpasa", side = 0, category = 0,
         x = 150000, alt = 20, z = -80000},
        {id = 16, name = "Incirlik", side = 2, category = 0,
         x = 142000, alt = 60, z = -38000},
        {id = 5, name = "Bassel Al-Assad", side = 1, category = 0,
         x = -8000, alt = 30, z = 45000},
    },

    mission = {
        theatre = "Syria",
        date = {Year = 2024, Month = 9, Day = 22},
        start_time = 43200,
    },
}

local function vec3(x, y, z)
    return {x = x, y = y, z = z}
end

-- ------------------------------------------------------------------
-- Objects
-- ------------------------------------------------------------------

local function copy_ammo(list)
    if list == nil then return nil end
    local out = {}
    for i = 1, #list do
        local e = list[i]
        local desc = {}
        for k, v in pairs(e.desc or {}) do desc[k] = v end
        out[i] = {count = e.count, desc = desc}
    end
    return out
end

local function make_unit(group, udata, side)
    local u = {}
    u.__name = udata.name
    u.__type = udata.type
    u.__group = group
    u.__exists = true
    u.__side = side
    u.__player = nil
    u.__pos = vec3(udata.x or 0, udata.alt or 0, udata.y or 0)
    u.__velocity = vec3(0, 0, 0)
    u.__ammo = copy_ammo(M.ammo_by_type[udata.type])

    function u:getName() return self.__name end
    function u:getTypeName() return self.__type end
    function u:isExist() return self.__exists end
    function u:getCoalition() return self.__side end
    function u:getPlayerName() return self.__player end
    function u:getGroup()
        -- A dead unit's group handle is exactly the sort of thing that
        -- raises in DCS rather than returning nil.
        if not self.__exists then error("unit " .. self.__name .. " is gone", 0) end
        return self.__group
    end
    function u:getPoint()
        if not self.__exists then error("unit " .. self.__name .. " is gone", 0) end
        return vec3(self.__pos.x, self.__pos.y, self.__pos.z)
    end
    function u:getPosition()
        return {p = self:getPoint()}
    end
    function u:getVelocity()
        if not self.__exists then error("unit " .. self.__name .. " is gone", 0) end
        return vec3(self.__velocity.x, self.__velocity.y, self.__velocity.z)
    end
    function u:destroy() M.kill_unit(self.__name) end
    if not M.no_get_ammo then
        function u:getAmmo()
            if not self.__exists then error("unit " .. self.__name .. " is gone", 0) end
            if M.ammo_raises[self.__name] then
                error("getAmmo: injected failure on " .. self.__name, 0)
            end
            -- A fresh table every call, as DCS hands one back.
            return copy_ammo(self.__ammo)
        end
    end

    return u
end

local function make_group(name, data, category, side)
    local g = {}
    g.__name = name
    g.__id = M.next_group_id
    M.next_group_id = M.next_group_id + 1
    g.__exists = true
    g.__category = category
    g.__side = side
    --- The table the client handed addGroup, verbatim. This is what a test
    --- reads to find out what was tasked.
    g.__data = data
    g.__units = {}

    function g:getName() return self.__name end
    function g:getID() return self.__id end
    function g:isExist() return self.__exists end
    function g:getCategory() return self.__category end
    function g:getCoalition() return self.__side end
    function g:getUnits()
        if not self.__exists then error("group " .. self.__name .. " is gone", 0) end
        -- Every unit ever in the group, dead ones included: DCS's unit list
        -- lags a death by a frame, and the client is written to count with
        -- isExist rather than trust the list length.
        local out = {}
        for i = 1, #self.__units do out[i] = self.__units[i] end
        return out
    end
    function g:getSize()
        local n = 0
        for i = 1, #self.__units do
            if self.__units[i].__exists then n = n + 1 end
        end
        return n
    end
    function g:destroy() M.kill_group(self.__name) end

    return g
end

local function make_static(name, data, side)
    local s = {}
    s.__name = name
    s.__type = data.type
    s.__exists = true
    s.__side = side
    s.__data = data
    s.__pos = vec3(data.x or 0, 0, data.y or 0)

    function s:getName() return self.__name end
    function s:getTypeName() return self.__type end
    function s:isExist() return self.__exists end
    function s:getCoalition() return self.__side end
    function s:getPoint()
        if not self.__exists then error("static " .. self.__name .. " is gone", 0) end
        return vec3(self.__pos.x, self.__pos.y, self.__pos.z)
    end
    function s:getLife() return self.__exists and 100 or 0 end
    function s:destroy() M.kill_static(self.__name) end

    return s
end

local function make_airbase(entry)
    local b = {}
    function b:getID() return entry.id end
    function b:getName() return entry.name end
    function b:getCoalition() return entry.side end
    function b:getDesc() return {category = entry.category} end
    function b:getPoint() return vec3(entry.x, entry.alt, entry.z) end
    return b
end

-- ------------------------------------------------------------------
-- World mutation, for the harness
-- ------------------------------------------------------------------

function M.kill_unit(name)
    local u = M.units[name]
    if not u or not u.__exists then return false end
    u.__exists = false
    -- DCS stops resolving a destroyed unit by name. The handle a caller
    -- already holds survives, and goes stale.
    M.units[name] = nil
    local group = u.__group
    if group then
        local any = false
        for i = 1, #group.__units do
            if group.__units[i].__exists then any = true end
        end
        if not any then
            -- Last unit gone: DCS stops resolving the group by name. The
            -- handle stays, and stale.
            group.__exists = false
            M.groups[group.__name] = nil
        end
    end
    return true
end

function M.kill_group(name)
    local g = M.groups[name]
    if not g then return false end
    for i = 1, #g.__units do
        g.__units[i].__exists = false
        M.units[g.__units[i].__name] = nil
    end
    g.__exists = false
    M.groups[name] = nil
    return true
end

function M.kill_static(name)
    local s = M.statics[name]
    if not s then return false end
    s.__exists = false
    M.statics[name] = nil
    return true
end

--- Put a human in a jet. Not engine-owned: players are the client's
--- observer source, and the name deliberately carries no cmp_ prefix.
function M.add_player(unit_name, player_name, side, x, alt, z)
    local udata = {name = unit_name, type = "F/A-18C_hornet", x = x, y = z, alt = alt}
    local u = make_unit(nil, udata, side)
    u.__player = player_name
    M.units[unit_name] = u
    return u
end

--- Take `n` rounds of `type_name` off a unit, dropping the entry when it
--- runs out, as DCS lists only what is aboard. Returns how many were taken.
function M.fire_weapon(unit_name, type_name, n)
    local u = M.units[unit_name]
    if not u or not u.__ammo then return 0 end
    for i = 1, #u.__ammo do
        local e = u.__ammo[i]
        if e.desc and e.desc.typeName == type_name then
            local take = math.min(n, e.count)
            e.count = e.count - take
            if e.count == 0 then table.remove(u.__ammo, i) end
            if #u.__ammo == 0 then u.__ammo = nil end
            return take
        end
    end
    return 0
end

function M.move_unit(unit_name, x, alt, z)
    local u = M.units[unit_name]
    if not u then return false end
    u.__pos = vec3(x, alt, z)
    return true
end

function M.fire_event(e)
    for i = 1, #M.handlers do
        local h = M.handlers[i]
        if h and h.onEvent then h:onEvent(e) end
    end
end

-- ------------------------------------------------------------------
-- Scheduler
-- ------------------------------------------------------------------

--- Set the clock and run whatever that makes due. One call per sim step.
function M.advance(dt)
    M.time = M.time + dt
    M.run_due()
    return M.time
end

function M.run_due()
    local ids = {}
    for id in pairs(M.sched) do ids[#ids + 1] = id end
    table.sort(ids)
    for i = 1, #ids do
        local entry = M.sched[ids[i]]
        if entry and M.time >= entry.at then
            local ok, result = pcall(entry.fn, entry.arg, M.time)
            if not ok then
                -- DCS removes a scheduled function that raises. So do we,
                -- and the harness surfaces it rather than swallowing it.
                M.sched_errors[#M.sched_errors + 1] = tostring(result)
                M.sched[ids[i]] = nil
            elseif type(result) == "number" then
                entry.at = result
            else
                M.sched[ids[i]] = nil
            end
        end
    end
end

function M.pending_schedules()
    local n = 0
    for _ in pairs(M.sched) do n = n + 1 end
    return n
end

-- ------------------------------------------------------------------
-- Globals
-- ------------------------------------------------------------------

local function record_log(level)
    return function(msg)
        M.logs[#M.logs + 1] = {level = level, msg = tostring(msg)}
    end
end

_G.env = {
    info = record_log("info"),
    warning = record_log("warning"),
    error = record_log("error"),
    setErrorMessageBoxEnabled = function() end,
    mission = M.mission,
}

_G.timer = {
    getTime = function() return M.time end,
    getAbsTime = function() return M.time + M.mission.start_time end,
    scheduleFunction = function(fn, arg, at)
        M.next_sched_id = M.next_sched_id + 1
        M.sched[M.next_sched_id] = {fn = fn, arg = arg, at = at or M.time}
        return M.next_sched_id
    end,
    removeFunction = function(id)
        if id == nil or M.sched[id] == nil then
            -- DCS raises on an id it does not know.
            error("timer.removeFunction: unknown id " .. tostring(id), 0)
        end
        M.sched[id] = nil
    end,
}

_G.country = {
    id = {
        RUSSIA = 0,
        USA = 2,
        SWITZERLAND = 47,
        TURKEY = 44,
        SYRIA = 40,
    },
}

_G.Group = {
    Category = {AIRPLANE = 0, HELICOPTER = 1, GROUND = 2, SHIP = 3, TRAIN = 4},
    getByName = function(name)
        return M.groups[name]
    end,
}

_G.Unit = {
    Category = {AIRPLANE = 0, HELICOPTER = 1, GROUND_UNIT = 2, SHIP = 3, STRUCTURE = 4},
    getByName = function(name)
        return M.units[name]
    end,
}

_G.StaticObject = {
    Category = {VOID = 0, UNIT = 1, WEAPON = 2, STATIC = 3, BASE = 4, SCENERY = 5},
    getByName = function(name)
        return M.statics[name]
    end,
}

_G.Object = {
    Category = {UNIT = 1, WEAPON = 2, STATIC = 3, BASE = 4, SCENERY = 5},
}

_G.Airbase = {
    Category = {AIRDROME = 0, HELIPAD = 1, SHIP = 2},
}

local COUNTRY_SIDE = {
    [0] = 1,    -- RUSSIA -> red
    [2] = 2,    -- USA    -> blue
    [47] = 0,   -- SWITZERLAND -> neutral
}

_G.coalition = {
    side = {NEUTRAL = 0, RED = 1, BLUE = 2},

    addGroup = function(country_id, category, data)
        M.spawn_calls[#M.spawn_calls + 1] = {
            kind = "group",
            country = country_id,
            category = category,
            name = type(data) == "table" and data.name or nil,
            data = data,
        }
        if M.fail_add_group then
            if M.fail_add_group_silently then return nil end
            error("addGroup: injected failure", 0)
        end
        if type(data) ~= "table" or type(data.name) ~= "string" then
            error("addGroup: malformed group data", 0)
        end
        if type(data.units) ~= "table" or #data.units == 0 then
            error("addGroup: group has no units", 0)
        end
        local side = COUNTRY_SIDE[country_id] or 0
        local g = make_group(data.name, data, category, side)
        for i = 1, #data.units do
            local u = make_unit(g, data.units[i], side)
            g.__units[i] = u
            M.units[u.__name] = u
        end
        M.groups[data.name] = g
        return g
    end,

    addStaticObject = function(country_id, data)
        M.spawn_calls[#M.spawn_calls + 1] = {
            kind = "static",
            country = country_id,
            name = type(data) == "table" and data.name or nil,
            data = data,
        }
        if M.fail_add_static
           or (M.fail_static_named ~= nil
               and type(data) == "table"
               and data.name == M.fail_static_named) then
            if M.fail_add_static_silently then return nil end
            error("addStaticObject: injected failure", 0)
        end
        if type(data) ~= "table" or type(data.name) ~= "string" then
            error("addStaticObject: malformed static data", 0)
        end
        local side = COUNTRY_SIDE[country_id] or 0
        local s = make_static(data.name, data, side)
        M.statics[data.name] = s
        return s
    end,

    getAirbases = function(side)
        local out = {}
        for i = 1, #M.airbases do
            if M.airbases[i].side == side then
                out[#out + 1] = make_airbase(M.airbases[i])
            end
        end
        return out
    end,

    getPlayers = function(side)
        local out = {}
        local names = {}
        for name, u in pairs(M.units) do
            if u.__exists and u.__player and u.__side == side then
                names[#names + 1] = name
            end
        end
        table.sort(names)
        for i = 1, #names do out[i] = M.units[names[i]] end
        return out
    end,
}

_G.world = {
    event = {
        S_EVENT_INVALID = 0,
        S_EVENT_SHOT = 1,
        S_EVENT_HIT = 2,
        S_EVENT_TAKEOFF = 3,
        S_EVENT_LAND = 4,
        S_EVENT_CRASH = 5,
        S_EVENT_EJECTION = 6,
        S_EVENT_REFUELING = 7,
        S_EVENT_DEAD = 8,
        S_EVENT_PILOT_DEAD = 9,
        S_EVENT_BASE_CAPTURED = 10,
        S_EVENT_MISSION_START = 11,
        S_EVENT_MISSION_END = 12,
        S_EVENT_TOOK_CONTROL = 13,
        S_EVENT_REFUELING_STOP = 14,
        S_EVENT_BIRTH = 15,
        S_EVENT_HUMAN_FAILURE = 16,
        S_EVENT_ENGINE_STARTUP = 18,
        S_EVENT_ENGINE_SHUTDOWN = 19,
        S_EVENT_PLAYER_ENTER_UNIT = 20,
        S_EVENT_PLAYER_LEAVE_UNIT = 21,
        S_EVENT_SHOOTING_START = 23,
        S_EVENT_SHOOTING_END = 24,
        S_EVENT_KILL = 28,
        S_EVENT_SCORE = 29,
        S_EVENT_UNIT_LOST = 31,
        S_EVENT_MAX = 42,
    },
    addEventHandler = function(h)
        M.handlers[#M.handlers + 1] = h
    end,
    removeEventHandler = function(h)
        for i = 1, #M.handlers do
            if M.handlers[i] == h then table.remove(M.handlers, i) return end
        end
    end,
    getPlayer = function() return nil end,
}

local SIDE_NAME = {[0] = "neutral", [1] = "red", [2] = "blue"}

_G.trigger = {
    action = {
        outText = function(text, duration, clearview)
            M.messages[#M.messages + 1] =
                {to = "all", text = text, duration = duration}
        end,
        outTextForCoalition = function(side, text, duration, clearview)
            M.messages[#M.messages + 1] =
                {to = SIDE_NAME[side] or tostring(side), text = text,
                 duration = duration}
        end,
        outTextForGroup = function(gid, text, duration, clearview)
            M.messages[#M.messages + 1] =
                {to = "group:" .. tostring(gid), text = text, duration = duration}
        end,
    },
}

return M
