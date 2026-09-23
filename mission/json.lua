--[[----------------------------------------------------------------------
  mission/json.lua -- minimal JSON codec for the DCS campaign client.

  Written from scratch for this project (Lua 5.1 / LuaJIT, no bit ops, no
  utf8 library, no third-party code) so the provenance is clean.

  Scope: exactly the subset docs/protocol.md uses -- objects, arrays,
  strings, numbers, booleans, null. That is all the wire needs; anything
  else is a bug in the caller, not a feature to support.

  API
    json.encode(value)      -> string           | nil, err
    json.decode(text)       -> value            | nil, err
    json.array(t)           -> t tagged as a JSON array
    json.object(t)          -> t tagged as a JSON object
    json.null                  sentinel for a decoded JSON null
    json.isnull(v)          -> boolean

  Neither entry point ever raises: both return nil plus a message. That is
  deliberate. This code runs on the DCS simulation thread, where an
  uncaught error kills the script for the rest of the mission.

  ------------------------------------------------------------------------
  THE EMPTY-TABLE TRAP, AND WHY THIS FILE HAS TAGS

  Lua has one table type. `{}` is simultaneously an empty list and an empty
  hash, so a naive encoder has to guess, and whichever way it guesses it is
  wrong half the time. The protocol makes both halves load-bearing:

      spawn.route   is an array  -- [] when the engine sends no waypoints
      spawn.tasking is an object -- {} when there is no tasking
      state.groups  is an array  -- [] when nothing is instantiated
      observer.observers is an array -- [] when no humans are connected

  The last two are the ones that bite. An empty mission has no players and
  possibly no engine-owned groups, so the *steady state* of a quiet server
  is exactly the ambiguous case. Encoding `observers` as {} would hand the
  engine a dict where campaign/protocol.py expects a list: _build() only
  converts a value it finds to be a list, so a {} sails straight through
  into ObserverReport.observers and the engine iterates garbage. Silent
  wrong, not loud wrong -- the worst kind.

  So the tag is explicit, not inferred, wherever it matters:

      json.array(t)  marks t as an array   -- encodes [] when empty
      json.object(t) marks t as an object  -- encodes {} when empty

  The tag lives in a shared metatable (__jsontype), so tagging costs one
  setmetatable and no per-element bookkeeping. A tagged table's contents
  are still ordinary Lua values.

  Untagged tables fall back to inference, which is a convenience for
  hand-written literals, never a substitute for tagging on the wire:

      #t > 0 and no extra keys -> array
      empty                    -> object   (documented default)
      anything else            -> object

  Reasoning about the round trip, which is the property that actually
  matters here:

      decode('{"route":[],"tasking":{}}')
        -> route is tagged array, tasking is tagged object
      encode(that)
        -> '{"route":[],"tasking":{}}'          -- identical, both empties

  Without the tags the same round trip yields '{"route":{},"tasking":{}}'
  and the route stops being a route. With them, decode->encode is stable
  for every frame in docs/protocol.md, including the empty cases. The
  campaign client relies on that: it tags every array it builds
  (observers, groups, pos, route) and never leans on inference.

  Note the one thing the tag deliberately does NOT do: it does not survive
  a table copy. `local t2 = {}` from a tagged t2 loses the tag. Callers
  build the table and tag it in the same breath -- json.array{...} -- so
  there is no window where an untagged empty array exists.
------------------------------------------------------------------------]]

local json = {}

json._NAME = "campaign-json"
json._VERSION = 1

local schar, sfind, sformat, sgsub, smatch, ssub =
      string.char, string.find, string.format,
      string.gsub, string.match, string.sub
local tconcat, tsort = table.concat, table.sort
local floor = math.floor
local INF = math.huge

--- Nesting cap. Protocol frames nest three deep at most; anything beyond
--- this is a cyclic table on encode or a hostile payload on decode, and
--- either way recursing is worse than refusing.
local MAX_DEPTH = 32

-- ------------------------------------------------------------------
-- Tags and sentinels
-- ------------------------------------------------------------------

local ARRAY_MT = {__jsontype = "array"}
local OBJECT_MT = {__jsontype = "object"}

local NULL = setmetatable({}, {__tostring = function() return "null" end})

json.null = NULL

--- Tag `t` (default: a fresh table) as a JSON array. Returns `t`.
function json.array(t)
    return setmetatable(t or {}, ARRAY_MT)
end

--- Tag `t` (default: a fresh table) as a JSON object. Returns `t`.
function json.object(t)
    return setmetatable(t or {}, OBJECT_MT)
end

function json.isnull(v)
    return v == NULL
end

--- "array", "object", or nil when the table carries no tag.
function json.tagof(t)
    local mt = getmetatable(t)
    return mt and mt.__jsontype or nil
end

-- ------------------------------------------------------------------
-- Encoder
-- ------------------------------------------------------------------

local ESCAPES = {
    ['"'] = '\\"',
    ['\\'] = '\\\\',
    ['\b'] = '\\b',
    ['\f'] = '\\f',
    ['\n'] = '\\n',
    ['\r'] = '\\r',
    ['\t'] = '\\t',
}
for i = 0, 31 do
    local c = schar(i)
    if not ESCAPES[c] then ESCAPES[c] = sformat("\\u%04x", i) end
end

-- Bytes >= 0x80 pass through untouched: JSON strings are UTF-8 and so is
-- everything DCS hands us (unit names, player names, theatre names). We do
-- not transcode, because re-encoding a byte string we cannot prove is UTF-8
-- would corrupt the names the engine matches on.
local STRING_SPECIALS = '[%z\1-\31\\"]'

local function encode_string(s, out)
    out[#out + 1] = '"'
    out[#out + 1] = (sgsub(s, STRING_SPECIALS, ESCAPES))
    out[#out + 1] = '"'
end

local function encode_number(v, out)
    if v ~= v then error("cannot encode NaN", 0) end
    if v == INF or v == -INF then error("cannot encode infinity", 0) end
    -- %.14g is Lua 5.1's own tostring format for numbers, pinned explicitly
    -- so LuaJIT and PUC Lua produce byte-identical frames.
    out[#out + 1] = sformat("%.14g", v)
end

local encode_value

local function encode_array(t, n, out, depth)
    out[#out + 1] = "["
    for i = 1, n do
        if i > 1 then out[#out + 1] = "," end
        encode_value(t[i], out, depth + 1)
    end
    out[#out + 1] = "]"
end

local function encode_object(t, out, depth)
    local keys, n = {}, 0
    for k in pairs(t) do
        if type(k) ~= "string" then
            error("object key is a " .. type(k) .. ", must be a string", 0)
        end
        n = n + 1
        keys[n] = k
    end
    -- Sorted so a frame encodes to the same bytes every time. pairs() order
    -- is unspecified in Lua, and a wire log you cannot diff is half a log.
    tsort(keys)
    out[#out + 1] = "{"
    for i = 1, n do
        if i > 1 then out[#out + 1] = "," end
        encode_string(keys[i], out)
        out[#out + 1] = ":"
        encode_value(t[keys[i]], out, depth + 1)
    end
    out[#out + 1] = "}"
end

local function encode_table(t, out, depth)
    if depth > MAX_DEPTH then
        error("nesting deeper than " .. MAX_DEPTH .. " (cycle?)", 0)
    end

    local tag = json.tagof(t)
    local len = #t
    local count = 0
    for _ in pairs(t) do count = count + 1 end

    if tag == nil then
        tag = (len > 0 and count == len) and "array" or "object"
    end

    if tag == "array" then
        -- A tagged array with holes or stray string keys would encode as a
        -- shorter array than the caller believes it built. Refuse instead.
        if count ~= len then
            error("array has " .. count .. " keys but a length of " .. len
                  .. " (hole, or a non-integer key)", 0)
        end
        encode_array(t, len, out, depth)
    else
        encode_object(t, out, depth)
    end
end

encode_value = function(v, out, depth)
    if v == NULL then
        out[#out + 1] = "null"
        return
    end
    local ty = type(v)
    if ty == "string" then
        encode_string(v, out)
    elseif ty == "number" then
        encode_number(v, out)
    elseif ty == "boolean" then
        out[#out + 1] = v and "true" or "false"
    elseif ty == "table" then
        encode_table(v, out, depth)
    elseif ty == "nil" then
        error("cannot encode nil (use json.null for an explicit null)", 0)
    else
        error("cannot encode a " .. ty, 0)
    end
end

--- Serialise `value` to JSON text. Returns nil plus a message on failure.
function json.encode(value)
    local out = {}
    local ok, err = pcall(encode_value, value, out, 1)
    if not ok then
        return nil, "json.encode: " .. tostring(err)
    end
    return tconcat(out)
end

-- ------------------------------------------------------------------
-- Decoder
-- ------------------------------------------------------------------

local function perr(msg, pos)
    error(msg .. " at byte " .. pos, 0)
end

local function utf8_encode(cp)
    if cp < 0x80 then
        return schar(cp)
    elseif cp < 0x800 then
        return schar(0xC0 + floor(cp / 0x40),
                     0x80 + cp % 0x40)
    elseif cp < 0x10000 then
        return schar(0xE0 + floor(cp / 0x1000),
                     0x80 + floor(cp / 0x40) % 0x40,
                     0x80 + cp % 0x40)
    end
    return schar(0xF0 + floor(cp / 0x40000),
                 0x80 + floor(cp / 0x1000) % 0x40,
                 0x80 + floor(cp / 0x40) % 0x40,
                 0x80 + cp % 0x40)
end

local SIMPLE_ESC = {
    ['"'] = '"', ['\\'] = '\\', ['/'] = '/',
    b = '\b', f = '\f', n = '\n', r = '\r', t = '\t',
}

local function skip_ws(s, pos)
    local _, last = sfind(s, "^[ \t\r\n]*", pos)
    return last + 1
end

local function parse_string(s, pos)
    -- `pos` indexes the opening quote.
    local buf, n = {}, 0
    local i = pos + 1
    while true do
        local nxt = sfind(s, '["\\]', i)
        if not nxt then perr("unterminated string", pos) end
        if nxt > i then
            n = n + 1
            buf[n] = ssub(s, i, nxt - 1)
        end
        if ssub(s, nxt, nxt) == '"' then
            return tconcat(buf), nxt + 1
        end
        local esc = ssub(s, nxt + 1, nxt + 1)
        local simple = SIMPLE_ESC[esc]
        if simple then
            n = n + 1
            buf[n] = simple
            i = nxt + 2
        elseif esc == "u" then
            local hex = ssub(s, nxt + 2, nxt + 5)
            if not sfind(hex, "^%x%x%x%x$") then
                perr("malformed \\u escape", nxt)
            end
            local cp = tonumber(hex, 16)
            i = nxt + 6
            if cp >= 0xD800 and cp <= 0xDBFF then
                -- High surrogate: consume the low half if it is there.
                local lo = smatch(ssub(s, i, i + 5), "^\\u(%x%x%x%x)$")
                local lonum = lo and tonumber(lo, 16)
                if lonum and lonum >= 0xDC00 and lonum <= 0xDFFF then
                    cp = 0x10000 + (cp - 0xD800) * 0x400 + (lonum - 0xDC00)
                    i = i + 6
                end
                -- An unpaired surrogate is encoded as-is rather than
                -- rejected: a name we can still match on beats a dropped
                -- frame.
            end
            n = n + 1
            buf[n] = utf8_encode(cp)
        elseif esc == "" then
            perr("unterminated escape", nxt)
        else
            perr("unknown escape \\" .. esc, nxt)
        end
    end
end

local NUM_WITH_EXP = "^%-?%d+%.?%d*[eE][-+]?%d+"
local NUM_PLAIN = "^%-?%d+%.?%d*"

local function parse_number(s, pos)
    local text = smatch(s, NUM_WITH_EXP, pos) or smatch(s, NUM_PLAIN, pos)
    if not text then perr("expected a number", pos) end
    local v = tonumber(text)
    if not v then perr("malformed number '" .. text .. "'", pos) end
    return v, pos + #text
end

local parse_value

local function parse_array(s, pos, depth)
    local t = json.array({})
    local n = 0
    local i = skip_ws(s, pos + 1)
    if ssub(s, i, i) == "]" then return t, i + 1 end
    while true do
        local v
        v, i = parse_value(s, i, depth + 1)
        n = n + 1
        t[n] = v
        i = skip_ws(s, i)
        local c = ssub(s, i, i)
        if c == "," then
            i = skip_ws(s, i + 1)
        elseif c == "]" then
            return t, i + 1
        else
            perr("expected ',' or ']' in array", i)
        end
    end
end

local function parse_object(s, pos, depth)
    local t = json.object({})
    local i = skip_ws(s, pos + 1)
    if ssub(s, i, i) == "}" then return t, i + 1 end
    while true do
        if ssub(s, i, i) ~= '"' then perr("expected an object key", i) end
        local key
        key, i = parse_string(s, i)
        i = skip_ws(s, i)
        if ssub(s, i, i) ~= ":" then perr("expected ':' after object key", i) end
        i = skip_ws(s, i + 1)
        local v
        v, i = parse_value(s, i, depth + 1)
        t[key] = v
        i = skip_ws(s, i)
        local c = ssub(s, i, i)
        if c == "," then
            i = skip_ws(s, i + 1)
        elseif c == "}" then
            return t, i + 1
        else
            perr("expected ',' or '}' in object", i)
        end
    end
end

parse_value = function(s, pos, depth)
    if depth > MAX_DEPTH then perr("nesting deeper than " .. MAX_DEPTH, pos) end
    local c = ssub(s, pos, pos)
    if c == "" then perr("unexpected end of input", pos) end
    if c == "{" then
        return parse_object(s, pos, depth)
    elseif c == "[" then
        return parse_array(s, pos, depth)
    elseif c == '"' then
        return parse_string(s, pos)
    elseif c == "t" then
        if ssub(s, pos, pos + 3) == "true" then return true, pos + 4 end
        perr("expected 'true'", pos)
    elseif c == "f" then
        if ssub(s, pos, pos + 4) == "false" then return false, pos + 5 end
        perr("expected 'false'", pos)
    elseif c == "n" then
        if ssub(s, pos, pos + 3) == "null" then return NULL, pos + 4 end
        perr("expected 'null'", pos)
    end
    return parse_number(s, pos)
end

local function decode_all(text)
    local pos = skip_ws(text, 1)
    local value
    value, pos = parse_value(text, pos, 1)
    pos = skip_ws(text, pos)
    if pos <= #text then
        perr("trailing data after the top-level value", pos)
    end
    return value
end

--- Parse JSON `text`. Returns nil plus a message on failure.
---
--- Objects and arrays come back tagged, so decode -> encode round-trips an
--- empty array as [] and an empty object as {}. A JSON null becomes
--- json.null rather than nil, because a nil would silently erase the key
--- and turn "the engine said null" into "the engine said nothing".
function json.decode(text)
    if type(text) ~= "string" then
        return nil, "json.decode: expected a string, got a " .. type(text)
    end
    local ok, result = pcall(decode_all, text)
    if not ok then
        return nil, "json.decode: " .. tostring(result)
    end
    return result
end

-- Exposed as a global as well as returned, because the DCS mission editor's
-- "DO SCRIPT FILE" action discards the chunk's return value. See
-- mission/README.md for the load order.
_G.CAMPAIGN_JSON = json

return json
