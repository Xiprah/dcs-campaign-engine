--[==[--------------------------------------------------------------------
  tests/dcsmock/socket_shim.lua -- the LuaSocket surface the client uses,
  backed by a real Python socket.

  This is a shim and not a simulation: every call below ends up in a genuine
  non-blocking BSD socket, so the client opens a real TCP connection to a
  real engine and the "timeout" semantics it is built around are the
  operating system's, not a mock's idea of them.

  The one thing worth spelling out is `send`. LuaSocket's contract is:

      sock:send(data [, i [, j]])
        -> j                        on success (index of the LAST byte sent)
        -> nil, "timeout", last     on a partial write

  Both the success value and `last` are absolute indices into `data`, not
  counts, and mission/campaign_client.lua does index arithmetic with them.
  Getting that wrong here would make a passing test meaningless, so the
  Python half returns exactly those indices.

  Called with the Python bridge table as the chunk argument.
------------------------------------------------------------------------]==]

local py = ...

local socket = {}

socket._NAME = "dcsmock-luasocket"
socket._VERSION = "mock"

local function new_socket(handle)
    local s = {__h = handle}

    function s:settimeout(value)
        py.settimeout(self.__h, value)
        return 1
    end

    function s:connect(host, port)
        return py.connect(self.__h, host, port)
    end

    function s:getpeername()
        return py.getpeername(self.__h)
    end

    function s:getsockname()
        return py.getsockname(self.__h)
    end

    function s:setoption(option, value)
        return py.setoption(self.__h, option, value)
    end

    function s:send(data, i, j)
        return py.send(self.__h, data, i, j)
    end

    function s:receive(pattern)
        return py.receive(self.__h, pattern)
    end

    function s:close()
        return py.close(self.__h)
    end

    function s:shutdown(how)
        return py.close(self.__h)
    end

    return s
end

function socket.tcp()
    local handle = py.tcp()
    if handle == nil then return nil, "socket: cannot create" end
    return new_socket(handle)
end

--- Only the readable/writable sets the client asks for; no timeout wait,
--- because the client always passes 0 and a blocking select on the sim
--- thread is the thing the whole design exists to avoid.
function socket.select(readable, writable, timeout)
    local r, w = {}, {}
    if readable then
        for i = 1, #readable do
            local s = readable[i]
            if py.readable(s.__h) then r[#r + 1] = s end
        end
    end
    if writable then
        for i = 1, #writable do
            local s = writable[i]
            if py.writable(s.__h) then w[#w + 1] = s end
        end
    end
    return r, w
end

function socket.gettime()
    return py.gettime()
end

function socket.sleep(_)
    return nil
end

return socket
