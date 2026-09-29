"""Wire contract between the campaign engine and the DCS mission client.

The authoritative specification is docs/protocol.md. This module is the
executable half of it: both the engine and the offline fake-DCS harness encode
and decode through here, so the two cannot drift apart silently.

Frames are newline-delimited JSON. Nothing here touches a socket.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 2

#: Hard cap on a single frame. Longer frames indicate a bug, not a big payload.
MAX_FRAME_BYTES = 64 * 1024

#: Defaults handed to the client in `sync`. The client does not choose these.
DEFAULT_STATE_PERIOD = 30.0
DEFAULT_OBSERVER_PERIOD = 5.0
DEFAULT_BUBBLE_RADIUS = 75_000.0

Coalition = Literal["blue", "red", "neutral"]
Vec3 = tuple[float, float, float]

#: Prefix on every DCS group name the engine owns. Anything without it belongs
#: to the mission file, a client aircraft, or another script, and is ignored.
OWNED_PREFIX = "cmp_"


def group_name(spawn_id: str) -> str:
    """DCS group name for an engine-owned entity."""
    return f"{OWNED_PREFIX}{spawn_id}"


def spawn_id_of(name: object) -> str | None:
    """Inverse of :func:`group_name`; None if the name is not ours.

    Takes anything, because names arrive off the wire and the engine may not
    depend on a client being well-behaved: DCS scenery answers getName with a
    number, and a number is not one of our names.
    """
    if not isinstance(name, str) or not name.startswith(OWNED_PREFIX):
        return None
    return name[len(OWNED_PREFIX):]


class ProtocolError(Exception):
    """A frame that cannot be trusted. Always fatal to the connection."""


# --------------------------------------------------------------------------
# Uplink: mission client -> engine
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observer:
    id: str
    pos: Vec3
    speed: float = 0.0


@dataclass(frozen=True)
class GroupSnapshot:
    """Ground truth for one instantiated entity. See docs/protocol.md."""

    spawn_id: str
    alive: bool
    units: int
    units_initial: int
    pos: Vec3 | None = None


@dataclass(frozen=True)
class Hello:
    seq: int
    t: float
    protocol: int
    theater: str
    dcs_version: str = ""
    mission_start_epoch: int = 0
    type: str = "hello"


@dataclass(frozen=True)
class ObserverReport:
    seq: int
    t: float
    observers: list[Observer] = field(default_factory=list)
    type: str = "observer"


@dataclass(frozen=True)
class Event:
    """A hint about *how* something happened. Never a source of truth.

    The engine must stay correct if every Event is dropped; events may enrich
    a loss with attribution but may not create or withhold one.
    """

    seq: int
    t: float
    kind: str
    initiator: str | None = None
    target: str | None = None
    weapon: str | None = None
    place: str | None = None
    type: str = "event"


@dataclass(frozen=True)
class StateReport:
    seq: int
    t: float
    groups: list[GroupSnapshot] = field(default_factory=list)
    type: str = "state"


@dataclass(frozen=True)
class Ack:
    seq: int
    t: float
    ref: int
    ok: bool
    error: str | None = None
    type: str = "ack"


# --------------------------------------------------------------------------
# Downlink: engine -> mission client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Waypoint:
    pos: Vec3
    alt: float = 0.0
    speed: float = 0.0
    action: str = "turning_point"
    #: DCS airdrome id. On the first waypoint it makes the flight a ramp start
    #: at that airfield rather than an air start. See docs/protocol.md.
    airdrome_id: int | None = None


@dataclass(frozen=True)
class Sync:
    seq: int
    t: float
    campaign_time: float
    protocol: int = PROTOCOL_VERSION
    state_period: float = DEFAULT_STATE_PERIOD
    observer_period: float = DEFAULT_OBSERVER_PERIOD
    bubble_radius: float = DEFAULT_BUBBLE_RADIUS
    type: str = "sync"


@dataclass(frozen=True)
class Spawn:
    seq: int
    t: float
    ref: int
    spawn_id: str
    coalition: Coalition
    category: str
    template: str
    #: Exactly how many units to build. No default, so no emitter can forget
    #: it: the engine owns the airframe ledger, and a count left to the
    #: client's template is how a flight that lost a wingman comes back whole.
    units: int
    position: Vec3
    heading: float = 0.0
    route: list[Waypoint] = field(default_factory=list)
    tasking: dict[str, Any] = field(default_factory=dict)
    type: str = "spawn"


@dataclass(frozen=True)
class Despawn:
    seq: int
    t: float
    ref: int
    spawn_id: str
    reason: str = ""
    type: str = "despawn"


@dataclass(frozen=True)
class Message:
    seq: int
    t: float
    to: str
    text: str
    duration: float = 15.0
    type: str = "message"


Uplink = Hello | ObserverReport | Event | StateReport | Ack
Downlink = Sync | Spawn | Despawn | Message

_UPLINK: dict[str, type] = {
    "hello": Hello,
    "observer": ObserverReport,
    "event": Event,
    "state": StateReport,
    "ack": Ack,
}

_DOWNLINK: dict[str, type] = {
    "sync": Sync,
    "spawn": Spawn,
    "despawn": Despawn,
    "message": Message,
}

_NESTED = {
    "observers": Observer,
    "groups": GroupSnapshot,
    "route": Waypoint,
}

#: Event fields that name things. See :func:`decode_uplink`.
_EVENT_NAMES = ("initiator", "target", "weapon", "place")


def encode(frame: Uplink | Downlink) -> bytes:
    """Serialise one frame, including its trailing newline."""
    raw = json.dumps(asdict(frame), separators=(",", ":")).encode("utf-8")
    if len(raw) + 1 > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(raw)} bytes exceeds MAX_FRAME_BYTES")
    return raw + b"\n"


def _tuple_pos(key: str, value: Any) -> Any:
    """JSON has no tuples; positions round-trip as lists."""
    if key in ("pos", "position") and isinstance(value, list):
        return tuple(value)
    return value


def _build_nested(cls: type, item: Any) -> Any:
    # A member that is not an object, or lacks a field, must fail the whole
    # frame as a ProtocolError. Kept as a raw value it half-decodes; escaping
    # as a TypeError it is only fatal to callers that think to translate it.
    if not isinstance(item, dict):
        raise ProtocolError(f"malformed {cls.__name__}: {item!r} is not an object")
    try:
        return cls(**{k: _tuple_pos(k, v) for k, v in item.items()})
    except TypeError as exc:
        raise ProtocolError(f"malformed {cls.__name__}: {exc}") from exc


def _build(cls: type, payload: dict[str, Any]) -> Any:
    payload.pop("type", None)
    for key, nested in _NESTED.items():
        if key in payload and isinstance(payload[key], list):
            payload[key] = [_build_nested(nested, item) for item in payload[key]]
    payload = {k: _tuple_pos(k, v) for k, v in payload.items()}
    try:
        return cls(**payload)
    except TypeError as exc:
        raise ProtocolError(f"malformed {cls.__name__}: {exc}") from exc


def _payload(raw: bytes | str, table: dict[str, type], direction: str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("frame is not a JSON object")
    kind = payload.get("type")
    if kind not in table:
        raise ProtocolError(f"unknown {direction} frame type: {kind!r}")
    return payload


def _check_state(report: StateReport) -> StateReport:
    """A snapshot is ground truth, so a malformed one is refused outright.

    Nothing here is lenient the way events are. A group reported under a
    spawn_id that is not a string matches nothing the engine tracks, so the
    group it was meant to be reads as absent from the census -- and absence is
    a loss. Guessing would write off a flight that is still flying.
    """
    if not isinstance(report.groups, list):
        raise ProtocolError(f"malformed StateReport: groups is {type(report.groups).__name__}")
    for group in report.groups:
        if not isinstance(group.spawn_id, str):
            raise ProtocolError(
                f"malformed GroupSnapshot: spawn_id {group.spawn_id!r} is not a string"
            )
    return report


def decode_uplink(raw: bytes | str) -> Uplink:
    """Decode a frame sent by the mission client.

    An `event` whose name fields are not strings keeps the frame and loses
    those fields. Events are attribution only, so the worst a junk name can
    cost is one attribution; refusing the frame would make it a ProtocolError,
    and that closes a live connection mid-sortie over something the campaign
    is designed to do without.
    """
    payload = _payload(raw, _UPLINK, "uplink")
    kind = payload["type"]
    if kind == "event":
        for key in _EVENT_NAMES:
            if not isinstance(payload.get(key), (str, type(None))):
                payload[key] = None
    frame = _build(_UPLINK[kind], payload)
    if kind == "state":
        return _check_state(frame)
    return frame


def decode_downlink(raw: bytes | str) -> Downlink:
    """Decode a frame sent by the engine. Used by the fake-DCS harness."""
    payload = _payload(raw, _DOWNLINK, "downlink")
    return _build(_DOWNLINK[payload["type"]], payload)


class FrameBuffer:
    """Reassembles newline-delimited frames from a byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add received bytes, return whatever complete frames that produced."""
        self._buf.extend(chunk)
        if len(self._buf) > MAX_FRAME_BYTES:
            raise ProtocolError("frame exceeds MAX_FRAME_BYTES with no newline")
        frames: list[bytes] = []
        while (idx := self._buf.find(b"\n")) != -1:
            frames.append(bytes(self._buf[:idx]))
            del self._buf[: idx + 1]
        return frames
