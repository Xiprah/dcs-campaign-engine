--[[----------------------------------------------------------------------
  mission/validate_templates.lua -- ask DCS which of the campaign client's
  content strings it actually accepts.

  Drop this into any mission, fly nothing, and read the log. It spawns every
  template mission/campaign_client.lua names, one at a time, checks whether
  the object it asked for really exists afterwards, destroys it again, and
  writes one greppable line per case.

  WHY IT EXISTS

  campaign_client.lua names a lot of DCS *content* that nothing can check
  from outside a DCS install: the unit type "F-16C_50", the SA-6 battery's
  "Kub 1S91 str" / "Kub 2P25 ln" and its "Ground Nothing" task, the static
  type/category pair ("Tank" / "Fortifications"), three country ids, the
  Bombing and AttackGroup task schemas, the waypoint action and alt_type
  strings, and a payload whose pylon table is empty. The offline suite is
  green whether or not DCS would accept any of it, because the mock accepts
  any well-formed table. This script is the other half: it makes DCS answer,
  for all of them, in one run instead of one crash at a time.

  Two failure modes it is built around:

  * DCS accepting a table and DCS creating a usable object are different
    things. `coalition.addGroup` fails *silently* on content it dislikes --
    it logs internally and returns without raising. So every case here also
    asks getByName afterwards, and reports ACCEPTED-BUT-ORPHANED separately
    from ACCEPTED.
  * An unknown weapon CLSID does not raise either. It yields an empty pylon,
    which is exactly how a strike package ends up unarmed with nobody
    noticing. The pylon probes below are therefore about ammunition that is
    actually aboard the jet, not about whether the spawn succeeded.

  WHERE THE VALUES COME FROM

  The client keeps its content in locals. Only `TEMPLATES` is exported
  (`CampaignClient.TEMPLATES`); the country map, the group task strings and
  the waypoint action table are not reachable from outside the file at all.
  So:

  * when the client is loaded in the same mission, SPEC.templates is
    CROSS-CHECKED against the live `CampaignClient.TEMPLATES`, field by
    field, and any difference is reported as a DRIFT failure -- see
    `cross_check`. tests/test_validation.py asserts the same thing offline,
    so drift breaks CI rather than quietly invalidating a run;
  * the rest is mirrored below under a single SPEC table, with the refactor
    that would remove the mirror written up in mission/VALIDATION.md.

  This file NEVER requires campaign_client.lua and never needs the engine:
  finding the client is an optional cross-check, not a dependency.

  DISCIPLINE

  Lua 5.1 only -- no goto, no bitwise operators, no 5.3 library calls.
  Everything runs on the simulation thread, so work is spread one case per
  timer.scheduleFunction tick, every DCS call sits inside pcall, and nothing
  loops over the world. Nothing reads a wall clock or an unseeded random
  source: case order and spawn positions are fixed.
------------------------------------------------------------------------]]

local V = {}

V.VERSION = 1

--- One prefix on every line this file emits. Grep for it in dcs.log.
local PREFIX = "[campaign-validate] "

--- Deliberately not the client's "cmp_" prefix: the running client filters
--- events by that prefix, and nothing here is engine-owned.
local NAME_PREFIX = "cmpval_"

-- ------------------------------------------------------------------
-- Configuration. Define CAMPAIGN_VALIDATE_CONFIG before loading.
-- ------------------------------------------------------------------

local CONFIG = {
    --- Seconds between ticks. Each case takes two ticks: attempt, then
    --- re-check and clean up.
    tick = 0.1,

    --- Cases started per tick. One is the honest default on the sim thread.
    cases_per_tick = 1,

    --- Air-start altitude for probe flights, in metres.
    altitude = 5000,

    --- Metres between one case's spawn point and the next, so ground
    --- objects never overlap.
    spacing = 250,

    --- {x = <map x>, y = <map y>}. Leave nil to derive one -- see
    --- `pick_origin`. Statics need LAND: a sea origin rejects every static
    --- case and the report would blame the template.
    origin = nil,

    --- Where the JSON report goes. nil derives one from lfs.writedir().
    out_path = nil,

    --- Start as soon as the file loads, as the client does.
    auto_start = true,

    --- Weapon CLSIDs to probe. Replace these with values copied out of a
    --- mission-editor group export; the defaults are candidates, not
    --- knowledge. See mission/VALIDATION.md.
    clsids = nil,

    --- DCS airdrome id the ramp-start case parks on. Leave nil to pick one --
    --- see `pick_airdrome`. Ids are per map, so there is no safe default.
    airdrome_id = nil,
}

if type(_G.CAMPAIGN_VALIDATE_CONFIG) == "table" then
    for k, v in pairs(_G.CAMPAIGN_VALIDATE_CONFIG) do CONFIG[k] = v end
end

-- ------------------------------------------------------------------
-- The declarations under test
--
-- SPEC.templates mirrors campaign_client.lua's TEMPLATES and is verified
-- against the live table by `cross_check`. Everything else is a local in
-- the client and cannot be read from here at all.
-- ------------------------------------------------------------------

local DEFAULT_PAYLOAD = {
    pylons = {},
    fuel = 3249,
    flare = 60,
    chaff = 60,
    gun = 100,
}

local SU24M_PAYLOAD = {
    pylons = {},
    fuel = 11700,
    flare = 60,
    chaff = 60,
    gun = 100,
}

local SPEC = {
    templates = {
        ["F-16C_strike_jdam"] = {
            unit_type = "F-16C_50",
            count = 2,
            task = "Ground Attack",
            skill = "High",
            payload = DEFAULT_PAYLOAD,
            probe_category = "plane",
        },
        ["F-16C_cap"] = {
            unit_type = "F-16C_50",
            count = 2,
            task = "CAP",
            skill = "High",
            payload = DEFAULT_PAYLOAD,
            probe_category = "plane",
        },
        ["fuel_depot_medium"] = {
            static = true,
            unit_type = "Tank",
            static_category = "Fortifications",
            count = 4,
            spread = 60,
        },
        -- A red ground group: probed as the client builds it, for RUSSIA,
        -- on the ground, with no payload, radar first.
        ["SA-6_Kub_site"] = {
            lead_type = "Kub 1S91 str",
            unit_type = "Kub 2P25 ln",
            count = 5,
            task = "Ground Nothing",
            skill = "High",
            probe_category = "ground",
            probe_country = "RUSSIA",
        },
        -- Red's strike aircraft, probed for RUSSIA because that is who the
        -- client spawns red for: a type DCS refuses to a country is a
        -- rejection no USA probe would ever see.
        ["Su-24M_strike_fab"] = {
            unit_type = "Su-24M",
            count = 2,
            task = "Ground Attack",
            skill = "High",
            payload = SU24M_PAYLOAD,
            probe_category = "plane",
            probe_country = "RUSSIA",
        },
        -- Blue's strategic target, a static probed for USA.
        ["munitions_storage_medium"] = {
            static = true,
            unit_type = "Warehouse",
            static_category = "Warehouses",
            count = 4,
            spread = 60,
            probe_country = "USA",
        },
        -- A blue ground group: probed for USA, on the ground, radar first.
        ["Patriot_site"] = {
            lead_type = "Patriot str",
            unit_type = "Patriot ln",
            count = 5,
            task = "Ground Nothing",
            skill = "High",
            probe_category = "ground",
            probe_country = "USA",
        },
    },

    --- Iterated in this order so a run is reproducible.
    template_order = {"F-16C_strike_jdam", "F-16C_cap", "fuel_depot_medium",
                      "SA-6_Kub_site", "Su-24M_strike_fab",
                      "munitions_storage_medium", "Patriot_site"},

    --- campaign_client.lua: dcs_maps().country
    country = {
        {wire = "blue", id = "USA"},
        {wire = "red", id = "RUSSIA"},
        {wire = "neutral", id = "SWITZERLAND"},
    },

    --- campaign_client.lua: dcs_maps().category
    category = {
        plane = "AIRPLANE",
        helicopter = "HELICOPTER",
        ground = "GROUND",
        ship = "SHIP",
    },

    --- campaign_client.lua: WAYPOINT_ACTIONS
    waypoint_actions = {
        {wire = "turning_point", type = "Turning Point", action = "Turning Point"},
        {wire = "fly_over_point", type = "Turning Point", action = "Fly Over Point"},
        {wire = "landing", type = "Land", action = "Landing"},
    },

    --- campaign_client.lua: RAMP_START, which build_route puts on waypoint 1
    --- when the engine sends an airdrome_id with it.
    ramp_start = {wire = "ramp_start", type = "TakeOffParking",
                  action = "From Parking Area"},

    --- campaign_client.lua: build_route / build_group_data
    alt_types = {"BARO", "RADIO"},

    --- Group-level task strings the client can put on a group table.
    group_tasks = {"Ground Attack", "CAP", "Nothing"},

    --- Static type/category pairs. The first is the client's; the rest are
    --- alternatives to fall back to when it turns out to be rejected.
    static_pairs = {
        {type = "Tank", category = "Fortifications", client = true},
        {type = "Tank", category = "Warehouses"},
        {type = "Fuel tank", category = "Fortifications"},
        {type = "Workshop A", category = "Fortifications"},
    },
}

--- CLSIDs to probe, in order. `expect = "empty"` marks a control: a case
--- whose pylon MUST come back empty, because if a CLSID nobody has ever
--- heard of reports ammunition then the probe cannot tell loaded from
--- unarmed and none of the other pylon lines mean anything.
local DEFAULT_CLSIDS = {
    {clsid = "{CAMPAIGN_VALIDATOR_NOT_A_REAL_CLSID}", pylon = 3,
     label = "control: a CLSID that cannot exist", expect = "empty"},
    {clsid = "{GBU-38}", pylon = 3, label = "GBU-38 JDAM (candidate)"},
    {clsid = "{GBU-31}", pylon = 3, label = "GBU-31 JDAM (candidate)"},
    {clsid = "{GBU-12}", pylon = 3, label = "GBU-12 (candidate)"},
    {clsid = "{Mk-82}", pylon = 3, label = "Mk-82 (candidate)"},
}

-- ------------------------------------------------------------------
-- Small helpers
-- ------------------------------------------------------------------

--- pcall an accessor and return nil rather than propagating. DCS handles go
--- stale and a stale handle raises instead of returning nil.
local function try(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, v = pcall(fn, ...)
    if ok then return v end
    return nil
end

local function log_line(text)
    if env and env.info then
        pcall(env.info, PREFIX .. text)
    end
end

local function log_err_line(text)
    if env and env.error then
        pcall(env.error, PREFIX .. text)
    end
end

local function trim(s, limit)
    s = tostring(s)
    -- One line, always: a multi-line DCS error would break the log format.
    s = string.gsub(s, "[\r\n\t]+", " ")
    s = string.gsub(s, '"', "'")
    if #s > (limit or 200) then s = string.sub(s, 1, limit or 200) .. "..." end
    return s
end

local function sorted_keys(t)
    local keys = {}
    for k in pairs(t) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    return keys
end

-- ------------------------------------------------------------------
-- A very small JSON writer
--
-- Self-contained on purpose: this file must load into a mission that has
-- never heard of mission/json.lua. Only the value shapes built below are
-- supported, and object keys are sorted so two runs produce the same bytes.
-- ------------------------------------------------------------------

local JSON_ESCAPES = {
    ['"'] = '\\"', ["\\"] = "\\\\", ["\b"] = "\\b", ["\f"] = "\\f",
    ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t",
}

local function json_string(s)
    local out = string.gsub(s, '[%c"\\]', function(c)
        return JSON_ESCAPES[c] or string.format("\\u%04x", string.byte(c))
    end)
    return '"' .. out .. '"'
end

local encode_value

local function json_number(v)
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if v == math.floor(v) and math.abs(v) < 1e15 then
        return string.format("%d", v)
    end
    return string.format("%.6f", v)
end

encode_value = function(v, out)
    local t = type(v)
    if v == nil then
        out[#out + 1] = "null"
    elseif t == "boolean" then
        out[#out + 1] = v and "true" or "false"
    elseif t == "number" then
        out[#out + 1] = json_number(v)
    elseif t == "string" then
        out[#out + 1] = json_string(v)
    elseif t == "table" then
        if v.__array then
            out[#out + 1] = "["
            for i = 1, v.n or #v do
                if i > 1 then out[#out + 1] = "," end
                encode_value(v[i], out)
            end
            out[#out + 1] = "]"
        else
            out[#out + 1] = "{"
            local keys = sorted_keys(v)
            local first = true
            for i = 1, #keys do
                local k = keys[i]
                if k ~= "__array" and k ~= "n" then
                    if not first then out[#out + 1] = "," end
                    first = false
                    out[#out + 1] = json_string(k)
                    out[#out + 1] = ":"
                    encode_value(v[k], out)
                end
            end
            out[#out + 1] = "}"
        end
    else
        out[#out + 1] = json_string(tostring(v))
    end
end

local function json_encode(v)
    local out = {}
    encode_value(v, out)
    return table.concat(out)
end

local function array(t)
    t = t or {}
    t.__array = true
    return t
end

-- ------------------------------------------------------------------
-- Run state
-- ------------------------------------------------------------------

local RUN = {
    started = false,
    finished = false,
    sched_id = nil,
    index = 1,
    cases = {},
    pending = nil,
    results = {},
    origin = {x = 0, y = 0},
    origin_source = "default",
    out_path = nil,
    out_written = false,
    out_error = nil,
    template_source = "mirror",
    counts = {},
}

local STATUSES = {"OK", "REJECTED", "ORPHAN", "DRIFT", "UNKNOWN", "SKIP", "ERROR"}

local function record(rec)
    rec.detail = rec.detail or {}
    RUN.results[#RUN.results + 1] = rec
    RUN.counts[rec.status] = (RUN.counts[rec.status] or 0) + 1

    local parts = {
        "RESULT",
        "status=" .. rec.status,
        "required=" .. (rec.required and "1" or "0"),
        "id=" .. rec.id,
        "kind=" .. rec.kind,
    }
    local keys = sorted_keys(rec.detail)
    for i = 1, #keys do
        local value = rec.detail[keys[i]]
        if type(value) == "boolean" then value = value and "1" or "0" end
        parts[#parts + 1] = keys[i] .. '="' .. trim(value) .. '"'
    end
    parts[#parts + 1] = 'label="' .. trim(rec.label) .. '"'
    parts[#parts + 1] = 'error="' .. trim(rec.error or "") .. '"'

    local line = table.concat(parts, " ")
    if rec.status == "REJECTED" or rec.status == "ORPHAN"
       or rec.status == "DRIFT" or rec.status == "ERROR" then
        log_err_line(line)
    else
        log_line(line)
    end
end

-- ------------------------------------------------------------------
-- Building the tables under test
--
-- These mirror campaign_client.lua's build_route / build_group_data /
-- build_static_data / bombing_task. Shape, not content: the content comes
-- from SPEC above.
-- ------------------------------------------------------------------

local function waypoint(x, y, alt, wp_type, wp_action, alt_type)
    return {
        x = x,
        y = y,
        alt = alt,
        alt_type = alt_type or "BARO",
        type = wp_type or "Turning Point",
        action = wp_action or "Turning Point",
        speed = 200,
        speed_locked = true,
        ETA = 0,
        ETA_locked = false,
        task = {id = "ComboTask", params = {tasks = {}}},
    }
end

--- opts: name, unit_type, lead_type, count, task, skill, payload, ground,
---       x, y, alt, wp_type, wp_action, alt_type, tasks, airdrome_id
---
--- `ground` mirrors the client's AIRBORNE test: a ground group's units
--- carry no payload at all.
local function build_group_data(opts)
    local units = {}
    local payload = nil
    if not opts.ground then payload = opts.payload or DEFAULT_PAYLOAD end
    for i = 1, (opts.count or 1) do
        units[i] = {
            name = opts.name .. "_" .. i,
            type = (i == 1 and opts.lead_type) or opts.unit_type,
            x = opts.x + (i - 1) * 50,
            y = opts.y + (i - 1) * 50,
            alt = opts.alt,
            alt_type = opts.alt_type or "BARO",
            heading = 0,
            speed = 200,
            skill = opts.skill or "High",
            payload = payload,
        }
    end

    local point = waypoint(opts.x, opts.y, opts.alt,
                           opts.wp_type, opts.wp_action, opts.alt_type)
    point.airdromeId = opts.airdrome_id
    if opts.tasks then
        for i = 1, #opts.tasks do
            point.task.params.tasks[i] = opts.tasks[i]
        end
    end

    return {
        name = opts.name,
        task = opts.task or "Nothing",
        visible = false,
        uncontrolled = false,
        hidden = false,
        x = opts.x,
        y = opts.y,
        route = {points = {point}},
        units = units,
    }
end

local function build_static_data(opts)
    return {
        name = opts.name,
        type = opts.unit_type,
        category = opts.static_category,
        x = opts.x,
        y = opts.y,
        heading = 0,
        dead = false,
    }
end

--- campaign_client.lua: bombing_task
local function bombing_task(x, y)
    return {
        number = 1,
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

--- campaign_client.lua: attach_tasking, the AttackGroup branch
local function attack_group_task(group_id)
    return {
        number = 1,
        auto = false,
        id = "AttackGroup",
        enabled = true,
        params = {groupId = group_id, expend = "All", groupAttack = true},
    }
end

-- ------------------------------------------------------------------
-- Spawn / retrieve / destroy, each wrapped so a failure is data
-- ------------------------------------------------------------------

local function country_id(name)
    if not country or not country.id then return nil end
    return country.id[name]
end

local function group_category(name)
    if not Group or not Group.Category then return nil end
    local gc = Group.Category
    if name == "GROUND" then return gc.GROUND or gc.GROUND_UNIT end
    return gc[name]
end

--- Returns created (bool), err (string or nil).
local function add_group(cid, category, data)
    local ok, err = pcall(coalition.addGroup, cid, category, data)
    if not ok then return false, tostring(err) end
    return true, nil
end

local function add_static(cid, data)
    local ok, err = pcall(coalition.addStaticObject, cid, data)
    if not ok then return false, tostring(err) end
    return true, nil
end

local function live_group(name)
    local grp = try(Group.getByName, name)
    if grp and try(grp.isExist, grp) then return grp end
    return nil
end

local function live_static(name)
    local obj = try(StaticObject.getByName, name)
    if obj and try(obj.isExist, obj) then return obj end
    return nil
end

local function group_units(name)
    local grp = live_group(name)
    if not grp then return 0 end
    local units = try(grp.getUnits, grp) or {}
    local n = 0
    for i = 1, #units do
        if units[i] and try(units[i].isExist, units[i]) then n = n + 1 end
    end
    return n
end

--- Destroy everything a case created. Returns how many are still there.
local function cleanup(created)
    local leaked = 0
    for i = 1, #created do
        local item = created[i]
        if item.kind == "group" then
            local grp = try(Group.getByName, item.name)
            if grp then pcall(grp.destroy, grp) end
            if live_group(item.name) then leaked = leaked + 1 end
        else
            local obj = try(StaticObject.getByName, item.name)
            if obj then pcall(obj.destroy, obj) end
            if live_static(item.name) then leaked = leaked + 1 end
        end
    end
    return leaked
end

-- ------------------------------------------------------------------
-- The ammunition probe
--
-- An unknown CLSID is not an error. The pylon is simply empty, so the only
-- honest question is what is aboard the jet afterwards -- and the gun does
-- not count, because `gun = 100` in the payload puts shells in every one of
-- these aircraft whether a pylon loaded or not.
-- ------------------------------------------------------------------

--- Weapon desc.category 0 is SHELL, which is the internal gun.
local SHELL = 0

--- Returns n (non-gun munitions), summary (string), err (string or nil).
local function inspect_ammo(unit_name)
    local unit = try(Unit.getByName, unit_name)
    if not unit then return nil, nil, "unit " .. unit_name .. " is not retrievable" end
    if type(unit.getAmmo) ~= "function" then
        return nil, nil, "Unit.getAmmo is unavailable in this environment"
    end
    local ok, ammo = pcall(unit.getAmmo, unit)
    if not ok then return nil, nil, "getAmmo raised: " .. tostring(ammo) end
    if ammo == nil then return 0, "", nil end

    local n = 0
    local names = {}
    for i = 1, #ammo do
        local entry = ammo[i]
        local desc = entry and entry.desc
        local category = desc and desc.category
        if category ~= SHELL then
            n = n + (tonumber(entry.count) or 0)
            local type_name = (desc and desc.typeName) or "?"
            names[#names + 1] = type_name .. "x" .. tostring(entry.count)
        end
    end
    table.sort(names)
    return n, table.concat(names, ","), nil
end

-- ------------------------------------------------------------------
-- Cases
--
-- A case is {id, kind, label, required, attempt}. `attempt` returns a
-- record; if it created anything, the record is finished on the NEXT tick,
-- which is where retrievability is re-checked, ammunition is read and
-- everything is destroyed.
-- ------------------------------------------------------------------

--- The airdrome the ramp-start case parks on, as {id, name, x, y, alt}, or
--- nil and the reason there is none.
---
--- Blue or neutral only, because the probe flies for USA and DCS will not
--- park a flight on an enemy field -- a red one would read as the ramp-start
--- pair being rejected. Lowest id rather than first listed, so the choice
--- does not hang on the order DCS happens to enumerate bases in.
local function pick_airdrome()
    if not (coalition and coalition.getAirbases and coalition.side) then
        return nil, "coalition.getAirbases is unavailable"
    end
    local kind = Airbase and Airbase.Category and Airbase.Category.AIRDROME
    if kind == nil then
        return nil, "Airbase.Category.AIRDROME is unavailable"
    end
    local wanted = tonumber(CONFIG.airdrome_id)
    local best = nil
    local sides = {coalition.side.BLUE, coalition.side.NEUTRAL}
    for s = 1, #sides do
        local bases = try(coalition.getAirbases, sides[s]) or {}
        for i = 1, #bases do
            local base = bases[i]
            local desc = try(base.getDesc, base)
            local id = try(base.getID, base)
            local point = try(base.getPoint, base)
            if desc and desc.category == kind and type(id) == "number" and point
               and (wanted == nil or id == wanted)
               and (best == nil or id < best.id) then
                best = {id = id, name = tostring(try(base.getName, base) or "?"),
                        x = point.x, y = point.z, alt = point.y}
            end
        end
    end
    if best then return best end
    if wanted then
        return nil, "CAMPAIGN_VALIDATE_CONFIG.airdrome_id " .. tostring(wanted)
                    .. " is not a blue or neutral airdrome in this mission"
    end
    return nil, "no blue or neutral airdrome in this mission; set "
                .. "CAMPAIGN_VALIDATE_CONFIG.airdrome_id"
end

local function case_position(index)
    return RUN.origin.x + (index - 1) * CONFIG.spacing, RUN.origin.y
end

local function new_case_record(case)
    return {
        id = case.id,
        kind = case.kind,
        label = case.label,
        required = case.required and true or false,
        status = "UNKNOWN",
        error = nil,
        detail = {},
        created = {},
    }
end

--- The shared body of every group case.
local function try_group(rec, opts, cid, category)
    local created, err = add_group(cid, category, opts.data)
    rec.detail.created = created
    if not created then
        rec.status = "REJECTED"
        rec.error = err
        return rec
    end

    rec.created[#rec.created + 1] = {kind = "group", name = opts.data.name}
    local grp = live_group(opts.data.name)
    local units = group_units(opts.data.name)
    rec.detail.retrievable = grp ~= nil
    rec.detail.units = units
    rec.detail.wanted = #opts.data.units
    if not grp or units == 0 then
        -- The silent failure: DCS took the table and made nothing.
        rec.status = "ORPHAN"
        rec.error = "addGroup returned without raising but Group.getByName "
                    .. "found nothing live"
    else
        rec.status = "OK"
    end
    return rec
end

local function try_static(rec, data, cid)
    local created, err = add_static(cid, data)
    rec.detail.created = created
    if not created then
        rec.status = "REJECTED"
        rec.error = err
        return rec
    end

    rec.created[#rec.created + 1] = {kind = "static", name = data.name}
    local obj = live_static(data.name)
    rec.detail.retrievable = obj ~= nil
    if not obj then
        rec.status = "ORPHAN"
        rec.error = "addStaticObject returned without raising but "
                    .. "StaticObject.getByName found nothing live"
    else
        rec.status = "OK"
    end
    return rec
end

local function build_cases()
    local cases = {}
    local function add(case) cases[#cases + 1] = case end

    -- 1. Every template the client declares, exactly as the client builds it.
    for i = 1, #SPEC.template_order do
        local key = SPEC.template_order[i]
        local tmpl = SPEC.templates[key]
        if tmpl then
            if tmpl.static then
                add({
                    id = "template." .. key,
                    kind = "static",
                    required = true,
                    label = key .. " -> type '" .. tostring(tmpl.unit_type)
                            .. "' category '" .. tostring(tmpl.static_category)
                            .. "' x" .. tostring(tmpl.count),
                    attempt = function(case, index)
                        local rec = new_case_record(case)
                        -- The side the target belongs to: red's depot and
                        -- blue's storage area are both statics, spawned for
                        -- different countries.
                        local country = tmpl.probe_country or "RUSSIA"
                        local cid = country_id(country)
                        if cid == nil then
                            rec.status = "SKIP"
                            rec.error = "country.id." .. country .. " is nil"
                            return rec
                        end
                        local x, y = case_position(index)
                        -- One object only: the template's ring layout is the
                        -- client's arithmetic, not a thing DCS can reject.
                        return try_static(rec, build_static_data({
                            name = NAME_PREFIX .. key,
                            unit_type = tmpl.unit_type,
                            static_category = tmpl.static_category,
                            x = x, y = y,
                        }), cid)
                    end,
                })
            else
                local ground = tmpl.probe_category == "ground"
                local lead = tmpl.lead_type
                              and (tostring(tmpl.lead_type) .. " + ") or ""
                add({
                    id = "template." .. key,
                    kind = "group",
                    required = true,
                    label = key .. " -> " .. lead .. tostring(tmpl.unit_type)
                            .. " x" .. tostring(tmpl.count)
                            .. " task '" .. tostring(tmpl.task)
                            .. "' skill '" .. tostring(tmpl.skill) .. "'",
                    attempt = function(case, index)
                        local rec = new_case_record(case)
                        local cid = country_id(tmpl.probe_country or "USA")
                        local cat = group_category(
                            SPEC.category[tmpl.probe_category or "plane"])
                        if cid == nil or cat == nil then
                            rec.status = "SKIP"
                            rec.error = "country or Group.Category unavailable"
                            return rec
                        end
                        local x, y = case_position(index)
                        -- Ground groups sit on the ground at the case point,
                        -- which is why the origin has to be land.
                        return try_group(rec, {data = build_group_data({
                            name = NAME_PREFIX .. key,
                            unit_type = tmpl.unit_type,
                            lead_type = tmpl.lead_type,
                            count = tmpl.count,
                            task = tmpl.task,
                            skill = tmpl.skill,
                            payload = tmpl.payload,
                            ground = ground,
                            x = x, y = y,
                            alt = ground and 0 or CONFIG.altitude,
                        })}, cid, cat)
                    end,
                })
            end
        end
    end

    -- 2. The three country ids the client maps coalitions onto.
    for i = 1, #SPEC.country do
        local entry = SPEC.country[i]
        add({
            id = "country." .. entry.id,
            kind = "group",
            required = true,
            label = "coalition '" .. entry.wire .. "' -> country.id."
                    .. entry.id,
            attempt = function(case, index)
                local rec = new_case_record(case)
                local cid = country_id(entry.id)
                rec.detail.country_id = cid
                if cid == nil then
                    rec.status = "REJECTED"
                    rec.error = "country.id." .. entry.id
                                .. " does not exist in this DCS build"
                    return rec
                end
                local cat = group_category("AIRPLANE")
                if cat == nil then
                    rec.status = "SKIP"
                    rec.error = "Group.Category.AIRPLANE unavailable"
                    return rec
                end
                local x, y = case_position(index)
                return try_group(rec, {data = build_group_data({
                    name = NAME_PREFIX .. "country_" .. entry.id,
                    unit_type = SPEC.templates["F-16C_cap"].unit_type,
                    count = 1,
                    task = "Nothing",
                    x = x, y = y, alt = CONFIG.altitude,
                })}, cid, cat)
            end,
        })
    end

    -- 3. Waypoint type/action pairs and alt_type strings.
    for i = 1, #SPEC.waypoint_actions do
        local wp = SPEC.waypoint_actions[i]
        add({
            id = "waypoint." .. wp.wire,
            kind = "group",
            required = true,
            label = "route point type '" .. wp.type .. "' action '"
                    .. wp.action .. "'",
            attempt = function(case, index)
                local rec = new_case_record(case)
                local cid, cat = country_id("USA"), group_category("AIRPLANE")
                if cid == nil or cat == nil then
                    rec.status = "SKIP"
                    rec.error = "country or Group.Category unavailable"
                    return rec
                end
                local x, y = case_position(index)
                return try_group(rec, {data = build_group_data({
                    name = NAME_PREFIX .. "wp_" .. wp.wire,
                    unit_type = SPEC.templates["F-16C_cap"].unit_type,
                    count = 1,
                    task = "Nothing",
                    wp_type = wp.type,
                    wp_action = wp.action,
                    x = x, y = y, alt = CONFIG.altitude,
                })}, cid, cat)
            end,
        })
    end

    -- The ramp start the client builds when waypoint 1 names an airdrome.
    -- Placed on the airfield rather than at the case origin: the pair only
    -- means anything with a real airdromeId, and those are per map.
    local ramp = SPEC.ramp_start
    add({
        id = "waypoint." .. ramp.wire,
        kind = "group",
        required = true,
        label = "first route point type '" .. ramp.type .. "' action '"
                .. ramp.action .. "' with an airdromeId",
        attempt = function(case, index)
            local rec = new_case_record(case)
            local cid, cat = country_id("USA"), group_category("AIRPLANE")
            if cid == nil or cat == nil then
                rec.status = "SKIP"
                rec.error = "country or Group.Category unavailable"
                return rec
            end
            local base, why = pick_airdrome()
            if not base then
                rec.status = "SKIP"
                rec.error = why
                return rec
            end
            rec.detail.airdrome_id = base.id
            rec.detail.airdrome = base.name
            return try_group(rec, {data = build_group_data({
                name = NAME_PREFIX .. "wp_" .. ramp.wire,
                unit_type = SPEC.templates["F-16C_cap"].unit_type,
                count = 1,
                task = "Nothing",
                wp_type = ramp.type,
                wp_action = ramp.action,
                airdrome_id = base.id,
                x = base.x, y = base.y, alt = base.alt,
            })}, cid, cat)
        end,
    })

    for i = 1, #SPEC.alt_types do
        local alt_type = SPEC.alt_types[i]
        add({
            id = "alt_type." .. alt_type,
            kind = "group",
            -- Only BARO is in the client; RADIO is the alternative.
            required = alt_type == "BARO",
            label = "waypoint and unit alt_type '" .. alt_type .. "'",
            attempt = function(case, index)
                local rec = new_case_record(case)
                local cid, cat = country_id("USA"), group_category("AIRPLANE")
                if cid == nil or cat == nil then
                    rec.status = "SKIP"
                    rec.error = "country or Group.Category unavailable"
                    return rec
                end
                local x, y = case_position(index)
                return try_group(rec, {data = build_group_data({
                    name = NAME_PREFIX .. "alt_" .. alt_type,
                    unit_type = SPEC.templates["F-16C_cap"].unit_type,
                    count = 1,
                    task = "Nothing",
                    alt_type = alt_type,
                    x = x, y = y, alt = CONFIG.altitude,
                })}, cid, cat)
            end,
        })
    end

    -- 4. Group-level task strings.
    for i = 1, #SPEC.group_tasks do
        local task = SPEC.group_tasks[i]
        add({
            id = "grouptask." .. string.gsub(task, "%s", "_"),
            kind = "group",
            required = true,
            label = "group table task = '" .. task .. "'",
            attempt = function(case, index)
                local rec = new_case_record(case)
                local cid, cat = country_id("USA"), group_category("AIRPLANE")
                if cid == nil or cat == nil then
                    rec.status = "SKIP"
                    rec.error = "country or Group.Category unavailable"
                    return rec
                end
                local x, y = case_position(index)
                return try_group(rec, {data = build_group_data({
                    name = NAME_PREFIX .. "task_" .. string.gsub(task, "%s", "_"),
                    unit_type = SPEC.templates["F-16C_cap"].unit_type,
                    count = 1,
                    task = task,
                    x = x, y = y, alt = CONFIG.altitude,
                })}, cid, cat)
            end,
        })
    end

    -- 5. The two waypoint tasks the client composes.
    add({
        id = "task.Bombing",
        kind = "group",
        required = true,
        label = "ComboTask carrying a Bombing task at a map point",
        attempt = function(case, index)
            local rec = new_case_record(case)
            local cid, cat = country_id("USA"), group_category("AIRPLANE")
            if cid == nil or cat == nil then
                rec.status = "SKIP"
                rec.error = "country or Group.Category unavailable"
                return rec
            end
            local x, y = case_position(index)
            return try_group(rec, {data = build_group_data({
                name = NAME_PREFIX .. "task_bombing",
                unit_type = SPEC.templates["F-16C_strike_jdam"].unit_type,
                count = 1,
                task = SPEC.templates["F-16C_strike_jdam"].task,
                tasks = {bombing_task(x + 2000, y + 2000)},
                x = x, y = y, alt = CONFIG.altitude,
            })}, cid, cat)
        end,
    })

    add({
        id = "task.AttackGroup",
        kind = "group",
        required = true,
        label = "ComboTask carrying an AttackGroup task against a live group",
        attempt = function(case, index)
            local rec = new_case_record(case)
            local cid = country_id("USA")
            local red = country_id("RUSSIA")
            local cat = group_category("AIRPLANE")
            if cid == nil or red == nil or cat == nil then
                rec.status = "SKIP"
                rec.error = "country or Group.Category unavailable"
                return rec
            end

            local x, y = case_position(index)
            -- A victim first: AttackGroup needs a groupId that resolves.
            local victim_name = NAME_PREFIX .. "attackgroup_victim"
            local made = add_group(red, cat, build_group_data({
                name = victim_name,
                unit_type = SPEC.templates["F-16C_cap"].unit_type,
                count = 1,
                task = "Nothing",
                x = x + 8000, y = y, alt = CONFIG.altitude,
            }))
            if made then
                rec.created[#rec.created + 1] = {kind = "group", name = victim_name}
            end
            local victim = live_group(victim_name)
            if not victim then
                rec.status = "SKIP"
                rec.error = "could not create a target group to attack"
                return rec
            end
            local gid = try(victim.getID, victim)
            rec.detail.target_group_id = gid
            if gid == nil then
                rec.status = "SKIP"
                rec.error = "Group.getID returned nothing"
                return rec
            end

            return try_group(rec, {data = build_group_data({
                name = NAME_PREFIX .. "task_attackgroup",
                unit_type = SPEC.templates["F-16C_strike_jdam"].unit_type,
                count = 1,
                task = SPEC.templates["F-16C_strike_jdam"].task,
                tasks = {attack_group_task(gid)},
                x = x, y = y, alt = CONFIG.altitude,
            })}, cid, cat)
        end,
    })

    -- 6. Static type/category pairs beyond the client's own.
    for i = 1, #SPEC.static_pairs do
        local pair = SPEC.static_pairs[i]
        if not pair.client then
            add({
                id = "static." .. string.gsub(pair.type .. "_" .. pair.category,
                                              "%s", "_"),
                kind = "static",
                required = false,
                label = "alternative static type '" .. pair.type
                        .. "' category '" .. pair.category .. "'",
                attempt = function(case, index)
                    local rec = new_case_record(case)
                    local cid = country_id("RUSSIA")
                    if cid == nil then
                        rec.status = "SKIP"
                        rec.error = "country.id.RUSSIA is nil"
                        return rec
                    end
                    local x, y = case_position(index)
                    return try_static(rec, build_static_data({
                        name = NAME_PREFIX .. "static_" .. i,
                        unit_type = pair.type,
                        static_category = pair.category,
                        x = x, y = y,
                    }), cid)
                end,
            })
        end
    end

    -- 7. Pylons. The spawn is not the question here; the ammunition is.
    local clsids = CONFIG.clsids or DEFAULT_CLSIDS

    add({
        id = "pylon.baseline_empty",
        kind = "pylon",
        required = true,
        label = "the client's DEFAULT_PAYLOAD, whose pylons table is empty",
        expect_ammo = "empty",
        attempt = function(case, index)
            local rec = new_case_record(case)
            local cid, cat = country_id("USA"), group_category("AIRPLANE")
            if cid == nil or cat == nil then
                rec.status = "SKIP"
                rec.error = "country or Group.Category unavailable"
                return rec
            end
            local x, y = case_position(index)
            local name = NAME_PREFIX .. "pylon_baseline"
            rec.inspect_unit = name .. "_1"
            rec.expect_ammo = "empty"
            return try_group(rec, {data = build_group_data({
                name = name,
                unit_type = SPEC.templates["F-16C_strike_jdam"].unit_type,
                count = 1,
                task = SPEC.templates["F-16C_strike_jdam"].task,
                payload = DEFAULT_PAYLOAD,
                x = x, y = y, alt = CONFIG.altitude,
            })}, cid, cat)
        end,
    })

    for i = 1, #clsids do
        local probe = clsids[i]
        add({
            id = "pylon." .. tostring(i),
            kind = "pylon",
            -- Only the control is required: it is what proves the probe can
            -- tell an empty pylon from a loaded one at all. The candidates
            -- are discovery, and a miss is information, not a failure.
            required = probe.expect == "empty",
            label = (probe.label or "CLSID probe") .. " -- "
                    .. tostring(probe.clsid) .. " on pylon "
                    .. tostring(probe.pylon or 1),
            attempt = function(case, index)
                local rec = new_case_record(case)
                local cid, cat = country_id("USA"), group_category("AIRPLANE")
                if cid == nil or cat == nil then
                    rec.status = "SKIP"
                    rec.error = "country or Group.Category unavailable"
                    return rec
                end
                local payload = {
                    pylons = {[probe.pylon or 1] = {CLSID = probe.clsid}},
                    fuel = DEFAULT_PAYLOAD.fuel,
                    flare = DEFAULT_PAYLOAD.flare,
                    chaff = DEFAULT_PAYLOAD.chaff,
                    gun = DEFAULT_PAYLOAD.gun,
                }
                local x, y = case_position(index)
                local name = NAME_PREFIX .. "pylon_" .. i
                rec.inspect_unit = name .. "_1"
                rec.expect_ammo = probe.expect
                rec.detail.clsid = probe.clsid
                return try_group(rec, {data = build_group_data({
                    name = name,
                    unit_type = SPEC.templates["F-16C_strike_jdam"].unit_type,
                    count = 1,
                    task = SPEC.templates["F-16C_strike_jdam"].task,
                    payload = payload,
                    x = x, y = y, alt = CONFIG.altitude,
                })}, cid, cat)
            end,
        })
    end

    return cases
end

-- ------------------------------------------------------------------
-- Cross-check against the live client, when there is one
-- ------------------------------------------------------------------

local TEMPLATE_FIELDS = {"unit_type", "lead_type", "count", "task", "skill",
                         "static", "static_category", "spread"}

local function pylon_count(payload)
    if type(payload) ~= "table" or type(payload.pylons) ~= "table" then
        return nil
    end
    local n = 0
    for _ in pairs(payload.pylons) do n = n + 1 end
    return n
end

--- Compare SPEC.templates with CampaignClient.TEMPLATES. Any difference is
--- a DRIFT result: this file would then be validating something the client
--- does not actually ask DCS for, which is worse than not validating at all.
local function cross_check()
    local client = _G.CampaignClient
    local live = client and client.TEMPLATES
    if type(live) ~= "table" then
        RUN.template_source = "mirror (campaign_client.lua is not loaded)"
        record({
            id = "drift.templates",
            kind = "meta",
            required = false,
            status = "SKIP",
            label = "cross-check SPEC.templates against CampaignClient.TEMPLATES",
            error = "campaign_client.lua is not loaded in this mission; the "
                    .. "mirrored template values could not be verified",
            detail = {},
        })
        return
    end

    RUN.template_source = "CampaignClient.TEMPLATES (cross-checked)"

    local problems = {}
    local seen = {}
    for key, mine in pairs(SPEC.templates) do
        seen[key] = true
        local theirs = live[key]
        if theirs == nil then
            problems[#problems + 1] = key .. ": not in the client"
        else
            for i = 1, #TEMPLATE_FIELDS do
                local f = TEMPLATE_FIELDS[i]
                if mine[f] ~= theirs[f] then
                    problems[#problems + 1] = key .. "." .. f .. ": validator "
                        .. tostring(mine[f]) .. " vs client " .. tostring(theirs[f])
                end
            end
            local a, b = pylon_count(mine.payload), pylon_count(theirs.payload)
            if a ~= b then
                problems[#problems + 1] = key .. ".payload.pylons: validator "
                    .. tostring(a) .. " entries vs client " .. tostring(b)
            end
        end
    end
    for key in pairs(live) do
        if not seen[key] then
            problems[#problems + 1] = key .. ": in the client, not validated here"
        end
    end
    table.sort(problems)

    record({
        id = "drift.templates",
        kind = "meta",
        required = true,
        status = (#problems == 0) and "OK" or "DRIFT",
        label = "cross-check SPEC.templates against CampaignClient.TEMPLATES",
        error = (#problems > 0) and table.concat(problems, "; ") or nil,
        detail = {mismatches = #problems},
    })
end

-- ------------------------------------------------------------------
-- Origin
-- ------------------------------------------------------------------

local function pick_origin()
    if type(CONFIG.origin) == "table"
       and type(CONFIG.origin.x) == "number"
       and type(CONFIG.origin.y) == "number" then
        RUN.origin = {x = CONFIG.origin.x, y = CONFIG.origin.y}
        RUN.origin_source = "CAMPAIGN_VALIDATE_CONFIG.origin"
        return
    end

    -- An airbase is land by definition, which is what the static cases need.
    if coalition and coalition.getAirbases and coalition.side then
        local sides = {coalition.side.BLUE, coalition.side.RED,
                       coalition.side.NEUTRAL}
        for i = 1, #sides do
            local bases = try(coalition.getAirbases, sides[i]) or {}
            local base = bases[1]
            local point = base and try(base.getPoint, base)
            if point then
                -- 10 km clear of the runway, so nothing spawns on the ramp.
                RUN.origin = {x = point.x + 10000, y = point.z + 10000}
                RUN.origin_source = "airbase "
                    .. tostring(try(base.getName, base) or "?")
                return
            end
        end
    end

    if coalition and coalition.getPlayers and coalition.side then
        local sides = {coalition.side.BLUE, coalition.side.RED,
                       coalition.side.NEUTRAL}
        for i = 1, #sides do
            local units = try(coalition.getPlayers, sides[i]) or {}
            local unit = units[1]
            local point = unit and try(unit.getPoint, unit)
            if point then
                RUN.origin = {x = point.x, y = point.z}
                RUN.origin_source = "player unit"
                return
            end
        end
    end

    RUN.origin = {x = 0, y = 0}
    RUN.origin_source = "default (0,0) -- SET AN ORIGIN, this may be sea"
end

-- ------------------------------------------------------------------
-- Report
-- ------------------------------------------------------------------

local function default_out_path()
    if CONFIG.out_path then return CONFIG.out_path end
    local dir = lfs and try(lfs.writedir)
    if type(dir) == "string" and #dir > 0 then
        return dir .. "Logs/campaign_validation.json"
    end
    return "campaign_validation.json"
end

local function report_table()
    local results = array({})
    for i = 1, #RUN.results do
        local r = RUN.results[i]
        local detail = {}
        for k, v in pairs(r.detail) do detail[k] = v end
        results[i] = {
            id = r.id,
            kind = r.kind,
            label = r.label,
            required = r.required,
            status = r.status,
            error = r.error or "",
            detail = detail,
        }
    end

    local counts = {}
    for i = 1, #STATUSES do counts[STATUSES[i]] = RUN.counts[STATUSES[i]] or 0 end

    return {
        version = V.VERSION,
        prefix = PREFIX,
        theatre = (env and env.mission and env.mission.theatre) or "",
        mission_time = (timer and try(timer.getTime)) or 0,
        origin = {x = RUN.origin.x, y = RUN.origin.y, source = RUN.origin_source},
        template_source = RUN.template_source,
        complete = RUN.finished,
        leaked = RUN.leaked or 0,
        counts = counts,
        total = #RUN.results,
        results = results,
    }
end

local function write_report()
    RUN.out_path = default_out_path()
    if not io or type(io.open) ~= "function" then
        RUN.out_error = "io is sanitised; JSON report not written "
                        .. "(the log lines above are the whole result)"
        return
    end
    local ok, err = pcall(function()
        local fh, oerr = io.open(RUN.out_path, "w")
        if not fh then error(tostring(oerr), 0) end
        fh:write(json_encode(report_table()))
        fh:close()
    end)
    if ok then
        RUN.out_written = true
    else
        RUN.out_error = tostring(err)
    end
end

local function finalize()
    if RUN.finished then return end
    RUN.finished = true

    -- Defensive sweep: anything a case somehow left behind.
    local leaked = 0
    for i = 1, #RUN.results do
        local r = RUN.results[i]
        if r.created then leaked = leaked + cleanup(r.created) end
    end
    if RUN.pending then
        leaked = leaked + cleanup(RUN.pending.created or {})
        RUN.pending = nil
    end
    RUN.leaked = leaked

    write_report()

    local parts = {"SUMMARY", "total=" .. #RUN.results}
    for i = 1, #STATUSES do
        parts[#parts + 1] = string.lower(STATUSES[i]) .. "="
                            .. tostring(RUN.counts[STATUSES[i]] or 0)
    end
    parts[#parts + 1] = "leaked=" .. leaked
    parts[#parts + 1] = 'json="' .. trim(RUN.out_path or "") .. '"'
    parts[#parts + 1] = "json_written=" .. (RUN.out_written and "1" or "0")
    parts[#parts + 1] = 'json_error="' .. trim(RUN.out_error or "") .. '"'
    local line = table.concat(parts, " ")
    if leaked > 0 then log_err_line(line) else log_line(line) end
    log_line("END")
end

-- ------------------------------------------------------------------
-- The tick
-- ------------------------------------------------------------------

--- Second half of a case: re-check retrievability a tick later, read the
--- ammunition, destroy everything and confirm it is gone.
local function finish(rec)
    if rec.kind == "pylon" and rec.inspect_unit then
        local n, summary, err = inspect_ammo(rec.inspect_unit)
        if err then
            rec.detail.ammo = "unknown"
            if rec.status == "OK" then
                rec.status = "UNKNOWN"
                rec.error = err
            end
        else
            rec.detail.ammo = n
            rec.detail.ammo_types = summary
            if rec.status == "OK" then
                if rec.expect_ammo == "empty" then
                    if n == 0 then
                        rec.status = "OK"
                    else
                        rec.status = "REJECTED"
                        rec.error = "expected an empty pylon and found " .. n
                            .. " munition(s); this probe cannot tell loaded "
                            .. "from unarmed"
                    end
                elseif n == 0 then
                    rec.status = "UNKNOWN"
                    rec.error = "the pylon stayed empty: this DCS build does "
                        .. "not know that CLSID"
                end
            end
        end
    end

    -- Retrievability one tick on, for EVERY object the case created: the
    -- client checks immediately, and if these two ever disagree that check
    -- is looking a frame too early.
    if #rec.created > 0 then
        local all_live = true
        for i = 1, #rec.created do
            local item = rec.created[i]
            local live
            if item.kind == "group" then
                live = live_group(item.name) ~= nil
            else
                live = live_static(item.name) ~= nil
            end
            if not live then all_live = false end
        end
        rec.detail.retrievable_next = all_live
    end

    local leaked = cleanup(rec.created)
    rec.detail.cleaned = leaked == 0
    if leaked > 0 then
        rec.status = "ERROR"
        rec.error = (rec.error and (rec.error .. "; ") or "")
                    .. leaked .. " object(s) could not be destroyed"
    end
    rec.created = {}
    record(rec)
end

local function step()
    if RUN.pending then
        finish(RUN.pending)
        RUN.pending = nil
        return true
    end

    local case = RUN.cases[RUN.index]
    if not case then
        finalize()
        return false
    end
    RUN.index = RUN.index + 1

    local ok, rec = pcall(case.attempt, case, RUN.index - 1)
    if not ok then
        record({
            id = case.id,
            kind = case.kind,
            label = case.label,
            required = case.required and true or false,
            status = "ERROR",
            error = "the case itself raised: " .. tostring(rec),
            detail = {},
        })
        return true
    end

    if rec.created and #rec.created > 0 then
        RUN.pending = rec
    else
        rec.created = nil
        record(rec)
    end
    return true
end

local function tick()
    if RUN.finished then return nil end
    local more = true
    for _ = 1, math.max(1, CONFIG.cases_per_tick) do
        if not more then break end
        local ok, result = pcall(step)
        if not ok then
            log_err_line("STEP-ERROR " .. tostring(result))
            -- One broken case must not strand the rest of the run.
            RUN.pending = nil
            result = true
        end
        more = result
    end
    if not more or RUN.finished then return nil end
    return timer.getTime() + CONFIG.tick
end

-- ------------------------------------------------------------------
-- Public surface
-- ------------------------------------------------------------------

function V.start()
    if RUN.started then return end
    RUN.started = true

    pick_origin()
    RUN.cases = build_cases()

    log_line("BEGIN version=" .. V.VERSION
             .. " cases=" .. #RUN.cases
             .. ' origin="' .. RUN.origin.x .. "," .. RUN.origin.y .. '"'
             .. ' origin_source="' .. trim(RUN.origin_source) .. '"'
             .. ' note="one result line per case, a summary at the end"')

    cross_check()

    RUN.sched_id = timer.scheduleFunction(
        function() return tick() end, {}, timer.getTime() + CONFIG.tick)
end

--- Run every remaining case right now, ignoring the tick budget. For a test
--- harness, or for a one-shot from a debug hook. Never call this from a
--- mission trigger: it is the frame hitch the tick exists to avoid.
function V.run_to_completion(limit)
    limit = limit or 10000
    local n = 0
    while not RUN.finished and n < limit do
        step()
        n = n + 1
    end
    if not RUN.finished then finalize() end
    return RUN.finished
end

function V.stop()
    if RUN.sched_id and timer and timer.removeFunction then
        pcall(timer.removeFunction, RUN.sched_id)
        RUN.sched_id = nil
    end
    if not RUN.finished then finalize() end
end

--- Everything a test or a debug hook needs, with no mutation.
function V.report()
    return report_table()
end

function V.results()
    return RUN.results
end

function V.finished()
    return RUN.finished
end

V.CONFIG = CONFIG
V.SPEC = SPEC
V.PREFIX = PREFIX
V.DEFAULT_CLSIDS = DEFAULT_CLSIDS
V.json_encode = json_encode

_G.CampaignValidator = V

-- Guarded so the file can be loaded outside DCS for syntax checking, and so
-- a harness can drive it by hand.
if CONFIG.auto_start and timer and coalition and Group and StaticObject then
    V.start()
end

return V
