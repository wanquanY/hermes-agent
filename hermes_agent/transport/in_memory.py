"""In-memory transport — tests + wiring smoke checks."""

from __future__ import annotations

from collections import deque
from typing import Any

from hermes_agent.transport.envelope import (
    FrameEnvelope,
    InboundFrame,
    OutboundFrame,
)


class InMemoryTransport:
    """A pair of FIFO queues playing the role of a bidirectional channel.

    Test code pushes wire dicts / JSON into ``inbound`` via ``client_send``
    and reads server-emitted frames from ``sent`` via ``pop_sent``.
    """

    def __init__(self) -> None:
        self._inbound: deque[InboundFrame] = deque()
        self._sent: list[OutboundFrame] = []
        self._connected = False

    # Transport protocol -------------------------------------------------

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def send(self, frame: OutboundFrame) -> None:
        self._sent.append(frame)

    def receive(self) -> InboundFrame | None:
        if not self._inbound:
            return None
        return self._inbound.popleft()

    def is_connected(self) -> bool:
        return self._connected

    # Test-side helpers --------------------------------------------------

    def client_send(self, wire: dict[str, Any] | str) -> None:
        """Push a request from the peer's perspective."""
        self._inbound.append(FrameEnvelope.parse(wire))

    def pop_sent(self) -> list[OutboundFrame]:
        """Drain everything the server has emitted since the last call."""
        out = list(self._sent)
        self._sent.clear()
        return out

    def sent_snapshot(self) -> list[OutboundFrame]:
        """Non-destructive snapshot for assertions."""
        return list(self._sent)
