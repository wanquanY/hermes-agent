from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.runtime.activity_command_bridge import record_legacy_activity_command
from tui_gateway import server
from tui_gateway.services import run_control
from tui_gateway.services.activity_reconciler import ActivityReconciler

activity_methods = importlib.import_module("tui_gateway.methods.activity")


class _CaptureTransport:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, obj: dict[str, Any]) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        return None


class _ToggleTransport(_CaptureTransport):
    def __init__(self, *, failing: bool) -> None:
        super().__init__()
        self.failing = failing

    def write(self, obj: dict[str, Any]) -> bool:
        if self.failing:
            return False
        return super().write(obj)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[CliSessionStore]:
    state = open_cli_session_store(tmp_path / "state.db")
    try:
        yield state
    finally:
        state.close()


@pytest.fixture(autouse=True)
def gateway_state(db: CliSessionStore) -> Iterator[None]:
    previous_db = server._db
    previous_db_error = server._db_error
    previous_db_by_home = dict(server._db_by_home)
    previous_db_error_by_home = dict(server._db_error_by_home)
    server._db = db
    server._db_error = None
    server._db_by_home = {}
    server._db_error_by_home = {}
    with run_control._lock:
        run_control._subscriptions_by_id.clear()
        run_control._subscription_ids_by_session.clear()
        run_control._subscription_ids_by_activity.clear()
        run_control._subscription_ids_by_transport.clear()
    try:
        yield
    finally:
        with run_control._lock:
            run_control._subscriptions_by_id.clear()
            run_control._subscription_ids_by_session.clear()
            run_control._subscription_ids_by_activity.clear()
            run_control._subscription_ids_by_transport.clear()
        server._db = previous_db
        server._db_error = previous_db_error
        server._db_by_home = previous_db_by_home
        server._db_error_by_home = previous_db_error_by_home


def _call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    transport: _CaptureTransport | None = None,
) -> dict[str, Any]:
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": f"test-{method}-{time.time_ns()}",
            "method": method,
            "params": params or {},
        },
        transport=transport,
    )
    assert isinstance(response, dict)
    return response


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return response["result"]


def _record_activity_event(
    db: CliSessionStore,
    *,
    activity_id: str = "act-test-1",
    conversation_session_id: str = "session-activity-1",
    event_type: str = "activity.command.created",
    command_id: str = "cmd-test-1",
    publish: bool = False,
) -> dict[str, Any]:
    frame = {
        "type": event_type,
        "session_id": conversation_session_id,
        "conversation_session_id": conversation_session_id,
        "activity_id": activity_id,
        "seq": run_control.next_event_seq(conversation_session_id, db=db),
        "payload": {
            "activity_id": activity_id,
            "command_id": command_id,
            "state": "satisfied",
        },
    }
    if publish:
        run_control.publish_recorded_event(frame, db=db)
    else:
        run_control.record_event(frame, db=db)
    return frame


def _event_frames(transport: _CaptureTransport) -> list[dict[str, Any]]:
    return [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]


def test_subscribe_rejects_missing_activity_id() -> None:
    response = _call("runtime.activity.subscribe", {})

    assert response["error"]["code"] == 4006


def test_subscribe_rejects_malformed_activity_id() -> None:
    response = _call("runtime.activity.subscribe", {"activity_id": "not-valid"})

    assert response["error"]["code"] == 4006


def test_subscribe_returns_5008_when_db_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity_methods._server, "_get_db", lambda: None)

    response = _call("runtime.activity.subscribe", {"activity_id": "act-test-missing-db"})

    assert response["error"]["code"] == 5008


def test_subscribe_allows_future_activity_with_empty_replay() -> None:
    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-empty"})
    )

    assert result["subscription_id"]
    assert result["events"] == []
    assert result["after_seq"] == 0


def test_unsubscribe_returns_zero_when_subscription_missing() -> None:
    result = _assert_ok(
        _call("runtime.activity.unsubscribe", {"subscription_id": "missing-subscription"})
    )

    assert result == {"removed": 0}


def test_subscribe_returns_events_for_activity_id(db: CliSessionStore) -> None:
    _record_activity_event(db, activity_id="act-test-replay", command_id="cmd-replay-1")

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-replay"})
    )

    assert result["subscription_id"]
    assert [event["payload"]["command_id"] for event in result["events"]] == ["cmd-replay-1"]
    assert result["after_seq"] == result["events"][-1]["seq"]


def test_subscribe_returns_events_for_team_conversation_activity_id(db: CliSessionStore) -> None:
    _record_activity_event(
        db,
        activity_id="team-conversation:conversation-1",
        command_id="cmd-conversation-1",
    )

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "team-conversation:conversation-1"})
    )

    assert result["subscription_id"]
    assert [event["activity_id"] for event in result["events"]] == [
        "team-conversation:conversation-1",
    ]
    assert [event["payload"]["command_id"] for event in result["events"]] == [
        "cmd-conversation-1",
    ]


def test_team_dispatch_activity_waits_for_mission_event_log_binding(db: CliSessionStore) -> None:
    """Activity-first team dispatch subscriptions must not mix run_events and mission event seqs."""
    activity_id = "act-team_dispatch-subscribe-e2e"
    _record_activity_event(
        db,
        activity_id=activity_id,
        command_id="cmd-leader-run-event",
    )

    unbound = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": activity_id})
    )

    assert unbound["events"] == []
    assert unbound["after_seq"] == 0

    db.upsert_team_mission(
        mission_id="mission-dispatch-subscribe",
        conversation_id="conversation-dispatch-subscribe",
        title="Dispatch subscription",
        mode="supervised_mission",
        leader_session_id="team-session-dispatch-subscribe",
        metadata={"task_id": "task-dispatch-subscribe"},
    )
    db.activities.bind_to_mission(
        activity_id=activity_id,
        conversation_id="team-session-dispatch-subscribe",
        mission_id="mission-dispatch-subscribe",
        target_team_id="team-1",
        status="running",
    )
    stored = db.append_team_mission_structural_event(
        mission_id="mission-dispatch-subscribe",
        source_event={
            "type": "mission.node.created",
            "payload": {
                "mission_id": "mission-dispatch-subscribe",
                "node": {
                    "node_id": "node-worker",
                    "kind": "worker",
                    "title": "Worker",
                    "status": "pending",
                },
            },
        },
    )

    rebound = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": activity_id, "after_seq": 2294})
    )

    assert stored["seq"] == 1
    assert [event["activity_id"] for event in rebound["events"]] == [activity_id]
    assert rebound["events"][0]["seq"] == 1
    assert rebound["events"][0]["payload"]["source_event_type"] == "mission.node.created"


def test_subscribe_after_seq_filters_correctly(db: CliSessionStore) -> None:
    first = _record_activity_event(db, activity_id="act-test-after", command_id="cmd-after-1")
    _record_activity_event(db, activity_id="act-test-after", command_id="cmd-after-2")

    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-after", "after_seq": first["seq"]},
        )
    )

    assert [event["payload"]["command_id"] for event in result["events"]] == ["cmd-after-2"]


def test_subscribe_limit_caps_replay_size(db: CliSessionStore) -> None:
    for index in range(3):
        _record_activity_event(
            db,
            activity_id="act-test-limit",
            command_id=f"cmd-limit-{index}",
        )

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-limit", "limit": 2})
    )

    assert len(result["events"]) == 2


def test_terminal_team_mission_subscribe_defaults_to_cursor_only(db: CliSessionStore) -> None:
    db.upsert_team_mission(
        mission_id="mission-terminal-subscribe",
        conversation_id="conversation-terminal-subscribe",
        title="Terminal mission",
        mode="supervised_mission",
        status="completed",
    )
    db.append_team_mission_structural_event(
        mission_id="mission-terminal-subscribe",
        source_event={
            "type": "mission.node.created",
            "payload": {
                "mission_id": "mission-terminal-subscribe",
                "node": {
                    "node_id": "node-terminal",
                    "kind": "worker",
                    "title": "Done",
                    "status": "completed",
                },
            },
        },
    )
    last_seq = max(int(event.get("seq") or 0) for event in db.list_team_mission_events("mission-terminal-subscribe"))
    transport = _CaptureTransport()

    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "mission:mission-terminal-subscribe"},
            transport=transport,
        )
    )

    assert result["events"] == []
    assert result["after_seq"] == last_seq
    assert result["afterSeq"] == last_seq
    subscription = run_control._subscriptions_by_id[result["subscription_id"]]
    assert subscription["activity_event_last_seq"] == last_seq
    assert subscription["cursor_only"] is True


def test_terminal_team_mission_subscribe_allows_debug_audit_replay(db: CliSessionStore) -> None:
    db.upsert_team_mission(
        mission_id="mission-terminal-debug",
        conversation_id="conversation-terminal-debug",
        title="Terminal mission",
        mode="supervised_mission",
        status="completed",
    )
    db.append_team_mission_structural_event(
        mission_id="mission-terminal-debug",
        source_event={
            "type": "mission.node.created",
            "payload": {
                "mission_id": "mission-terminal-debug",
                "node": {
                    "node_id": "node-terminal",
                    "kind": "worker",
                    "title": "Done",
                    "status": "completed",
                },
            },
        },
    )

    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {
                "activity_id": "mission:mission-terminal-debug",
                "debug_replay_audit": True,
            },
        )
    )

    assert len(result["events"]) >= 1
    assert result["events"][0]["activity_id"] == "mission:mission-terminal-debug"


def test_subscribe_returns_unique_subscription_id(db: CliSessionStore) -> None:
    _record_activity_event(db, activity_id="act-test-unique")

    first = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-unique"}, transport=_CaptureTransport())
    )
    second = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-unique"}, transport=_CaptureTransport())
    )

    assert first["subscription_id"] != second["subscription_id"]


def test_subscribe_then_unsubscribe_removes_registration(db: CliSessionStore) -> None:
    _record_activity_event(db, activity_id="act-test-lifecycle")
    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-lifecycle"},
            transport=_CaptureTransport(),
        )
    )
    subscription_id = result["subscription_id"]

    removed = _assert_ok(
        _call("runtime.activity.unsubscribe", {"subscription_id": subscription_id})
    )

    assert removed == {"removed": 1}
    assert subscription_id not in run_control._subscriptions_by_id
    assert subscription_id not in run_control._subscription_ids_by_activity["act-test-lifecycle"]


def test_record_event_pushes_to_activity_subscribers(db: CliSessionStore) -> None:
    _record_activity_event(db, activity_id="act-test-live")
    transport = _CaptureTransport()
    _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-live"},
            transport=transport,
        )
    )

    _record_activity_event(
        db,
        activity_id="act-test-live",
        command_id="cmd-live-2",
        publish=True,
    )

    delivered = _event_frames(transport)
    assert [event["payload"]["command_id"] for event in delivered] == ["cmd-live-2"]


def test_failed_activity_delivery_remains_replayable(db: CliSessionStore) -> None:
    transport = _ToggleTransport(failing=True)
    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-retry"},
            transport=transport,
        )
    )
    subscription_id = result["subscription_id"]

    _record_activity_event(
        db,
        activity_id="act-test-retry",
        command_id="cmd-retry-1",
        publish=True,
    )

    assert subscription_id not in run_control._subscriptions_by_id
    persisted = db.runs.list_events_by_activity("act-test-retry")
    assert len(persisted) == 1

    replay_transport = _CaptureTransport()
    replay = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-retry"},
            transport=replay_transport,
        )
    )
    assert [event["payload"]["command_id"] for event in replay["events"]] == [
        "cmd-retry-1"
    ]


def test_future_activity_subscription_receives_first_event(db: CliSessionStore) -> None:
    transport = _CaptureTransport()
    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-future"},
            transport=transport,
        )
    )
    assert result["events"] == []

    _record_activity_event(
        db,
        activity_id="act-test-future",
        command_id="cmd-future-1",
        publish=True,
    )

    delivered = _event_frames(transport)
    assert [event["payload"]["command_id"] for event in delivered] == ["cmd-future-1"]


def test_future_team_mission_activity_subscription_receives_first_event_after_graph_created(
    db: CliSessionStore,
) -> None:
    transport = _CaptureTransport()
    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "mission:mission-future"},
            transport=transport,
        )
    )
    assert result["events"] == []

    db.initialize_team_mission_from_strategy(
        mission_id="mission-future",
        title="Future mission",
        objective="deliver the first live event",
        mode="supervised_mission",
    )
    root_node_id = db.get_team_mission_graph("mission-future")["nodes"][0]["node_id"]
    db.runs.upsert(
        run_id="run-future",
        session_id="team:mission-future:node:root",
        runtime_scope_key="team:mission-future:node:root",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-future",
        node_id=root_node_id,
        run_id="run-future",
        session_id="team:mission-future:node:root",
        runtime_scope_key="team:mission-future:node:root",
        role="worker",
    )

    run_control.publish_recorded_event(
        {
            "type": "message.delta",
            "session_id": "runtime-node-future",
            "conversation_session_id": "team:mission-future:node:root",
            "run_id": "run-future",
            "runtime_scope_key": "team:mission-future:node:root",
            "activity_id": f"act-node:mission-future:{root_node_id}",
            "seq": 1,
            "payload": {
                "activity_id": f"act-node:mission-future:{root_node_id}",
                "delta": "first live event",
            },
        },
        db=db,
    )

    delivered = _event_frames(transport)
    assert [event["type"] for event in delivered] == ["team_mission.runtime.event"]
    assert delivered[0]["activity_id"] == "mission:mission-future"
    assert delivered[0]["payload"]["source_event_type"] == "message.delta"
    assert delivered[0]["payload"]["text_stream"]["delta"] == "first live event"


def test_team_dispatch_raw_cursor_does_not_block_bound_mission_graph_event(
    db: CliSessionStore,
) -> None:
    activity_id = "act-team_dispatch-client-turn-1"
    transport = _CaptureTransport()
    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": activity_id},
            transport=transport,
        )
    )
    subscription_id = result["subscription_id"]

    run_control.publish_recorded_event(
        {
            "type": "message.delta",
            "session_id": "leader-runtime",
            "conversation_session_id": "team-session-live",
            "run_id": "leader-run",
            "runtime_scope_key": "team-session-live",
            "activity_id": activity_id,
            "seq": 2294,
            "payload": {
                "activity_id": activity_id,
                "delta": "leader planning",
            },
        },
        db=db,
    )

    subscription = run_control._subscriptions_by_id[subscription_id]
    assert subscription["last_seq"] == 0
    assert subscription["activity_event_last_seq"] == 0

    db.upsert_team_mission(
        mission_id="mission-live-graph",
        conversation_id="conversation-live-graph",
        title="Live graph mission",
        mode="supervised_mission",
        leader_session_id="team-session-live",
        metadata={"task_id": "task-live-graph"},
    )
    db.activities.bind_to_mission(
        activity_id=activity_id,
        conversation_id="team-session-live",
        mission_id="mission-live-graph",
        target_team_id="team-1",
        status="running",
    )
    transport.frames.clear()

    db.append_team_mission_structural_event(
        mission_id="mission-live-graph",
        source_event={
            "type": "mission.node.created",
            "payload": {
                "mission_id": "mission-live-graph",
                "node": {
                    "node_id": "worker-node-1",
                    "title": "Create test file",
                    "kind": "worker",
                    "status": "pending",
                },
            },
        },
    )

    delivered = _event_frames(transport)
    assert [event["type"] for event in delivered] == ["team_mission.runtime.event"]
    assert delivered[0]["activity_id"] == activity_id
    activity_event_seq = int(delivered[0]["activity_event_seq"])
    assert 0 < activity_event_seq < 2294
    assert delivered[0]["payload"]["source_event_type"] == "mission.node.created"
    assert run_control._subscriptions_by_id[subscription_id]["activity_event_last_seq"] == activity_event_seq


def test_record_event_does_not_double_push_when_transport_subscribed_by_session_and_activity(
    db: CliSessionStore,
) -> None:
    conversation_session_id = "session-double-subscribe"
    activity_id = "act-test-double"
    _record_activity_event(db, activity_id=activity_id, conversation_session_id=conversation_session_id)
    transport = _CaptureTransport()
    run_control.subscribe_session_with_id(
        conversation_session_id=conversation_session_id,
        transport=transport,
        db=db,
    )
    _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": activity_id},
            transport=transport,
        )
    )

    _record_activity_event(
        db,
        activity_id=activity_id,
        conversation_session_id=conversation_session_id,
        command_id="cmd-double-live",
        publish=True,
    )

    delivered = _event_frames(transport)
    assert [event["payload"]["command_id"] for event in delivered] == ["cmd-double-live"]


def test_subscribe_picks_up_events_emitted_by_reconciler(db: CliSessionStore) -> None:
    db.activities.insert_command(
        command_id="cmd-reconciler-create",
        activity_id="act-test-reconciler",
        kind="create",
        payload={
            "kind": "mission",
            "conversation_id": "conv-reconciler",
            "conversation_session_id": "session-reconciler",
        },
        metadata={"test": True},
    )
    ActivityReconciler(db).run_one_cycle()

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-reconciler"})
    )

    assert [event["type"] for event in result["events"]] == ["activity.command.created"]


def test_subscribe_picks_up_events_for_team_mission_create_via_legacy_bridge(
    db: CliSessionStore,
) -> None:
    command_id = record_legacy_activity_command(
        db,
        activity_id="mission:test-legacy",
        kind="create",
        payload={
            "kind": "mission",
            "conversation_id": "conversation-legacy",
            "conversation_session_id": "session-legacy",
        },
        source="team_mission.create",
    )
    assert command_id
    ActivityReconciler(db).run_one_cycle()

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "mission:test-legacy"})
    )

    assert [event["payload"]["command_id"] for event in result["events"]] == [command_id]


def test_subscribe_activity_id_filter_isolates_different_activities(db: CliSessionStore) -> None:
    _record_activity_event(db, activity_id="act-test-isolated-a", command_id="cmd-a")
    _record_activity_event(db, activity_id="act-test-isolated-b", command_id="cmd-b")

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-isolated-a"})
    )

    assert [event["payload"]["command_id"] for event in result["events"]] == ["cmd-a"]


def test_list_run_events_by_activity_returns_only_matching_rows(db: CliSessionStore) -> None:
    _record_activity_event(
        db,
        activity_id="act-test-dao",
        conversation_session_id="session-dao-a",
        command_id="cmd-dao-a",
    )
    _record_activity_event(
        db,
        activity_id="act-test-dao",
        conversation_session_id="session-dao-b",
        command_id="cmd-dao-b",
    )
    _record_activity_event(
        db,
        activity_id="act-test-other",
        conversation_session_id="session-dao-a",
        command_id="cmd-other",
    )

    events = db.runs.list_events_by_activity("act-test-dao")

    assert [event["payload"]["command_id"] for event in events] == ["cmd-dao-a", "cmd-dao-b"]
    assert {event["conversation_session_id"] for event in events} == {"session-dao-a", "session-dao-b"}


def test_list_run_events_by_activity_returns_empty_for_unknown(db: CliSessionStore) -> None:
    assert db.runs.list_events_by_activity("act-test-unknown") == []
