"""Stdio transport — reads JSONL frames from a stream, writes JSONL to another.

Wire format: one JSON object per line.
- Requests:  ``{"id": "...", "method": "...", "params": {...}}``
- Handshake: ``{"type": "handshake", "contractVersion": "3.1", ...}``
- Responses: ``{"id": "...", "result": {...}}``
- Errors:    ``{"id": "...", "error": {"code": "...", ...}}``

A JSON line that fails to parse is dropped and reported via the returned
``InboundFrame(kind=REQUEST, method='')`` — the router then answers with
a MALFORMED_FRAME error envelope. Malformed input does not tear the
connection down.
"""

from __future__ import annotations

import io
import logging
from typing import IO, Any

from hermes_agent.transport.envelope import (
    FrameEnvelope,
    FrameKind,
    InboundFrame,
    OutboundFrame,
)


_logger = logging.getLogger(__name__)


class StdioTransport:
    """Line-delimited JSON transport bound to a pair of streams.

    In production the streams are ``sys.stdin`` / ``sys.stdout``; tests pass
    ``io.StringIO`` or ``BytesIO`` wrapped in TextIOWrapper.
    """

    def __init__(
        self,
        in_stream: IO[str],
        out_stream: IO[str],
        *,
        flush_after_send: bool = True,
    ) -> None:
        self._in = in_stream
        self._out = out_stream
        self._flush = flush_after_send
        self._connected = False

    # Transport protocol -------------------------------------------------

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def send(self, frame: OutboundFrame) -> None:
        wire = FrameEnvelope.to_json(frame)
        self._out.write(wire + "\n")
        if self._flush:
            self._out.flush()

    def receive(self) -> InboundFrame | None:
        line = self._in.readline()
        if not line:
            # Empty string signals EOF for both sys.stdin and StringIO.
            self._connected = False
            return None
        line = line.rstrip("\r\n").strip()
        if not line:
            # Whitespace-only lines are ignored.
            return None
        try:
            return FrameEnvelope.parse(line)
        except ValueError as exc:
            _logger.warning("stdio transport dropped malformed frame: %s", exc)
            # Surface a synthetic frame so the router emits a MALFORMED_FRAME
            # error back to the peer.
            return InboundFrame(kind=FrameKind.REQUEST, id="", method="")

    def is_connected(self) -> bool:
        return self._connected


def make_pipe_transport() -> tuple[StdioTransport, IO[str], IO[str]]:
    """Convenience factory for tests — returns (transport, client_write_stream,
    server_read_from_client).

    The client writes to ``client_write_stream`` (the transport's inbound
    side) and reads from ``server_read_from_client`` (the transport's
    outbound side) — reversed on the peer end.
    """
    inbound = io.StringIO()
    outbound = io.StringIO()
    transport = StdioTransport(inbound, outbound)
    return transport, inbound, outbound
