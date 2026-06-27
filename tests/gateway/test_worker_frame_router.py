"""Unit tests for ``WorkerFrameRouter``.

The router is plain Python — no subprocess. We inject:
- a fake supervisor that captures ``send()`` calls
- a fake ``publish_event`` that captures payloads
- a fake ``publish_run_terminal`` that captures kwargs

Covers:
- on_event → publish_event 1:1
- on_interactive_request → record pending + publish frontend event
- on_run_terminal cross-fills from record_run_start
- on_run_terminal without record_run_start and no carry-through → drop (log warn)
- respond looks up scope_key + kind → sends InteractiveResponseFrame
- respond rejects on missing request_id
- respond rejects on kind mismatch
- repeated respond returns False
- run.terminal drops stale pending entries for that session
- _infer_stored_session_for_scope picks single active run
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tui_gateway.run_worker import (
    EventFrame,
    InteractiveRequestFrame,
    InteractiveResponseFrame,
    LogFrame,
    RunTerminalFrame,
)
from tui_gateway.services.worker_frame_router import WorkerFrameRouter


class _FakeSupervisor:
    def __init__(self, deliver: bool = True) -> None:
        self.deliver = deliver
        self.sent: list[tuple[str, str, Any]] = []

    async def send(
        self,
        scope_key: str,
        conversation_id: str,
        frame: Any,
    ) -> bool:
        self.sent.append((scope_key, conversation_id, frame))
        return self.deliver


def _make_router(supervisor: _FakeSupervisor = None):
    sup = supervisor or _FakeSupervisor()
    events: list[dict] = []
    terminals: list[dict] = []

    def publish_event(payload: dict) -> Any:
        events.append(payload)
        return []

    def publish_run_terminal(**kwargs: Any) -> dict:
        terminals.append(kwargs)
        return {"published": True}

    router = WorkerFrameRouter(
        sender=sup,
        publish_event=publish_event,
        publish_run_terminal=publish_run_terminal,
    )
    return router, sup, events, terminals


@pytest.mark.asyncio
async def test_on_event_forwards_payload() -> None:
    router, _sup, events, _ = _make_router()
    await router.on_event(
        "profile:x",
        "sess-1",
        EventFrame(params={"type": "message.delta", "text": "hi"}),
    )
    assert events == [
        {
            "type": "message.delta",
            "text": "hi",
            "runtime_scope_key": "profile:x",
            "conversation_id": "sess-1",
        }
    ]


@pytest.mark.asyncio
async def test_on_interactive_request_records_pending_only() -> None:
    """The worker's monkey-patched publish_recorded_event emits the
    public ``{kind}.request`` event via the standard path; the router
    must NOT synthesize a second one here."""
    router, _sup, events, _ = _make_router()
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="clarify",
            request_id="req-1",
            payload={"question": "ok?"},
            stored_session_id="sess-1",
        ),
    )
    assert router.has_pending_request("req-1")
    # No event published — that's the worker's job, not the router's.
    assert events == []
    # But the routing table did capture the stored_session_id.
    snap = router.pending_snapshot()
    assert snap["pendingInteractive"] == [
        {
            "requestId": "req-1",
            "scopeKey": "profile:x",
            "conversationId": "sess-1",
            "kind": "clarify",
            "storedSessionId": "sess-1",
        },
    ]


@pytest.mark.asyncio
async def test_on_interactive_request_cross_fills_stored_session_from_run() -> None:
    """When the worker omits stored_session_id (e.g. an older worker
    build), the router fills it from the active run record so a later
    respond/lookup can still scope correctly."""
    router, _sup, events, _ = _make_router()
    router.record_run_start(
        scope_key="profile:x",
        run_id="run-1",
        stored_session_id="sess-A",
        turn_id="t-1",
    )
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="approval", request_id="req-2", payload={"command": "rm"},
        ),
    )
    # Cross-fill applied to the routing-table entry.
    snap = router.pending_snapshot()
    pending = [e for e in snap["pendingInteractive"] if e["requestId"] == "req-2"]
    assert pending == [
        {
            "requestId": "req-2",
            "scopeKey": "profile:x",
            "conversationId": "sess-A",
            "kind": "approval",
            "storedSessionId": "sess-A",
        },
    ]
    assert events == []


@pytest.mark.asyncio
async def test_on_interactive_request_drops_unknown_kind() -> None:
    router, _sup, events, _ = _make_router()
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(kind="bogus", request_id="req-x", payload={}),
    )
    assert not router.has_pending_request("req-x")
    assert events == []


@pytest.mark.asyncio
async def test_on_run_terminal_skips_publish_on_completed() -> None:
    """For a normal completion the worker's agent already published a
    ``message.complete`` event via the publish hook; the router must
    NOT re-publish here."""
    router, _sup, _events, terminals = _make_router()
    await router.on_run_terminal(
        "profile:x",
        RunTerminalFrame(
            run_id="run-1",
            status="completed",
            stored_session_id="sess-1",
            turn_id="turn-1",
            message="",
        ),
    )
    assert terminals == []  # NOT published


@pytest.mark.asyncio
async def test_on_run_terminal_publishes_on_failed() -> None:
    """A failed/cancelled exit must synthesize a terminal event — the
    agent may have died before its own terminal publish reached the
    pipe."""
    router, _sup, _events, terminals = _make_router()
    await router.on_run_terminal(
        "profile:x",
        RunTerminalFrame(
            run_id="run-1",
            status="failed",
            stored_session_id="sess-1",
            turn_id="turn-1",
            message="kaboom",
        ),
    )
    assert terminals == [
        {
            "stored_session_id": "sess-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:x",
            "runtime_session_id": "sess-1",
            "status": "failed",
            "message": "kaboom",
        }
    ]


@pytest.mark.asyncio
async def test_on_run_terminal_publishes_on_cancelled() -> None:
    router, _sup, _events, terminals = _make_router()
    await router.on_run_terminal(
        "profile:x",
        RunTerminalFrame(
            run_id="run-1",
            status="cancelled",
            stored_session_id="sess-1",
            turn_id="turn-1",
            message="user cancelled",
        ),
    )
    assert len(terminals) == 1
    assert terminals[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_on_run_terminal_cross_fills_from_record_run_start() -> None:
    router, _sup, _events, terminals = _make_router()
    router.record_run_start(
        scope_key="profile:x",
        run_id="run-1",
        stored_session_id="sess-A",
        turn_id="turn-A",
    )
    # Use failed so the publish path actually fires (completed skips publish).
    await router.on_run_terminal(
        "profile:x",
        RunTerminalFrame(run_id="run-1", status="failed"),
    )
    assert terminals[0]["stored_session_id"] == "sess-A"
    assert terminals[0]["turn_id"] == "turn-A"
    # cleanup: run table no longer holds run-1
    snapshot = router.pending_snapshot()
    assert all(r["runId"] != "run-1" for r in snapshot["activeRuns"])


@pytest.mark.asyncio
async def test_on_run_terminal_drops_when_no_stored_session(caplog) -> None:
    router, _sup, _events, terminals = _make_router()
    await router.on_run_terminal(
        "profile:x",
        RunTerminalFrame(run_id="run-orphan", status="failed"),
    )
    assert terminals == []


@pytest.mark.asyncio
async def test_respond_routes_to_correct_worker() -> None:
    router, sup, _events, _ = _make_router()
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="clarify",
            request_id="req-1",
            payload={"question": "?"},
            stored_session_id="sess-1",
        ),
    )
    ok = await router.respond("req-1", "yes", expected_kind="clarify")
    assert ok
    assert len(sup.sent) == 1
    scope, conversation_id, frame = sup.sent[0]
    assert scope == "profile:x"
    assert conversation_id == "sess-1"
    assert isinstance(frame, InteractiveResponseFrame)
    assert frame.kind == "clarify"
    assert frame.request_id == "req-1"
    assert frame.answer == "yes"
    # entry consumed → second respond fails
    assert await router.respond("req-1", "yes") is False


@pytest.mark.asyncio
async def test_respond_rejects_missing_request_id() -> None:
    router, _sup, _events, _ = _make_router()
    assert await router.respond("never-seen", "yes") is False


@pytest.mark.asyncio
async def test_respond_rejects_kind_mismatch() -> None:
    router, sup, _events, _ = _make_router()
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="approval", request_id="req-1", payload={},
            stored_session_id="sess-1",
        ),
    )
    ok = await router.respond("req-1", "yes", expected_kind="clarify")
    assert ok is False
    assert sup.sent == []
    # entry still pending after the rejected respond
    assert router.has_pending_request("req-1")


@pytest.mark.asyncio
async def test_respond_keeps_entry_on_send_failure() -> None:
    sup = _FakeSupervisor(deliver=False)
    router, _sup, _events, _ = _make_router(supervisor=sup)
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="clarify", request_id="req-1", payload={},
            stored_session_id="sess-1",
        ),
    )
    ok = await router.respond("req-1", "yes")
    assert ok is False
    # Worker not reachable — keep pending so a retry can resend.
    assert router.has_pending_request("req-1")


@pytest.mark.asyncio
async def test_run_terminal_clears_stale_pending_for_session() -> None:
    router, _sup, _events, _terminals = _make_router()
    # Pending for sess-A
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="clarify", request_id="req-A1", payload={},
            stored_session_id="sess-A",
        ),
    )
    # Pending for sess-B (different session, same scope)
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="clarify", request_id="req-B1", payload={},
            stored_session_id="sess-B",
        ),
    )
    # Terminal for the sess-A run should drop only sess-A pending.
    router.record_run_start(
        scope_key="profile:x", run_id="run-A",
        stored_session_id="sess-A", turn_id="t-A",
    )
    await router.on_run_terminal(
        "profile:x", RunTerminalFrame(run_id="run-A", status="completed"),
    )
    assert not router.has_pending_request("req-A1")
    assert router.has_pending_request("req-B1")


@pytest.mark.asyncio
async def test_on_log_uses_main_logger(caplog) -> None:
    import logging
    router, _sup, _events, _ = _make_router()
    with caplog.at_level(logging.WARNING, logger="tui_gateway.services.worker_frame_router"):
        await router.on_log("profile:x", LogFrame(level="warn", text="hello"))
    assert any("hello" in m for m in caplog.messages)


def test_pending_snapshot_shape() -> None:
    router, _sup, _events, _ = _make_router()
    router.record_run_start(
        scope_key="profile:x", run_id="run-1",
        stored_session_id="sess-1", turn_id="t-1",
    )
    snap = router.pending_snapshot()
    assert {"pendingInteractive", "activeRuns"} <= snap.keys()
    assert snap["activeRuns"] == [
        {
                "runId": "run-1",
                "scopeKey": "profile:x",
                "conversationId": "sess-1",
                "storedSessionId": "sess-1",
            "turnId": "t-1",
        }
    ]
