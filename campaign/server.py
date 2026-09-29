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

#: Ceiling on waiting for a torn-down connection's handler to finish. The abort
#: in `_Connection.close` should make this a formality; the bound exists so that
#: a handler wedged for any other reason cannot lock out the reconnecting sim,
#: or the shutdown that persists the campaign, indefinitely.
_CLOSE_TIMEOUT: Final = 5.0

def _elapsed_clock() -> Callable[[], float]:
    """Monotonic seconds since the server started, not since the host booted.

    `api.CampaignEngine.tick` asks for monotonic seconds and says nothing about
    the origin. `time.monotonic()` on Windows counts from boot, so an engine
    that folds `tick(now)` into the same clock as a frame's mission-time `t`
    would be shoved years into the future by the first tick. Starting at zero
    keeps both inputs on the same scale and is still monotonic.
    """
    start = time.monotonic()

    def now() -> float:
        return time.monotonic() - start

    return now


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
        #: Set once this socket's `hello` has reached the engine. Nothing may
        #: be written before that: the engine restarts its downlink stream at
        #: every hello, so a frame sent ahead of one is a frame outside every
        #: stream. See `_send`.
        self.synced = False
        self.closed = asyncio.Event()
        self.write_lock = asyncio.Lock()

    def close(self) -> None:
        self.alive = False
        with contextlib.suppress(Exception):
            # abort(), not close(): close() defers `connection_lost` until the
            # write buffer drains, so a DCS that wedged with bytes outstanding
            # would never reach it -- and nothing that waits on `closed` would
            # ever be released. This socket is being torn down, not flushed.
            self.writer.transport.abort()


class CampaignServer:
    """Serves one mission client at a time over newline-delimited JSON."""

    def __init__(
        self,
        engine: CampaignEngine,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        tick_period: float = DEFAULT_TICK_PERIOD,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._engine = engine
        self._host = host
        self._port = port
        self._tick_period = tick_period
        self._clock = clock if clock is not None else _elapsed_clock()
        self._server: asyncio.Server | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._conn: _Connection | None = None
        self._serving: asyncio.Future[None] | None = None

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
        """Accept clients until cancelled, tearing everything down on the way out.

        Deliberately not a passthrough to `asyncio.Server.serve_forever()`.
        That one answers cancellation by awaiting `wait_closed()`, which since
        3.12 waits for every live connection handler to finish -- and ours only
        finishes when the client socket dies, which is exactly what the caller's
        `finally` was going to do. Ctrl-C with DCS attached therefore hung
        forever and the campaign was never persisted. Owning the wait here means
        cancellation runs the teardown instead of deadlocking against it.
        """
        if self._serving is not None:
            raise RuntimeError("serve_forever() is already running on this server")
        if self._server is None:
            await self.start()
        self._serving = asyncio.get_running_loop().create_future()
        try:
            await self._serving
        except asyncio.CancelledError:
            await self.close()
            raise
        finally:
            self._serving = None

    async def close(self) -> None:
        """Shut down: no task, socket or engine callback left outstanding."""
        if self._serving is not None and not self._serving.done():
            self._serving.set_result(None)
        if self._tick_task is not None:
            self._tick_task.cancel()
            # A tick task that already died of its own exception must not take
            # the shutdown -- and the campaign save that follows it -- with it.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._tick_task
            self._tick_task = None
        conn = self._conn
        if conn is not None:
            conn.close()
            try:
                await asyncio.wait_for(conn.closed.wait(), _CLOSE_TIMEOUT)
            except TimeoutError:
                logger.error("%s did not finish closing; shutting down anyway", conn.peer)
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                # Bounded for the same reason as everything else here: since
                # 3.12 `wait_closed()` waits for every connection handler, and
                # the campaign save happens after this returns. Shutdown must
                # not be hostage to a handler that will not let go.
                await asyncio.wait_for(self._server.wait_closed(), _CLOSE_TIMEOUT)
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
            try:
                await asyncio.wait_for(previous.closed.wait(), _CLOSE_TIMEOUT)
            except TimeoutError:
                # The reconnecting sim is the one that matters. A predecessor
                # whose handler will not let go is a bug to log, not a reason to
                # leave DCS talking to nothing.
                logger.error("%s did not finish closing; proceeding", previous.peer)
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
                # Bounded for the same reason the close is an abort: a transport
                # with bytes still queued to a client that stopped reading never
                # reports itself closed, and this handler is what everything
                # else waits on.
                await asyncio.wait_for(writer.wait_closed(), _CLOSE_TIMEOUT)
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
                try:
                    frame = decode_uplink(raw)
                except ProtocolError:
                    raise
                except Exception as exc:
                    # protocol.py means to raise nothing but ProtocolError, but
                    # it is not the place a decoder bug should cost the engine
                    # its connection handling. Undecodable is undecodable.
                    raise ProtocolError(f"undecodable frame: {exc!r}") from exc
                await self._dispatch(conn, frame)
                if not conn.alive:
                    return

    async def _dispatch(self, conn: _Connection, frame: Uplink) -> None:
        name = _HANDLERS.get(type(frame))
        if name is None:
            raise ProtocolError(f"no handler for {type(frame).__name__}")
        try:
            out = getattr(self._engine, name)(frame)
        except ProtocolError:
            # docs/protocol.md: the engine closes the connection on a protocol
            # mismatch rather than negotiating. The engine states that verdict by
            # raising, so it has to reach `_on_client` instead of being logged as
            # one more survivable handler crash -- otherwise an incompatible
            # client keeps driving the campaign it was just refused by.
            raise
        except Exception:
            logger.exception("%s raised; campaign continues", name)
            return
        if isinstance(frame, Hello):
            # The engine has now rebased this connection's stream, so the
            # reply and everything after it may go out. Set before the send,
            # because the `sync` is itself one of the gated frames.
            conn.synced = True
        try:
            await self._send(conn, out or ())
        except Exception:
            logger.exception("sending the reply to %s failed; campaign continues", name)

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
        if not conn.synced:
            # An accepted socket is not yet a client. The mission client starts
            # a non-blocking connect on one tick and sends `hello` on the next,
            # so there is always a window where the TCP connection exists and
            # the engine has not been told about it -- and a tick landing in
            # that window would write frames from the *previous* stream: their
            # seq and ref are about to be reset by `on_hello`, and a spawn
            # issued there is re-issued by the hello that follows it, which the
            # protocol obliges the client to refuse as a duplicate spawn_id.
            # The engine then blocks that entity for the rest of the campaign.
            # Dropping is safe for the same reason dropping with no client at
            # all is: `hello` re-issues everything that should be live.
            logger.debug("%s has not said hello; dropped %d frame(s)",
                         conn.peer, len(frames))
            return
        blob = bytearray()
        for frame in frames:
            try:
                blob += encode(frame)
            except Exception:
                # An unsendable frame is an engine bug. Losing the rest of the
                # batch over it would turn a bug into a broken campaign, and it
                # need not be a ProtocolError: a non-dataclass or a value JSON
                # cannot serialise comes out of `encode` as a TypeError.
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
                try:
                    await self._send(self._conn, frames)
                except Exception:
                    # The clock is the campaign's heartbeat. Nothing watches this
                    # task, so an exception escaping here stops time silently.
                    logger.exception("sending tick output failed; campaign continues")


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
