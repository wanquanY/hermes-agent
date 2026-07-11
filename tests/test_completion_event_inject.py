from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.activity_event_bus import (
    ActivityEventBus,
    set_default_activity_event_bus,
)
from agent.conversation_loop import _drain_activity_events_for_api
from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway.run_worker import (
    ActivityEventFrame,
    EventFrame,
    RunStartFrame,
    RunTerminalFrame,
    WorkerInteractiveResponder,
    WorkerRunBackend,
    _build_default_handler,
    decode_incoming,
)
from tui_gateway.services.agent_runner import _run_context_from_frame
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter


class _NoopBackend(WorkerRunBackend):
    pass


class _NoopResponder(WorkerInteractiveResponder):
    async def resolve(self, frame) -> bool:
        return False


class _FakeSessionDB:
    def __init__(self) -> None:
        self.read_ids: list[str] = []

    def mark_activity_read(self, activity_id: str) -> bool:
        self.read_ids.append(activity_id)
        return True


class _FakeAgent:
    def __init__(self, bus: ActivityEventBus) -> None:
        self.activity_event_bus = bus
        self._session_db = _FakeSessionDB()


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
        self.sent.append((scope_key, conversation_id, frame))
        return True


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _make_router(db: CliSessionStore):
    sender = _FakeSender()
    events: list[dict[str, Any]] = []

    def publish_event(payload: dict[str, Any], **kwargs: Any) -> list[Any]:
        events.append({"payload": payload, "kwargs": kwargs})
        return []

    router = WorkerFrameRouter(
        sender=sender,
        publish_event=publish_event,
        publish_run_terminal=lambda **kwargs: {},
    )
    return router, sender, events


def _activity(db: CliSessionStore, activity_id: str = "act-1") -> None:
    db.activities.create(
        activity_id=activity_id,
        conversation_id="conv-parent",
        kind="agent_dispatch",
        target_profile_id="profile-worker",
        prompt_summary="Do the long task",
    )
    db.activities.update_status(activity_id, "running", started_at=100.0)


def test_activity_event_bus_push_and_drain() -> None:
    bus = ActivityEventBus()

    bus.push({"activity_id": "act-1", "result_summary": "done"})
    bus.push({"activity_id": "act-2", "result_summary": "failed"})

    assert bus.peek_count() == 2
    assert [event["activity_id"] for event in bus.drain()] == ["act-1", "act-2"]
    assert bus.peek_count() == 0


@pytest.mark.asyncio
async def test_worker_inbound_activity_event_routes_to_bus() -> None:
    bus = ActivityEventBus()
    set_default_activity_event_bus(bus)
    try:
        frame = decode_incoming(
            json.dumps(
                {
                    "op": "event",
                    "kind": "activity",
                    "event": {"activity_id": "act-1", "status": "completed"},
                }
            )
        )
        assert isinstance(frame, ActivityEventFrame)
        handler = _build_default_handler(_NoopBackend(), _NoopResponder(), set())

        await handler(None, frame)  # type: ignore[arg-type]

        assert bus.peek_count() == 1
        assert bus.drain()[0]["activity_id"] == "act-1"
    finally:
        set_default_activity_event_bus(None)


def test_leader_llm_context_injects_drained_events_as_system() -> None:
    bus = ActivityEventBus()
    bus.push(
        {
            "activity_id": "act-1",
            "status": "completed",
            "activity_kind": "agent_dispatch",
            "target_profile_id": "profile-worker",
            "result_summary": "The shard is green.",
            "started_at": 100.0,
        }
    )
    agent = _FakeAgent(bus)

    messages = _drain_activity_events_for_api(agent)

    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    content = messages[0]["content"]
    assert "Async activity act-1 (dispatched at 100.0) just completed:" in content
    assert "- kind: agent_dispatch" in content
    assert "- target: profile-worker" in content
    assert 'result summary: "The shard is green."' in content
    assert "call get_activity tool" in content


@pytest.mark.asyncio
async def test_dispatched_worker_completion_marks_activity_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    _activity(db)
    monkeypatch.setattr("tui_gateway.server._get_db", lambda: db, raising=False)
    router, sender, events = _make_router(db)
    router.record_run_start(
        scope_key="conv-child",
        run_id="run-1",
        conversation_session_id="conv-child",
        turn_id="turn-1",
        dispatch_activity_id="act-1",
        activity_kind="agent_dispatch",
        parent_scope_key="leader-scope",
    )
    await router.on_event(
        "conv-child",
        EventFrame(
            params={
                "type": "message.complete",
                "run_id": "run-1",
                "payload": {
                    "text": "All tests passed with a long explanation.",
                    "usage": {"total_tokens": 42},
                },
            }
        ),
    )

    await router.on_run_terminal("conv-child", RunTerminalFrame(run_id="run-1", status="completed"))

    row = db.activities.get("act-1")
    assert row["status"] == "completed"
    assert row["result_summary"] == "All tests passed with a long explanation."
    result_json = json.loads(row["result_json"])
    assert result_json["run_id"] == "run-1"
    assert result_json["usage"] == {"total_tokens": 42}
    assert events[-1]["payload"]["type"] == "activity.completed"
    assert sender.sent
    assert sender.sent[-1][0] == "leader-scope"
    assert isinstance(sender.sent[-1][2], ActivityEventFrame)


@pytest.mark.asyncio
async def test_dispatched_worker_failure_marks_activity_failed_with_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    _activity(db)
    monkeypatch.setattr("tui_gateway.server._get_db", lambda: db, raising=False)
    router, sender, events = _make_router(db)
    router.record_run_start(
        scope_key="conv-child",
        run_id="run-1",
        conversation_session_id="conv-child",
        dispatch_activity_id="act-1",
        activity_kind="agent_dispatch",
        parent_scope_key="leader-scope",
    )

    await router.on_run_terminal(
        "conv-child",
        RunTerminalFrame(run_id="run-1", status="failed", message="worker exploded"),
    )

    row = db.activities.get("act-1")
    assert row["status"] == "failed"
    assert row["result_summary"] == "worker exploded"
    assert events[-1]["payload"]["type"] == "activity.failed"
    assert isinstance(sender.sent[-1][2], ActivityEventFrame)
    assert sender.sent[-1][2].event["status"] == "failed"


def test_get_activity_tool_handler_returns_status_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.get_activity as get_activity

    class _Activities:
        def get(self, activity_id: str):
            assert activity_id == "act-1"
            return {
                "activity_id": "act-1",
                "status": "completed",
                "result_summary": "done",
                "result_json": '{"ok":true}',
            }

    class _Proxy:
        activities = _Activities()

    monkeypatch.setattr(get_activity, "get_default_worker_db_proxy", lambda: _Proxy())

    payload = json.loads(get_activity._handle_get_activity({"activity_id": "act-1"}))

    assert payload["found"] is True
    assert payload["status"] == "completed"
    assert payload["result_summary"] == "done"


def test_mark_activity_read_called_after_inject() -> None:
    bus = ActivityEventBus()
    bus.push({"activity_id": "act-1", "status": "completed", "result_summary": "done"})
    agent = _FakeAgent(bus)

    _drain_activity_events_for_api(agent)
    _drain_activity_events_for_api(agent)

    assert agent._session_db.read_ids == ["act-1"]


def test_agent_runner_parses_run_context_without_recursing(tmp_path: Path) -> None:
    from hermes_team_mission.domain.run_context import RunContext

    context = RunContext(
        conversation_session_id="conv-parent",
        participant_id="leader:conv-parent",
        activity_id="chat",
        activity_kind="chat",
        execution_scope_key="leader-scope",
        control_home=str(tmp_path),
        execution_home=str(tmp_path),
    )

    parsed = _run_context_from_frame(
        RunStartFrame(
            run_id="run-1",
            turn_id="turn-1",
            conversation_session_id="conv-parent",
            prompt="hello",
            params={"run_context_json": json.dumps(context.to_payload())},
        )
    )

    assert parsed == context
