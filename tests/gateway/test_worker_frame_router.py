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
import logging
import threading
from typing import Any

import pytest

from tui_gateway.run_worker import (
    EventFrame,
    InteractiveRequestFrame,
    InteractiveResponseFrame,
    LogFrame,
    RunTerminalFrame,
)
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter


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


def _make_router(supervisor: _FakeSupervisor = None, persist_interaction_event=None):
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
        persist_interaction_event=persist_interaction_event,
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
async def test_on_event_slow_persistence_keeps_runtime_loop_responsive() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_publish(_payload: dict) -> list:
        started.set()
        release.wait(timeout=2)
        return []

    router = WorkerFrameRouter(
        sender=_FakeSupervisor(),
        publish_event=slow_publish,
        publish_run_terminal=lambda **_kwargs: {},
    )
    publish_task = asyncio.create_task(
        router.on_event(
            "profile:x",
            "sess-1",
            EventFrame(params={"type": "message.complete", "text": "done"}),
        )
    )
    while not started.is_set():
        await asyncio.sleep(0.001)

    ticks = 0
    for _ in range(20):
        ticks += 1
        await asyncio.sleep(0.002)
    assert ticks == 20
    assert not publish_task.done()

    release.set()
    await publish_task


@pytest.mark.asyncio
async def test_interaction_request_publishes_independent_frame_and_persists_internal() -> None:
    persisted: list[tuple[str, str, str, str, int]] = []

    def persist(event_type: str, entry: Any) -> None:
        persisted.append((event_type, entry.request_id, entry.kind, entry.session_key, entry.anchor_seq))

    router, _sup, events, _ = _make_router(persist_interaction_event=persist)
    await router.on_event(
        "profile:x",
        "sess-1",
        EventFrame(params={
            "type": "approval.request",
            "conversation_session_id": "sess-1",
            "payload": {
                "request_id": "req-approval",
                "command": "rm -rf /tmp/demo",
                "anchor_seq": 12,
            },
        }),
    )

    assert persisted == [("interaction.requested", "req-approval", "approval", "sess-1", 12)]
    assert events == [
        {
            "type": "interaction.requested",
            "kind": "approval",
            "request_id": "req-approval",
            "conversation_session_id": "sess-1",
            "session_id": "",
            "runtime_scope_key": "profile:x",
            "conversation_id": "sess-1",
            "run_id": "",
            "turn_id": "",
            "seq": 0,
            "payload": {
                "request_id": "req-approval",
                "command": "rm -rf /tmp/demo",
                "anchor_seq": 12,
                "kind": "approval",
                "status": "pending",
                "source_event_type": "approval.request",
                "source_event": {
                    "type": "approval.request",
                    "conversation_session_id": "sess-1",
                    "payload": {
                        "request_id": "req-approval",
                        "command": "rm -rf /tmp/demo",
                        "anchor_seq": 12,
                    },
                    "runtime_scope_key": "profile:x",
                    "conversation_id": "sess-1",
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_interaction_request_persistence_failure_blocks_delivery() -> None:
    def persist(_event_type: str, _entry: Any) -> None:
        raise RuntimeError("persist failed")

    router, _sup, events, _ = _make_router(persist_interaction_event=persist)

    with pytest.raises(RuntimeError, match="persist failed"):
        await router.on_event(
            "profile:x",
            "sess-1",
            EventFrame(params={
                "type": "approval.request",
                "conversation_session_id": "sess-1",
                "payload": {
                    "request_id": "req-approval",
                    "anchor_seq": 12,
                },
            }),
        )

    assert events == []


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
            conversation_session_id="sess-1",
        ),
    )
    assert router.has_pending_request("req-1")
    # No event published — that's the worker's job, not the router's.
    assert events == []
    # But the routing table did capture the conversation_session_id.
    snap = router.pending_snapshot()
    assert snap["pendingInteractive"] == [
        {
            "requestId": "req-1",
            "scopeKey": "profile:x",
            "conversationId": "sess-1",
            "kind": "clarify",
            "conversationSessionId": "sess-1",
        },
    ]


@pytest.mark.asyncio
async def test_terminal_read_request_routes_back_to_owning_worker() -> None:
    router, supervisor, events, _ = _make_router()
    await router.on_interactive_request(
        "profile:agent-default",
        "conversation-1",
        InteractiveRequestFrame(
            kind="terminal_read",
            request_id="terminal-read-1",
            payload={"start": 0, "count": 20},
            conversation_session_id="conversation-1",
        ),
    )

    assert await router.respond(
        "terminal-read-1",
        '{"text":"terminal output"}',
        expected_kind="terminal_read",
    ) is True
    assert supervisor.sent == [
        (
            "profile:agent-default",
            "conversation-1",
            InteractiveResponseFrame(
                kind="terminal_read",
                request_id="terminal-read-1",
                answer='{"text":"terminal output"}',
                conversation_session_id="conversation-1",
            ),
        )
    ]
    assert not router.has_pending_request("terminal-read-1")
    assert events == []


@pytest.mark.asyncio
async def test_on_interactive_request_cross_fills_stored_session_from_run() -> None:
    """When the worker omits conversation_session_id (e.g. an older worker
    build), the router fills it from the active run record so a later
    respond/lookup can still scope correctly."""
    router, _sup, events, _ = _make_router()
    router.record_run_start(
        scope_key="profile:x",
        run_id="run-1",
        conversation_session_id="sess-A",
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
            "conversationSessionId": "sess-A",
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
async def test_on_run_terminal_reconciles_completed_run() -> None:
    """RunTerminalFrame is the main-side lifecycle reconciliation barrier."""
    router, _sup, _events, terminals = _make_router()
    await router.on_run_terminal(
        "profile:x",
        RunTerminalFrame(
            run_id="run-1",
            status="completed",
            conversation_session_id="sess-1",
            turn_id="turn-1",
            message="",
        ),
    )
    assert terminals == [
        {
            "conversation_session_id": "sess-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:x",
            "execution_session_id": "sess-1",
            "activity_id": "",
            "status": "completed",
            "message": "",
        }
    ]


@pytest.mark.asyncio
async def test_activity_terminal_is_persisted_through_activity_service(monkeypatch) -> None:
    from tui_gateway import server

    class Activities:
        def __init__(self) -> None:
            self.row = {
                "activity_id": "activity-1",
                "kind": "agent",
                "status": "running",
                "title": "Delegated task",
            }
            self.completed: list[dict] = []

        def get(self, activity_id: str):
            return dict(self.row) if activity_id == "activity-1" else {}

        def mark_completed(self, activity_id: str, **kwargs) -> bool:
            self.completed.append({"activity_id": activity_id, **kwargs})
            self.row.update(status="completed", **kwargs)
            return True

    class DB:
        def __init__(self) -> None:
            self.activities = Activities()

    db = DB()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    router, _sup, events, terminals = _make_router()
    router.record_run_start(
        scope_key="profile:x",
        run_id="run-1",
        conversation_session_id="sess-1",
        turn_id="turn-1",
        dispatch_activity_id="activity-1",
    )
    await router.on_event(
        "profile:x",
        "sess-1",
        EventFrame(
            params={
                "type": "message.complete",
                "run_id": "run-1",
                "payload": {"text": "done", "usage": {"total_tokens": 7}},
            }
        ),
    )

    await router.on_run_terminal(
        "profile:x",
        "sess-1",
        RunTerminalFrame(
            run_id="run-1",
            status="completed",
            conversation_session_id="sess-1",
            turn_id="turn-1",
        ),
    )

    assert db.activities.completed == [
        {
            "activity_id": "activity-1",
            "result_summary": "done",
            "result_json": {
                "last_message": {
                    "role": "assistant",
                    "content": "done",
                    "metadata": {
                        "run_id": "run-1",
                        "turn_id": None,
                        "status": None,
                        "usage": {"total_tokens": 7},
                        "source_event": "message.complete",
                    },
                },
                "usage": {"total_tokens": 7},
                "run_id": "run-1",
            },
        }
    ]
    assert any(event.get("type") == "activity.completed" for event in events)
    assert len(terminals) == 1
    assert terminals[0]["status"] == "completed"


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
            conversation_session_id="sess-1",
            turn_id="turn-1",
            message="kaboom",
        ),
    )
    assert terminals == [
        {
            "conversation_session_id": "sess-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:x",
            "execution_session_id": "sess-1",
            "activity_id": "",
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
            conversation_session_id="sess-1",
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
        conversation_session_id="sess-A",
        turn_id="turn-A",
    )
    await router.on_run_terminal(
        "profile:x",
        RunTerminalFrame(run_id="run-1", status="failed"),
    )
    assert terminals[0]["conversation_session_id"] == "sess-A"
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
            conversation_session_id="sess-1",
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
    assert frame.conversation_session_id == "sess-1"
    # entry consumed → second respond fails
    assert await router.respond("req-1", "yes") is False


@pytest.mark.asyncio
async def test_worker_resolution_event_persists_and_publishes_resolved_lifecycle() -> None:
    persisted: list[tuple[str, str, str, str, Any]] = []

    def persist(event_type: str, entry: Any) -> None:
        persisted.append(
            (event_type, entry.request_id, entry.kind, entry.session_key, entry.choice)
        )

    router, _sup, events, _ = _make_router(persist_interaction_event=persist)
    await router.on_event(
        "profile:x",
        "sess-1",
        EventFrame(
            params={
                "type": "clarify.request",
                "conversation_session_id": "sess-1",
                "payload": {"request_id": "req-1", "anchor_seq": 12},
            }
        ),
    )
    await router.on_event(
        "profile:x",
        "sess-1",
        EventFrame(
            params={
                "type": "clarify.resolved",
                "conversation_session_id": "sess-1",
                "payload": {"request_id": "req-1", "choice": "yes"},
            }
        ),
    )

    assert persisted == [
        ("interaction.requested", "req-1", "clarify", "sess-1", None),
        ("interaction.resolved", "req-1", "clarify", "sess-1", "yes"),
    ]
    assert [event["type"] for event in events] == [
        "interaction.requested",
        "interaction.resolved",
    ]
    assert events[-1]["payload"]["status"] == "resolved"


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
            conversation_session_id="sess-1",
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
            conversation_session_id="sess-1",
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
            conversation_session_id="sess-A",
        ),
    )
    # Pending for sess-B (different session, same scope)
    await router.on_interactive_request(
        "profile:x",
        InteractiveRequestFrame(
            kind="clarify", request_id="req-B1", payload={},
            conversation_session_id="sess-B",
        ),
    )
    # Terminal for the sess-A run should drop only sess-A pending.
    router.record_run_start(
        scope_key="profile:x", run_id="run-A",
        conversation_session_id="sess-A", turn_id="t-A",
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
    with caplog.at_level(logging.WARNING, logger="hermes_agent.orchestration.worker_frame_router"):
        await router.on_log("profile:x", LogFrame(level="warn", text="hello"))
    assert any("hello" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_on_log_preserves_worker_source_metadata(caplog) -> None:
    router, _sup, _events, _ = _make_router()
    frame = LogFrame(
        level="error",
        text="worker failure",
        logger="agent.worker-test",
        created=123.25,
        process_id=4321,
        thread_name="agent-thread",
        session_tag=" [session-worker]",
        pathname="/tmp/agent_worker.py",
        line_no=91,
        exception="ValueError: boom",
    )
    with caplog.at_level(logging.ERROR):
        await router.on_log("profile:x", "conversation-1", frame)

    record = next(
        record for record in caplog.records
        if "worker failure" in record.getMessage()
    )
    assert record.name == "agent.worker-test"
    assert record.pathname == "/tmp/agent_worker.py"
    assert record.lineno == 91
    assert record.process == 4321
    assert record.threadName == "agent-thread"
    assert record.session_tag == " [session-worker]"
    assert "[run-worker:profile:x:conversation-1]" in record.getMessage()
    assert "ValueError: boom" in record.getMessage()


def test_pending_snapshot_shape() -> None:
    router, _sup, _events, _ = _make_router()
    router.record_run_start(
        scope_key="profile:x", run_id="run-1",
        conversation_session_id="sess-1", turn_id="t-1",
    )
    snap = router.pending_snapshot()
    assert {"pendingInteractive", "activeRuns"} <= snap.keys()
    assert snap["activeRuns"] == [
        {
                "runId": "run-1",
                "scopeKey": "profile:x",
                "conversationId": "sess-1",
                "conversationSessionId": "sess-1",
            "turnId": "t-1",
        }
    ]
