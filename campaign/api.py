"""The seam between campaign logic and the network.

The campaign is a pure function of inbound frames and elapsed time onto
outbound frames. It never touches a socket, a clock, or a random seed it does
not own. That is what makes a whole war replayable from a log, and testable
without DCS running.

The transport layer (`server.py`) owns sockets and calls into this. The
offline harness (`tools/fake_dcs.py`) drives the same interface from the other
side.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from campaign.protocol import (
    Ack,
    Downlink,
    Event,
    Hello,
    ObserverReport,
    StateReport,
)

#: Campaign seconds in one offline paper step. With no mission client
#: connected, the war moves in steps of exactly this size and no other, so the
#: state after N steps is a pure function of the start state and N however the
#: wall clock happened to deliver them (docs/design.md, section 2).
#:
#: Five seconds because that is the resolution the connected war already has:
#: the observer frame is the campaign's heartbeat while DCS is attached, and
#: it arrives every DEFAULT_OBSERVER_PERIOD (5 s). Offline, a takeoff, a TOT or
#: an RTB lands at most one step late, the same as online. A finer step buys
#: precision the observed war never had; a coarser one lets a flight overfly
#: its TOT, and a day of war is still only 17,280 steps.
#:
#: Changing it changes what a saved war does next, so it is a constant here
#: rather than a per-campaign setting.
PAPER_STEP: Final = 5.0


@runtime_checkable
class CampaignEngine(Protocol):
    """Everything the transport needs from the campaign.

    Every handler returns the frames to send in response, in order. Returning
    an empty list is normal and common. Handlers must not block: anything
    slow belongs in :meth:`tick`, which the transport calls on its own cadence.
    """

    def on_hello(self, msg: Hello) -> list[Downlink]:
        """A mission client connected, possibly a reconnect after a restart.

        Must return a `Sync`, followed by a `Spawn` for everything that should
        currently be live. DCS restarted, so the client has nothing.
        """
        ...

    def on_observer(self, msg: ObserverReport) -> list[Downlink]:
        """Player positions moved. Sole input to the bubble."""
        ...

    def on_event(self, msg: Event) -> list[Downlink]:
        """Attribution hint. Must be safe to drop entirely."""
        ...

    def on_state(self, msg: StateReport) -> list[Downlink]:
        """Ground truth snapshot. The only thing that may record a loss."""
        ...

    def on_ack(self, msg: Ack) -> list[Downlink]:
        """Result of a downlink frame that carried a `ref`."""
        ...

    def tick(self, now: float) -> list[Downlink]:
        """Periodic pulse. `now` is monotonic wall seconds.

        Called on a fixed cadence by the transport, and directly by tests. It
        does not move campaign time: connected, time arrives on frames;
        disconnected, it arrives through :meth:`advance`.
        """
        ...

    def advance(self, dt: float) -> list[Downlink]:
        """Move the war forward `dt` campaign seconds with nobody watching.

        `dt` is a whole number of :data:`PAPER_STEP`. The transport calls this
        only while no mission client is synced, and the engine must refuse it
        (a no-op) while one is: mission time is authoritative then, and an
        engine that advanced itself would outrun the sim.
        """
        ...

    def on_disconnect(self) -> None:
        """The mission client went away. Campaign state survives."""
        ...
