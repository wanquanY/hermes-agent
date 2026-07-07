"""TransportRouter — glues transport frames to the gateway dispatcher (spec §3)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from hermes_agent.gateway import (
    ErrorCode,
    MethodRegistry,
    PermissionResolver,
    dispatch,
    err,
)
from hermes_agent.gateway.handshake import build_handshake_frame
from hermes_agent.transport.envelope import (
    FrameKind,
    InboundFrame,
    OutboundFrame,
)
from hermes_agent.transport.protocol import Transport


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouterStats:
    handshakes_sent: int
    requests_dispatched: int
    errors_returned: int


class TransportRouter:
    """Route inbound frames from a ``Transport`` into the gateway pipeline.

    Responsibilities:
    * Send the handshake frame as soon as ``start()`` is called (spec §11
      first-frame contract).
    * Turn every inbound REQUEST into a dispatch call and echo the response.
    * Return a canonical ``err(MALFORMED_FRAME)`` envelope when the peer
      sends garbage.
    * Track basic stats for observability.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        registry: MethodRegistry,
        resolver: PermissionResolver,
    ) -> None:
        self._transport = transport
        self._registry = registry
        self._resolver = resolver
        self._handshakes_sent = 0
        self._requests_dispatched = 0
        self._errors_returned = 0

    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the transport and greet the peer with the handshake."""
        if not self._transport.is_connected():
            self._transport.connect()
        self._send_handshake()

    def stop(self) -> None:
        self._transport.close()

    def stats(self) -> RouterStats:
        return RouterStats(
            handshakes_sent=self._handshakes_sent,
            requests_dispatched=self._requests_dispatched,
            errors_returned=self._errors_returned,
        )

    # ------------------------------------------------------------------

    def pump_one(self) -> bool:
        """Handle a single inbound frame if one is available.

        Returns ``True`` when a frame was pulled + processed, ``False`` when
        the transport had nothing to deliver.
        """
        frame = self._transport.receive()
        if frame is None:
            return False
        self._handle_inbound(frame)
        return True

    def pump_until_idle(self, max_frames: int = 1024) -> int:
        processed = 0
        while processed < max_frames and self.pump_one():
            processed += 1
        return processed

    # ------------------------------------------------------------------

    def _handle_inbound(self, frame: InboundFrame) -> None:
        if frame.kind is FrameKind.HANDSHAKE:
            # Peer echoed the greeting; nothing to do server-side.
            return
        if frame.kind is FrameKind.REQUEST:
            if not frame.method:
                self._send_error(
                    frame.id,
                    ErrorCode.MALFORMED_FRAME,
                    "request frame missing method",
                )
                return
            self._dispatch(frame)
            return
        # RESPONSE / ERROR inbound to the server side is unexpected — drop.
        _logger.debug(
            "TransportRouter: ignoring unexpected %s inbound frame", frame.kind
        )

    def _dispatch(self, frame: InboundFrame) -> None:
        wire_request = {
            "id": frame.id,
            "method": frame.method,
            "params": frame.params,
        }
        response = dispatch(self._registry, wire_request, resolver=self._resolver)
        if "error" in response:
            self._errors_returned += 1
            self._transport.send(
                OutboundFrame(
                    kind=FrameKind.ERROR,
                    id=response.get("id") or frame.id,
                    error=response["error"],
                )
            )
            return
        self._requests_dispatched += 1
        self._transport.send(
            OutboundFrame(
                kind=FrameKind.RESPONSE,
                id=response.get("id") or frame.id,
                result=response.get("result"),
            )
        )

    def _send_handshake(self) -> None:
        payload = build_handshake_frame()
        # build_handshake_frame returns the full payload dict; the router
        # wraps it as a handshake OutboundFrame (kind + extra top-level keys).
        self._transport.send(
            OutboundFrame(
                kind=FrameKind.HANDSHAKE,
                payload={k: v for k, v in payload.items() if k != "type"},
            )
        )
        self._handshakes_sent += 1

    def _send_error(self, request_id: str, code: ErrorCode, message: str) -> None:
        envelope = err(request_id, code, message)
        self._errors_returned += 1
        self._transport.send(
            OutboundFrame(
                kind=FrameKind.ERROR,
                id=envelope.get("id", ""),
                error=envelope["error"],
            )
        )
