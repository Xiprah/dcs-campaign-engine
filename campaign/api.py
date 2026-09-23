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

from typing import Protocol, runtime_checkable

from campaign.protocol import (
    Ack,
    Downlink,
    Event,
    Hello,
    ObserverReport,
    StateReport,
)


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
        """Advance the campaign to wall-clock `now` (seconds, monotonic).

        Called on a fixed cadence by the transport, and directly by tests.
        """
        ...

    def on_disconnect(self) -> None:
        """The mission client went away. Campaign state survives."""
        ...
