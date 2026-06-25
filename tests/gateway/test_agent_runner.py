"""Unit tests for ``tui_gateway.services.agent_runner`` — Phase 5c.2.

Most of the runner's logic is integration-shaped (touches the real
``tui_gateway.server`` module + agent build), so these tests focus on
the parts that ARE unit-testable in isolation:

- ``_NoopTransport.write`` returns True (write_json / _emit treat that
  as "delivered")
- ``setup_worker_environment`` is idempotent across calls
- ``setup_worker_environment`` replaces ``server._stdio_transport``
  with a ``_NoopTransport`` so any stray legacy write_json becomes
  a silent no-op instead of leaking a JSON-RPC envelope onto the
  protocol pipe

End-to-end behavior (RunStartFrame → message.complete EventFrame
arriving on the supervisor) is dev-app validated, not unit tested —
the prompt path drags in the full LLM stack."""

from __future__ import annotations

import pytest

from tui_gateway.run_worker import RunStartFrame
from tui_gateway.services.agent_runner import (
    _NoopTransport,
    setup_worker_environment,
)


def test_noop_transport_write_returns_true() -> None:
    t = _NoopTransport()
    assert t.write({"jsonrpc": "2.0", "method": "event", "params": {}}) is True
    assert t.is_connected() is True


def test_setup_worker_environment_neutralizes_stdio_transport() -> None:
    setup_worker_environment()
    from tui_gateway import server as _server
    assert isinstance(_server._stdio_transport, _NoopTransport)
    # Calling write_json with anything must succeed against the noop.
    assert _server.write_json({"jsonrpc": "2.0", "method": "event", "params": {}}) is True


def test_setup_worker_environment_is_idempotent() -> None:
    setup_worker_environment()
    from tui_gateway import server as _server
    first = _server._stdio_transport
    setup_worker_environment()
    second = _server._stdio_transport
    # Same instance — second call must NOT replace it again.
    assert first is second


def test_run_start_frame_carries_all_fields_for_runner() -> None:
    """Smoke test that the protocol frame contract has every field the
    runner reads. Catches accidental removals in the protocol."""
    frame = RunStartFrame(
        run_id="r1",
        turn_id="t1",
        stored_session_id="20260625_120000_abcdef",
        prompt="hello",
        params={"runtime_scope_key": "profile:test", "cwd": "/tmp"},
    )
    assert frame.stored_session_id
    assert frame.run_id
    assert frame.turn_id
    assert isinstance(frame.params, dict)
