"""A mock DCS mission environment that really runs mission/*.lua.

The Lua in `mission/` is the half of this system that DCS executes and that
nothing else ever has. Everything in here exists so that it can be executed
by a test instead: a Lua 5.1 interpreter (lupa), the DCS globals the client
actually calls, and -- the part that matters most -- a LuaSocket stand-in
backed by a genuine non-blocking Python socket, so the client opens a real
TCP connection to a real campaign engine.

What is deliberately *not* modelled: flight dynamics, weapons, damage. The
harness kills things when a test says to, and empties a pylon when a test
says a missile was fired (:meth:`DCSMock.fire_weapon`); `Unit:getAmmo` only
reports what a test loaded (:meth:`DCSMock.load_ammo`). Mission time is
whatever :meth:`DCSMock.advance` has been called with; nothing here reads a
clock.

Nothing in this package is imported by the main suite unless lupa is
installed -- see the skip guard in tests/test_mission_client.py.
"""

from __future__ import annotations

import errno
import select
import socket as pysocket
from pathlib import Path
from typing import Any

try:
    from lupa.lua51 import LuaRuntime
except ImportError:  # pragma: no cover - the main suite runs without lupa
    # Importable, but not usable. `unittest discover` imports every package
    # under tests/, so this module may not hard-require lupa; the tests that
    # need it are guarded with skipUnless instead.
    LuaRuntime = None

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MISSION = REPO / "mission"

#: Errnos a non-blocking connect reports while it is still in flight.
_IN_FLIGHT = {
    errno.EINPROGRESS,
    errno.EALREADY,
    errno.EWOULDBLOCK,
    getattr(errno, "WSAEWOULDBLOCK", errno.EWOULDBLOCK),
    getattr(errno, "WSAEALREADY", errno.EALREADY),
    getattr(errno, "WSAEINVAL", errno.EINVAL),
    10035,  # WSAEWOULDBLOCK, spelled out for a Windows host without the alias
    10037,  # WSAEALREADY
}

_ALREADY_CONNECTED = {
    errno.EISCONN,
    getattr(errno, "WSAEISCONN", errno.EISCONN),
    10056,
}

_LOADER = """
local src, name = ...
local chunk, err = loadstring(src, name)
if not chunk then error(err, 0) end
return chunk
"""


class LuaSocketBridge:
    """The Python half of tests/dcsmock/socket_shim.lua.

    Holds real sockets keyed by an integer handle and reproduces LuaSocket's
    return conventions exactly, including `timeout` in place of blocking and
    the absolute byte *index* that `send` reports.
    """

    def __init__(self, clock) -> None:
        self._clock = clock
        self._socks: dict[int, pysocket.socket] = {}
        self._next = 0
        #: Cap on the bytes one send() call may write, so a test can force
        #: the client's partial-write path. None means "whatever fits".
        self.send_limit: int | None = None
        #: Every socket handed out, for leak assertions.
        self.opened = 0
        #: (host, port) exactly as the Lua handed them to connect(), so a
        #: test can tell an address from a name the resolver would see.
        self.connected_to: list[tuple[str, int]] = []
        self.closed = 0

    # -- helpers -----------------------------------------------------------

    def _sock(self, handle: int) -> pysocket.socket | None:
        return self._socks.get(int(handle))

    @staticmethod
    def _as_bytes(data: Any) -> bytes:
        if isinstance(data, bytes):
            return data
        return str(data).encode("utf-8")

    # -- the surface socket_shim.lua calls ---------------------------------

    def tcp(self):
        sock = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_STREAM)
        sock.setblocking(False)
        self._next += 1
        self._socks[self._next] = sock
        self.opened += 1
        return self._next

    def settimeout(self, handle, value=None):
        sock = self._sock(handle)
        if sock is None:
            return None
        # The client only ever asks for 0. Anything else would be a blocking
        # call on the sim thread, which is the bug this harness exists to
        # catch, so refuse rather than quietly honour it.
        if value not in (0, 0.0):
            raise AssertionError(
                f"the mission client called settimeout({value!r}); the "
                f"protocol requires settimeout(0) on the sim thread"
            )
        sock.setblocking(False)
        return 1

    def connect(self, handle, host, port):
        sock = self._sock(handle)
        if sock is None:
            return (None, "closed")
        self.connected_to.append((str(host), int(port)))
        code = sock.connect_ex((str(host), int(port)))
        if code == 0 or code in _ALREADY_CONNECTED:
            return (1, None)
        if code in _IN_FLIGHT:
            return (None, "timeout")
        if code in (errno.ECONNREFUSED, 10061):
            return (None, "connection refused")
        return (None, pysocket.errno.errorcode.get(code, str(code)))

    def getpeername(self, handle):
        sock = self._sock(handle)
        if sock is None:
            return (None, "closed")
        try:
            host, port = sock.getpeername()[:2]
        except OSError:
            return (None, "getpeername failed")
        return (host, port)

    def getsockname(self, handle):
        sock = self._sock(handle)
        if sock is None:
            return (None, "closed")
        try:
            host, port = sock.getsockname()[:2]
        except OSError:
            return (None, "getsockname failed")
        return (host, port)

    def setoption(self, handle, option=None, value=None):
        sock = self._sock(handle)
        if sock is None:
            return (None, "closed")
        if option == "tcp-nodelay":
            sock.setsockopt(pysocket.IPPROTO_TCP, pysocket.TCP_NODELAY, 1)
        return 1

    def send(self, handle, data, i=None, j=None):
        """LuaSocket send: returns the index of the last byte written."""
        sock = self._sock(handle)
        payload = self._as_bytes(data)
        n = len(payload)

        def absolute(idx, default):
            if idx is None:
                return default
            idx = int(idx)
            if idx < 0:
                idx = n + idx + 1
            return idx

        start = max(1, absolute(i, 1))
        end = min(n, absolute(j, n))
        if sock is None:
            return (None, "closed", start - 1)
        if end < start:
            return (end, None, None)

        chunk = payload[start - 1 : end]
        if self.send_limit is not None:
            chunk = chunk[: self.send_limit]
        try:
            sent = sock.send(chunk)
        except BlockingIOError:
            return (None, "timeout", start - 1)
        except OSError:
            return (None, "closed", start - 1)
        if sent >= (end - start + 1):
            return (end, None, None)
        # Short write: nil, "timeout", and the absolute index of the last
        # byte that made it.
        return (None, "timeout", start + sent - 1)

    def receive(self, handle, pattern=None):
        """LuaSocket receive(n): nil, "timeout", partial when short."""
        sock = self._sock(handle)
        if sock is None:
            return (None, "closed", "")
        if pattern is None:
            raise AssertionError("dcsmock only implements receive(<count>)")
        want = int(pattern)
        try:
            data = sock.recv(want)
        except BlockingIOError:
            return (None, "timeout", b"")
        except OSError:
            return (None, "closed", b"")
        if data == b"":
            return (None, "closed", b"")
        if len(data) < want:
            return (None, "timeout", data)
        return (data, None, None)

    def close(self, handle):
        sock = self._socks.pop(int(handle), None)
        if sock is not None:
            sock.close()
            self.closed += 1
        return 1

    def readable(self, handle):
        sock = self._sock(handle)
        if sock is None:
            return False
        r, _, x = select.select([sock], [], [sock], 0)
        return bool(r or x)

    def writable(self, handle):
        sock = self._sock(handle)
        if sock is None:
            return False
        # LuaSocket folds the exception set into the writable set, which is
        # how a refused connect surfaces on Windows. The client then proves
        # the connection with getpeername, so reproducing the fold is what
        # makes that code path reachable at all.
        _, w, x = select.select([], [sock], [sock], 0)
        return bool(w or x)

    def gettime(self):
        return float(self._clock())

    def close_all(self) -> None:
        for handle in list(self._socks):
            self.close(handle)


def lua_to_py(value: Any, _depth: int = 0) -> Any:
    """Deep-convert a Lua value. A 1..n keyed table becomes a list.

    Object graphs in the mock are cyclic (a unit knows its group, a group
    knows its units), so conversion stops at a depth no protocol payload
    reaches and never follows a key the client did not put there.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if not hasattr(value, "items"):
        # A Lua function, or a Python object that came back out again.
        return value
    if _depth > 24:
        raise ValueError("lua_to_py: table nests deeper than any wire payload")
    items = {}
    for key, val in value.items():
        if isinstance(key, str) and key.startswith("__"):
            # Mock bookkeeping (back-references, handles). Never wire data.
            continue
        items[key] = lua_to_py(val, _depth + 1)
    keys = list(items)
    if keys and all(isinstance(k, int) for k in keys):
        if sorted(keys) == list(range(1, len(keys) + 1)):
            return [items[k] for k in sorted(keys)]
    return items


class DCSMock:
    """One DCS mission environment with the real client loaded into it."""

    def __init__(
        self,
        *,
        port: int,
        host: str = "127.0.0.1",
        tick: float = 1.0,
        config: dict[str, Any] | None = None,
        autostart: bool = True,
    ) -> None:
        if LuaRuntime is None:
            raise RuntimeError(
                "lupa is not installed; tests/dcsmock needs a Lua 5.1 runtime"
            )
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self._load_chunk = self.lua.eval(f"function(...) {_LOADER} end")
        self.sockets = LuaSocketBridge(lambda: self.time)

        self.env = self._dofile(HERE / "dcs_env.lua")

        shim = self._dofile(HERE / "socket_shim.lua", self.sockets)
        self.lua.eval("function(mod) package.loaded.socket = mod end")(shim)

        settings = {"host": host, "port": port, "tick": tick}
        if config:
            settings.update(config)
        self.config = settings
        self.lua.globals()["CAMPAIGN_CLIENT_CONFIG"] = self.lua.table_from(settings)

        self.json = self._dofile(MISSION / "json.lua")
        self.client = None
        if autostart:
            self.load_client()

    # -- loading -----------------------------------------------------------

    def _dofile(self, path: Path, *args: Any) -> Any:
        source = path.read_text(encoding="utf-8")
        chunk = self._load_chunk(source, "@" + path.name)
        return chunk(*args)

    def set_mission_date(self, year: int, month: int, day: int, start_time: int) -> None:
        """Set env.mission's date, which is where `mission_start_epoch` comes
        from. Call before load_client(); the client reads it at hello."""
        self.env.mission.date.Year = year
        self.env.mission.date.Month = month
        self.env.mission.date.Day = day
        self.env.mission.start_time = start_time

    def load_client(self) -> Any:
        """Load mission/campaign_client.lua. It starts itself, as in DCS."""
        self.client = self._dofile(MISSION / "campaign_client.lua")
        return self.client

    # -- clock -------------------------------------------------------------

    @property
    def time(self) -> float:
        return float(self.env.time)

    def advance(self, dt: float) -> float:
        """Move mission time by `dt` and run whatever that makes due."""
        return float(self.env.advance(dt))

    # -- world -------------------------------------------------------------

    def add_player(
        self,
        unit_name: str,
        player_name: str,
        side: int = 2,
        pos: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ):
        return self.env.add_player(unit_name, player_name, side, pos[0], pos[1], pos[2])

    def move_unit(self, unit_name: str, pos: tuple[float, float, float]) -> bool:
        return bool(self.env.move_unit(unit_name, pos[0], pos[1], pos[2]))

    def group(self, name: str):
        return self.env.groups[name]

    @staticmethod
    def _names(registry) -> list[str]:
        return sorted(str(k) for k in registry.keys())

    def group_names(self) -> list[str]:
        return self._names(self.env.groups)

    def static_names(self) -> list[str]:
        return self._names(self.env.statics)

    def unit_names(self) -> list[str]:
        return self._names(self.env.units)

    def group_data(self, name: str) -> dict[str, Any] | None:
        """The table the client handed coalition.addGroup, as plain Python."""
        group = self.env.groups[name]
        if group is None:
            return None
        return lua_to_py(group["__data"])

    def spawn_calls(self) -> list[dict[str, Any]]:
        return lua_to_py(self.env.spawn_calls) or []

    def waypoint_tasks(self, name: str) -> list[list[dict[str, Any]]]:
        """Per-waypoint task lists for a spawned group, waypoint order kept."""
        data = self.group_data(name)
        if not data:
            return []
        points = data.get("route", {}).get("points", [])
        if isinstance(points, dict):
            points = [points[k] for k in sorted(points)]
        out: list[list[dict[str, Any]]] = []
        for point in points:
            tasks = point.get("task", {}).get("params", {}).get("tasks", [])
            if isinstance(tasks, dict):
                tasks = [tasks[k] for k in sorted(tasks)] if tasks else []
            out.append(list(tasks))
        return out

    def attack_tasks(self, name: str) -> list[dict[str, Any]]:
        """Every task attached anywhere on the group's route, flattened."""
        return [task for tasks in self.waypoint_tasks(name) for task in tasks]

    def call(self, obj, method: str, *args: Any) -> Any:
        """`obj:method(...)`. Lua's colon call is not implicit from Python."""
        return obj[method](obj, *args)

    def group_id(self, name: str) -> int | None:
        group = self.env.groups[name]
        return None if group is None else int(self.call(group, "getID"))

    def static_point(self, name: str) -> tuple[float, float, float] | None:
        obj = self.env.statics[name]
        if obj is None:
            return None
        point = self.call(obj, "getPoint")
        return (float(point.x), float(point.y), float(point.z))

    def kill_unit(self, unit_name: str) -> bool:
        return bool(self.env.kill_unit(unit_name))

    def load_ammo(self, unit_type: str, entries: list[tuple[str, int, int]]) -> None:
        """Every unit of `unit_type` built from now on carries `entries`.

        Each entry is (DCS typeName, count, desc.category); category 0 is the
        gun's shells. Units already built keep what they have.
        """
        table = self.lua.table_from(
            [
                self.lua.table_from(
                    {
                        "count": count,
                        "desc": self.lua.table_from(
                            {"typeName": type_name, "category": category}
                        ),
                    }
                )
                for type_name, count, category in entries
            ]
        )
        self.env.ammo_by_type[unit_type] = table

    def fire_weapon(self, unit_name: str, type_name: str, count: int = 1) -> int:
        """Take `count` rounds of `type_name` off one unit; return how many."""
        return int(self.env.fire_weapon(unit_name, type_name, count))

    def fail_get_ammo(self, unit_name: str, fail: bool = True) -> None:
        """Make one unit's getAmmo raise, as a stale or broken handle does."""
        self.env.ammo_raises[unit_name] = True if fail else None

    def without_get_ammo(self) -> None:
        """Build units from now on with no getAmmo at all."""
        self.env.no_get_ammo = True

    def kill_group(self, name: str) -> bool:
        return bool(self.env.kill_group(name))

    def kill_static(self, name: str) -> bool:
        return bool(self.env.kill_static(name))

    def fire_event(self, event_id: int, **fields: Any):
        payload = {"id": event_id, "time": self.time}
        payload.update(fields)
        return self.env.fire_event(self.lua.table_from(payload))

    def event_id(self, name: str) -> int:
        return int(self.lua.globals().world.event[name])

    def unit(self, unit_name: str):
        return self.env.units[unit_name]

    def static(self, name: str):
        return self.env.statics[name]

    # -- observation -------------------------------------------------------

    def logs(self, level: str | None = None) -> list[str]:
        entries = lua_to_py(self.env.logs) or []
        if isinstance(entries, dict):
            entries = [entries[k] for k in sorted(entries)]
        return [
            e["msg"] for e in entries if level is None or e["level"] == level
        ]

    def messages(self) -> list[dict[str, Any]]:
        entries = lua_to_py(self.env.messages) or []
        if isinstance(entries, dict):
            entries = [entries[k] for k in sorted(entries)]
        return list(entries)

    def scheduler_errors(self) -> list[str]:
        entries = lua_to_py(self.env.sched_errors) or []
        if isinstance(entries, dict):
            entries = [entries[k] for k in sorted(entries)]
        return list(entries)

    def status(self) -> dict[str, Any]:
        return lua_to_py(self.client.status())

    def fail_next_group_spawn(self, silently: bool = True) -> None:
        self.env.fail_add_group = True
        self.env.fail_add_group_silently = silently

    def allow_group_spawns(self) -> None:
        self.env.fail_add_group = False

    def fail_static_named(self, name: str | None, silently: bool = True) -> None:
        """Make one object of a multi-object static template fail to build."""
        self.env.fail_static_named = name
        self.env.fail_add_static_silently = silently

    # -- teardown ----------------------------------------------------------

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.stop()
            except Exception:  # pragma: no cover - teardown must not mask
                pass
        self.sockets.close_all()


__all__ = ["DCSMock", "LuaSocketBridge", "lua_to_py", "MISSION", "REPO"]
