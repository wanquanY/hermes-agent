import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services import run_control
from tui_gateway.services import team_mission_activity_events
from tui_gateway.services.subagent_snapshots import build_subagent_run_snapshots


@pytest.fixture(autouse=True)
def reset_runtime_event_state():
    run_control._reset_for_tests()
    yield
    run_control._reset_for_tests()


@pytest.mark.parametrize(
    ("session_id", "runtime_scope_key"),
    [
        ("ordinary-session", "profile:agent-default"),
        ("team-leader-session", "team:conversation-1:leader-conversation"),
        ("team-member-session", "team:conversation-1:member:agent-reviewer"),
    ],
)
def test_primary_chat_streams_are_transient_until_full_terminal_message(
    tmp_path,
    session_id,
    runtime_scope_key,
):
    db = open_cli_session_store(tmp_path / f"{session_id}.db")
    run_id = f"run-{session_id}"
    for seq, text in enumerate(("A", "😀", "中"), start=1):
        run_control.record_event(
            {
                "type": "message.delta",
                "conversation_session_id": session_id,
                "session_id": f"runtime-{session_id}",
                "run_id": run_id,
                "turn_id": f"turn-{session_id}",
                "runtime_scope_key": runtime_scope_key,
                "seq": seq,
                "payload": {"mode": "append", "delta": text, "text": text},
            },
            db=db,
        )

    assert db.runs.list_events(session_id) == []
    _subscription_id, replay = run_control.subscribe_session_with_id(
        conversation_session_id=session_id,
        transport=None,
        db=db,
    )
    assert len(replay) == 1
    assert replay[0]["transient"] is True
    assert replay[0]["payload"]["mode"] == "snapshot"
    assert replay[0]["payload"]["text"] == "A😀中"

    run_control.record_event(
        {
            "type": "message.complete",
            "conversation_session_id": session_id,
            "session_id": f"runtime-{session_id}",
            "run_id": run_id,
            "turn_id": f"turn-{session_id}",
            "runtime_scope_key": runtime_scope_key,
            "seq": 4,
            "payload": {"status": "complete", "text": "A😀中"},
        },
        db=db,
    )

    events = db.runs.list_events(session_id)
    assert [event["type"] for event in events] == ["message.complete"]
    assert events[0]["payload"]["text"] == "A😀中"
    db.close()


def test_transient_source_seq_never_advances_the_durable_subscription_cursor(tmp_path):
    class Transport:
        def __init__(self):
            self.events = []

        def write(self, frame):
            self.events.append(dict(frame["params"]))
            return True

    db = open_cli_session_store(tmp_path / "cursor-domains.db")
    transport = Transport()
    session_id = "cursor-domain-session"
    run_id = "cursor-domain-run"
    common = {
        "conversation_session_id": session_id,
        "session_id": "runtime-cursor-domain",
        "run_id": run_id,
        "turn_id": "cursor-domain-turn",
        "runtime_scope_key": "profile:agent-default",
    }
    run_control.subscribe_session_with_id(
        conversation_session_id=session_id,
        transport=transport,
        db=db,
    )

    transient = {
        **common,
        "type": "message.delta",
        "seq": 5_751,
        "payload": {"mode": "append", "delta": "before tool", "offset": 0},
    }
    run_control.publish_recorded_event(transient, db=db)

    assert transient["transient"] is True
    assert "seq" not in transient
    assert transient["runtime_source_seq"] == 5_751
    assert transport.events[-1]["runtime_source_seq"] == 5_751
    assert "seq" not in transport.events[-1]

    tool = {
        **common,
        "type": "tool.start",
        "seq": 5_752,
        "payload": {"tool_id": "tool-after-stream", "name": "terminal"},
    }
    run_control.publish_recorded_event(tool, db=db)

    persisted = db.runs.list_events(session_id, run_id=run_id)
    assert [event["type"] for event in persisted] == ["message.delta", "tool.start"]
    assert [event["seq"] for event in persisted] == [1, 2]
    assert persisted[-1]["runtime_source_seq"] == 5_752
    assert transport.events[-1]["type"] == "tool.start"
    assert transport.events[-1]["seq"] == 2
    assert transport.events[-1]["runtime_source_seq"] == 5_752
    db.close()


def test_team_activity_projection_keeps_transient_source_out_of_cursor_fields():
    projected = team_mission_activity_events.project_run_event_for_subscription(
        {
            "type": "subagent.output_delta",
            "transient": True,
            "runtime_source_seq": 5_751,
            "conversation_session_id": "team-session",
            "run_id": "team-run",
            "turn_id": "team-turn",
            "payload": {
                "subagent_id": "reviewer",
                "mode": "append",
                "delta": "reviewing",
                "offset": 0,
            },
        },
        "mission:mission-1",
        mission_id_value="mission-1",
    )

    assert projected["transient"] is True
    assert projected["runtime_source_seq"] == 5_751
    assert projected["source_seq"] == 0
    assert "seq" not in projected
    assert "activity_event_seq" not in projected
    assert "team_mission_event_seq" not in projected
    assert "seq" not in projected["payload"]
    assert "activity_event_seq" not in projected["payload"]
    assert "team_mission_event_seq" not in projected["payload"]


def test_subagent_stream_checkpoints_once_before_complete(tmp_path):
    db = open_cli_session_store(tmp_path / "subagent.db")
    session_id = "team-session-with-subagent"
    common = {
        "conversation_session_id": session_id,
        "session_id": "runtime-team-leader",
        "run_id": "run-team-leader",
        "turn_id": "turn-team-leader",
        "runtime_scope_key": "team:conversation-1:leader-conversation",
    }
    for seq, text in enumerate(("审", "核", "完成"), start=1):
        run_control.record_event(
            {
                **common,
                "type": "subagent.output_delta",
                "seq": seq,
                "payload": {
                    "subagent_id": "subagent-reviewer",
                    "mode": "append",
                    "delta": text,
                    "offset": seq - 1 if seq < 3 else 2,
                },
            },
            db=db,
        )

    assert db.runs.list_events(session_id) == []
    run_control.record_event(
        {
            **common,
            "type": "subagent.complete",
            "seq": 4,
            "payload": {
                "subagent_id": "subagent-reviewer",
                "status": "completed",
                "summary": "审核完成",
            },
        },
        db=db,
    )

    events = db.runs.list_events(session_id)
    assert [event["type"] for event in events] == [
        "subagent.output_delta",
        "subagent.complete",
    ]
    assert events[0]["payload"]["mode"] == "append"
    assert events[0]["payload"]["offset"] == 0
    assert events[0]["payload"]["text"] == "审核完成"
    snapshots = build_subagent_run_snapshots(events, conversation_session_id=session_id)
    assert snapshots[0]["output_snapshot"]["text"] == "审核完成"
    assert snapshots[0]["status"] == "completed"
    db.close()


def test_subagent_stream_checkpoints_survive_parent_run_terminal_compaction(tmp_path):
    db = open_cli_session_store(tmp_path / "subagent-terminal.db")
    session_id = "subagent-terminal-session"
    run_id = "subagent-terminal-run"
    common = {
        "conversation_session_id": session_id,
        "session_id": "runtime-subagent-terminal",
        "run_id": run_id,
        "turn_id": "subagent-terminal-turn",
        "runtime_scope_key": "profile:agent-default",
    }
    run_control.record_event(
        {
            **common,
            "type": "subagent.reasoning_delta",
            "seq": 11,
            "payload": {
                "subagent_id": "reviewer",
                "mode": "append",
                "delta": "先检查结构。",
                "offset": 0,
            },
        },
        db=db,
    )
    run_control.record_event(
        {
            **common,
            "type": "subagent.tool",
            "seq": 12,
            "payload": {
                "subagent_id": "reviewer",
                "tool_id": "tool-1",
                "tool_name": "terminal",
                "status": "completed",
            },
        },
        db=db,
    )
    run_control.record_event(
        {
            **common,
            "type": "subagent.reasoning_delta",
            "seq": 13,
            "payload": {
                "subagent_id": "reviewer",
                "mode": "append",
                "delta": "再给出结论。",
                "offset": 6,
            },
        },
        db=db,
    )
    run_control.record_event(
        {
            **common,
            "type": "subagent.complete",
            "seq": 14,
            "payload": {"subagent_id": "reviewer", "status": "completed"},
        },
        db=db,
    )
    run_control.record_event(
        {
            **common,
            "type": "message.complete",
            "seq": 15,
            "payload": {"status": "complete", "text": "parent done"},
        },
        db=db,
    )

    events = db.runs.list_events(session_id, run_id=run_id, limit=100)
    assert [event["type"] for event in events] == [
        "subagent.reasoning_delta",
        "subagent.tool",
        "subagent.reasoning_delta",
        "subagent.complete",
        "message.complete",
    ]
    assert [
        event["payload"].get("text")
        for event in events
        if event["type"] == "subagent.reasoning_delta"
    ] == ["先检查结构。", "再给出结论。"]
    assert all(
        event["payload"].get("stream_checkpoint") is True
        for event in events
        if event["type"] == "subagent.reasoning_delta"
    )
    db.close()


def test_transport_disconnect_persists_one_active_stream_snapshot(tmp_path):
    class Transport:
        def write(self, _frame):
            return True

    db = open_cli_session_store(tmp_path / "disconnect.db")
    transport = Transport()
    session_id = "ordinary-disconnect-session"
    run_control.subscribe_session_with_id(
        conversation_session_id=session_id,
        transport=transport,
        db=db,
    )
    run_control.record_event(
        {
            "type": "message.delta",
            "conversation_session_id": session_id,
            "session_id": "runtime-disconnect",
            "run_id": "run-disconnect",
            "turn_id": "turn-disconnect",
            "runtime_scope_key": "profile:agent-default",
            "seq": 1,
            "payload": {"mode": "append", "delta": "partial", "offset": 0},
        },
        db=db,
    )

    run_control.detach_transport(transport)
    events = db.runs.list_events(session_id)
    assert len(events) == 1
    assert events[0]["type"] == "message.delta"
    assert events[0]["payload"]["mode"] == "append"
    assert events[0]["payload"]["text"] == "partial"
    db.close()


def test_thousand_token_stream_has_constant_durable_rows_and_run_reads(tmp_path, monkeypatch):
    db = open_cli_session_store(tmp_path / "volume.db")
    original_get = db.runs.get
    get_calls = 0

    def tracked_get(run_id):
        nonlocal get_calls
        get_calls += 1
        return original_get(run_id)

    monkeypatch.setattr(db.runs, "get", tracked_get)
    common = {
        "conversation_session_id": "volume-session",
        "session_id": "runtime-volume",
        "run_id": "run-volume",
        "turn_id": "turn-volume",
        "runtime_scope_key": "profile:agent-default",
    }
    for seq in range(1, 1001):
        run_control.record_event(
            {
                **common,
                "type": "message.delta",
                "seq": seq,
                "payload": {"mode": "append", "delta": "x", "offset": seq - 1},
            },
            db=db,
        )

    assert db.runs.list_events("volume-session") == []
    assert get_calls <= 1
    run_control.record_event(
        {
            **common,
            "type": "tool.start",
            "seq": 1001,
            "payload": {"tool_call_id": "tool-volume", "name": "read_file"},
        },
        db=db,
    )
    events = db.runs.list_events("volume-session")
    assert [event["type"] for event in events] == ["message.delta", "tool.start"]
    assert len(events[0]["payload"]["text"]) == 1000
    db.close()


def test_stream_checkpoints_preserve_tool_boundaries_in_canonical_history(tmp_path):
    db = open_cli_session_store(tmp_path / "stream-boundaries.db")
    common = {
        "conversation_session_id": "stream-boundary-session",
        "session_id": "runtime-stream-boundary",
        "run_id": "run-stream-boundary",
        "turn_id": "turn-stream-boundary",
        "runtime_scope_key": "profile:agent-default",
    }
    run_control.record_event({
        **common,
        "type": "message.delta",
        "seq": 101,
        "payload": {"mode": "append", "delta": "before", "offset": 0},
    }, db=db)
    run_control.record_event({
        **common,
        "type": "tool.start",
        "seq": 102,
        "payload": {"tool_id": "tool-1", "name": "terminal"},
    }, db=db)
    run_control.record_event({
        **common,
        "type": "message.delta",
        "seq": 103,
        "payload": {"mode": "append", "delta": "after", "offset": 6},
    }, db=db)
    run_control.record_event({
        **common,
        "type": "tool.complete",
        "seq": 104,
        "payload": {"tool_id": "tool-1", "name": "terminal", "status": "completed"},
    }, db=db)

    events = db.runs.list_events("stream-boundary-session")
    assert [event["type"] for event in events] == [
        "message.delta",
        "tool.start",
        "message.delta",
        "tool.complete",
    ]
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert events[0]["payload"]["text"] == "before"
    assert events[0]["payload"]["offset"] == 0
    assert events[2]["payload"]["text"] == "after"
    assert events[2]["payload"]["offset"] == 6
    db.close()


def test_already_persisted_terminal_broadcast_remains_durable(tmp_path):
    class Transport:
        def __init__(self):
            self.events = []

        def write(self, frame):
            self.events.append(dict(frame["params"]))
            return True

    db = open_cli_session_store(tmp_path / "durable-terminal.db")
    session_id = "durable-terminal-session"
    run_id = "durable-terminal-run"
    db.runs.upsert(
        run_id=run_id,
        session_id=session_id,
        runtime_scope_key="profile:agent-default",
        turn_id="durable-terminal-turn",
        execution_session_id="runtime-durable-terminal",
        status="running",
    )
    transport = Transport()
    run_control.subscribe_session_with_id(
        conversation_session_id=session_id,
        transport=transport,
        db=db,
    )

    terminal = run_control.terminate_run(
        conversation_session_id=session_id,
        run_id=run_id,
        turn_id="durable-terminal-turn",
        runtime_scope_key="profile:agent-default",
        execution_session_id="runtime-durable-terminal",
        status="completed",
        db=db,
    )

    assert terminal["transient"] is False
    assert terminal["seq"] > 0
    assert terminal["runtime_source_seq"] > terminal["seq"]
    assert transport.events[-1]["type"] == "message.complete"
    assert transport.events[-1]["transient"] is False
    assert transport.events[-1]["seq"] == terminal["seq"]
    _subscription_id, replay = run_control.subscribe_session_with_id(
        conversation_session_id=session_id,
        transport=None,
        after_seq=terminal["seq"],
        db=db,
    )
    assert replay == []
    db.close()
