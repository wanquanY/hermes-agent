"""L5 stdio transport (spec §3 L5)."""

from __future__ import annotations

import io
import json

from hermes_agent.gateway import (
    AllowAllResolver,
    MethodRegistry,
    requires_permission,
)
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.transport import (
    StdioTransport,
    TransportRouter,
)
from hermes_agent.transport.envelope import FrameEnvelope, FrameKind


@requires_permission("test.read", read_only=True)
def _echo_handler(params, ctx: DispatchContext):
    return {"echoed": params, "method": ctx.method}


def _wired(inbound: io.StringIO, outbound: io.StringIO):
    reg = MethodRegistry()
    reg.register("echo", _echo_handler)
    transport = StdioTransport(inbound, outbound)
    router = TransportRouter(
        transport=transport,
        registry=reg,
        resolver=AllowAllResolver(),
    )
    return transport, router


def _client_send(stream: io.StringIO, wire: dict) -> None:
    """Simulate client writing a JSON line to the transport's inbound stream."""
    stream.write(json.dumps(wire) + "\n")


def _read_lines(stream: io.StringIO) -> list[dict]:
    stream.seek(0)
    lines = [line.strip() for line in stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------


def test_stdio_transport_sends_handshake_on_start():
    inbound = io.StringIO()
    outbound = io.StringIO()
    transport, router = _wired(inbound, outbound)
    router.start()
    lines = _read_lines(outbound)
    assert len(lines) == 1
    assert lines[0]["type"] == "handshake"
    assert lines[0]["contractVersion"] == "3.1"


def test_stdio_transport_request_response_roundtrip():
    inbound = io.StringIO()
    outbound = io.StringIO()

    _client_send(inbound, {"id": "r1", "method": "echo", "params": {"k": "v"}})
    inbound.seek(0)  # rewind so router.receive() reads from the top

    transport, router = _wired(inbound, outbound)
    router.start()
    router.pump_one()

    lines = _read_lines(outbound)
    # First frame is handshake, second is the response.
    assert lines[0]["type"] == "handshake"
    resp = lines[1]
    assert resp["type"] == "response"
    assert resp["id"] == "r1"
    assert resp["result"]["echoed"] == {"k": "v"}


def test_stdio_transport_pumps_many_requests():
    inbound = io.StringIO()
    outbound = io.StringIO()
    for i in range(4):
        _client_send(inbound, {"id": f"r{i}", "method": "echo", "params": {"i": i}})
    inbound.seek(0)

    transport, router = _wired(inbound, outbound)
    router.start()
    processed = router.pump_until_idle()
    assert processed == 4

    lines = _read_lines(outbound)
    # 1 handshake + 4 responses.
    assert len(lines) == 5
    for i, resp in enumerate(lines[1:], start=0):
        assert resp["id"] == f"r{i}"
        assert resp["result"]["echoed"] == {"i": i}


def test_stdio_transport_malformed_frame_becomes_error_envelope():
    inbound = io.StringIO()
    outbound = io.StringIO()
    inbound.write("this is not json\n")
    inbound.seek(0)

    transport, router = _wired(inbound, outbound)
    router.start()
    router.pump_one()

    lines = _read_lines(outbound)
    # 1 handshake + 1 error envelope.
    assert len(lines) == 2
    err_frame = lines[1]
    assert err_frame["type"] == "error"
    assert err_frame["error"]["code"] == "4006"  # MALFORMED_FRAME


def test_stdio_transport_empty_line_is_skipped():
    inbound = io.StringIO()
    outbound = io.StringIO()
    inbound.write("\n\n\n")
    inbound.seek(0)

    transport, router = _wired(inbound, outbound)
    transport.connect()
    # receive() should return None or a no-op, not blow up.
    frame = transport.receive()
    # Empty line yields None (whitespace-only skip).
    assert frame is None


def test_stdio_transport_eof_returns_none_and_marks_disconnected():
    inbound = io.StringIO()
    outbound = io.StringIO()
    transport = StdioTransport(inbound, outbound)
    transport.connect()
    assert transport.is_connected()
    assert transport.receive() is None
    assert not transport.is_connected()


def test_stdio_transport_send_flushes_immediately():
    """Regression — responses must be flushed so a peer using line-buffered
    reads sees them without waiting on a buffer flush.
    """
    inbound = io.StringIO()
    outbound = io.StringIO()
    transport = StdioTransport(inbound, outbound, flush_after_send=True)
    from hermes_agent.transport.envelope import OutboundFrame

    transport.send(
        OutboundFrame(kind=FrameKind.RESPONSE, id="x", result={"ok": True})
    )
    output = outbound.getvalue()
    # to_json uses sort_keys=True but json.dumps default separator has a space
    # after ':'/','. Check for canonical structure without over-committing.
    parsed = json.loads(output.strip())
    assert parsed == {"id": "x", "result": {"ok": True}, "type": "response"}


def test_stdio_transport_line_parsing_ignores_trailing_crlf():
    """Frames with CRLF endings (Windows) still parse."""
    inbound = io.StringIO()
    outbound = io.StringIO()
    inbound.write('{"id":"r1","method":"echo","params":{}}\r\n')
    inbound.seek(0)

    transport, router = _wired(inbound, outbound)
    router.start()
    router.pump_one()

    lines = _read_lines(outbound)
    assert lines[1]["id"] == "r1"
