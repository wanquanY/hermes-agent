"""Transport protocol shared by every concrete adapter (spec §3 L5)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from hermes_agent.transport.envelope import InboundFrame, OutboundFrame


@runtime_checkable
class Transport(Protocol):
    """Bidirectional frame channel.

    Concrete implementations:
    - ``InMemoryTransport`` — in-process queues (tests, mocks)
    - WebSocket adapter (future)
    - stdio adapter (future)
    - HTTP long-poll adapter (future)

    The router-side contract is deliberately synchronous. Async adapters wrap
    their own event loop and translate to this shape.
    """

    def connect(self) -> None: ...

    def close(self) -> None: ...

    def send(self, frame: OutboundFrame) -> None:
        """Emit a frame to the peer."""

    def receive(self) -> InboundFrame | None:
        """Return the next inbound frame or ``None`` if the channel is idle/closed."""

    def is_connected(self) -> bool: ...
