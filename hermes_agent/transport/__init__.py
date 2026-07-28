"""L5 Transport layer (spec §3).

Transport-agnostic frame envelope: an incoming JSON frame produces a
handshake or dispatchable request; outgoing frames are handshake payloads
or dispatch responses. Concrete adapters (WebSocket, stdio, HTTP) map their
own wire format to the ``Transport`` protocol and delegate to the
``TransportRouter`` for routing.

The initial concrete impl is ``InMemoryTransport``, which lets the whole
5-layer stack run against pytest without touching a socket. WebSocket +
stdio adapters land in future phases once the runtime story is chosen.
"""

from __future__ import annotations

from hermes_agent.transport.envelope import FrameEnvelope, InboundFrame, OutboundFrame
from hermes_agent.transport.protocol import Transport
from hermes_agent.transport.router import TransportRouter
from hermes_agent.transport.in_memory import InMemoryTransport
from hermes_agent.transport.stdio import StdioTransport

__all__ = [
    "FrameEnvelope",
    "InMemoryTransport",
    "InboundFrame",
    "OutboundFrame",
    "StdioTransport",
    "Transport",
    "TransportRouter",
]
