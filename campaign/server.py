"""asyncio TCP transport for the campaign engine.

The engine listens; the mission client connects out (docs/protocol.md explains
why that direction). This module owns every socket in the system and nothing
else: it decodes uplink frames, hands them to a
:class:`campaign.api.CampaignEngine`, and writes back whatever the engine
returns. It knows nothing about airbases, targets or attrition.

Two failure modes matter more than anything else here:

* A malformed frame is fatal to the *connection* only. The campaign survives.
* A handler raising is fatal to *nothing*. A campaign brain that dies mid-sortie
  is the worst outcome available, so handler exceptions are logged and the
  server keeps running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from typing import Final

from campaign.api import CampaignEngine
from campaign.protocol import (
    Ack,
    Downlink,
    Event,
    FrameBuffer,
    Hello,
    ObserverReport,
    ProtocolError,
    StateReport,
    Uplink,
    decode_uplink,
    encode,
)

logger = logging.getLogger("campaign.server")

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 7777

#: How often :meth:`CampaignEngine.tick` is called, in seconds.
DEFAULT_TICK_PERIOD: Final = 1.0

#: Deliberately far below MAX_FRAME_BYTES: FrameBuffer rejects a *buffer* over
#: the cap, not a frame, so small reads keep a burst of legal frames legal.
_READ_CHUNK: Final = 8192

_HANDLERS: Final[dict[type, str]] = {
    Hello: "on_hello",
    ObserverReport: "on_observer",
    Event: "on_event",
    StateReport: "on_state",
    Ack: "on_ack",
}


class _Connection:
    """One mission-client socket, and the state that dies with it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.peer = str(writer.get_extra_info("peername") or "?")
        self.alive = True
        self.closed = asyncio.Event()
        self.write_lock = asyncio.Lock()

    def close(self) -> None:
        self.alive = False
        with contextlib.suppress(Exception):
            self.writer.close()


class CampaignServer:
    """Serves one mission client at a time over newline-delimited JSON."""

    def __init__(
        self,
        engine: CampaignEngine,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        tick_period: float = DEFAULT_TICK_PERIOD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._host = host
        self._port = port
        self._tick_period = tick_period
        self._clock = clock
        self._server: asyncio.Server | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._conn: _Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def port(self) -> int:
        """The bound port, which differs from the requested one when it was 0."""
        return self._port

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def start(self) -> None:
        """Bind, listen, and start the tick task. Returns once accepting."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._on_client, self._host, self._port)
        sockets = self._server.sockets or ()
        if sockets:
            self._port = sockets[0].getsockname()[1]
        self._tick_task = asyncio.create_task(self._tick_loop(), name="campaign-tick")
        logger.info("listening on %s:%d", self._host, self._port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def close(self) -> None:
        """Shut down: no task, socket or engine callback left outstanding."""
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        conn = self._conn
        if conn is not None:
            conn.close()
            await conn.closed.wait()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> CampaignServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- connection handling ----------------------------------------------

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = _Connection(reader, writer)
        previous = self._conn
        if previous is not None:
            # A DCS that crashed can leave a half-open socket the OS has not
            # reaped yet. The newest client is always the real one.
            logger.warning("client %s displaces %s", conn.peer, previous.peer)
            previous.close()
            await previous.closed.wait()
        self._conn = conn
        logger.info("client connected: %s", conn.peer)
        try:
            await self._read_loop(conn)
        except ProtocolError as exc:
            logger.error("protocol error from %s, closing: %s", conn.peer, exc)
        except (ConnectionError, OSError) as exc:
            logger.info("connection to %s lost: %s", conn.peer, exc)
        except asyncio.CancelledError:
            raise
        finally:
            conn.close()
            if self._conn is conn:
                self._conn = None
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.info("client disconnected: %s", conn.peer)
            self._notify_disconnect()
            conn.closed.set()

    async def _read_loop(self, conn: _Connection) -> None:
        buffer = FrameBuffer()
        while True:
            chunk = await conn.reader.read(_READ_CHUNK)
            if not chunk:
                return
            for raw in buffer.feed(chunk):
                if not raw.strip():
                    # A bare newline is not a frame; it is also not worth
                    # dropping a mid-sortie connection over.
                    logger.debug("ignoring empty line from %s", conn.peer)
                    continue
                frame = decode_uplink(raw)
                await self._dispatch(conn, frame)
                if not conn.alive:
                    return

    async def _dispatch(self, conn: _Connection, frame: Uplink) -> None:
        name = _HANDLERS.get(type(frame))
        if name is None:
            raise ProtocolError(f"no handler for {type(frame).__name__}")
        try:
            out = getattr(self._engine, name)(frame)
        except Exception:
            logger.exception("%s raised; campaign continues", name)
            return
        await self._send(conn, out or ())

    def _notify_disconnect(self) -> None:
        try:
            self._engine.on_disconnect()
        except Exception:
            logger.exception("on_disconnect raised; campaign continues")

    # -- writing -----------------------------------------------------------

    async def _send(self, conn: _Connection | None, frames: Sequence[Downlink]) -> None:
        if not frames:
            return
        if conn is None or not conn.alive:
            logger.debug("no client; dropped %d downlink frame(s)", len(frames))
            return
        blob = bytearray()
        for frame in frames:
            try:
                blob += encode(frame)
            except ProtocolError:
                # An unsendable frame is an engine bug. Losing the rest of the
                # batch over it would turn a bug into a broken campaign.
                logger.exception("cannot encode %s, skipping it", type(frame).__name__)
        if not blob:
            return
        async with conn.write_lock:
            if not conn.alive:
                return
            try:
                conn.writer.write(bytes(blob))
                await conn.writer.drain()
            except (ConnectionError, OSError) as exc:
                logger.info("write to %s failed: %s", conn.peer, exc)
                conn.close()

    # -- tick --------------------------------------------------------------

    async def _tick_loop(self) -> None:
        deadline = self._clock()
        while True:
            deadline += self._tick_period
            await asyncio.sleep(max(0.0, deadline - self._clock()))
            try:
                frames = self._engine.tick(self._clock())
            except Exception:
                logger.exception("tick raised; campaign continues")
                continue
            if frames:
                await self._send(self._conn, frames)


async def serve(
    engine: CampaignEngine,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    tick_period: float = DEFAULT_TICK_PERIOD,
) -> None:
    """Run a :class:`CampaignServer` until cancelled."""
    server = CampaignServer(engine, host=host, port=port, tick_period=tick_period)
    try:
        await server.serve_forever()
    finally:
        await server.close()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TICK_PERIOD",
    "CampaignServer",
    "serve",
]
