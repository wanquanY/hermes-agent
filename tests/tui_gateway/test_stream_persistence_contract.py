import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services import run_control
from tui_gateway.services import run_control_events
from tui_gateway.services import team_mission_activity_events
from tui_gateway.services.subagent_snapshots import (
    build_subagent_run_snapshots,
    compact_subagent_detail_events,
)


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


@pytest.mark.parametrize(
    "event_type",
    [
        "agent.terminal.output",
        "terminal.close",
        "terminal.list.request",
        "terminal.read.request",
        "terminal.write.request",
    ],
)
def test_renderer_platform_events_never_enter_conversation_ledger(tmp_path, event_type):
    db = open_cli_session_store(tmp_path / "platform-events.db")
    frame = {
        "type": event_type,
        "conversation_session_id": "session-platform",
        "session_id": "runtime-platform",
        "run_id": "run-platform",
        "turn_id": "turn-platform",
        "runtime_scope_key": "profile:agent-default",
        "seq": 91,
        "payload": {"process_id": "proc-1", "chunk": "tick\n", "request_id": "req-1"},
    }

    run_control.record_event(frame, db=db)

    assert frame["transient"] is True
    assert "seq" not in frame
    assert frame["event_domain"] == "terminal"
    assert db.runs.list_events("session-platform") == []
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


def test_checkpoint_race_does_not_redeliver_live_stream_prefix():
    subscription = {}
    common = {
        "type": "message.delta",
        "run_id": "run-race",
        "turn_id": "turn-race",
        "runtime_scope_key": "profile:default",
    }
    run_control_events.remember_stream_delivery(
        subscription,
        {**common, "transient": True, "payload": {"mode": "append", "delta": "你"}},
    )
    run_control_events.remember_stream_delivery(
        subscription,
        {**common, "transient": True, "payload": {"mode": "append", "delta": "好"}},
    )

    checkpoint = {
        **common,
        "seq": 1,
        "payload": {
            "mode": "append",
            "offset": 0,
            "delta": "你好",
            "text": "你好",
            "stream_checkpoint": True,
        },
    }

    assert run_control_events.delta_event_for_subscription(subscription, checkpoint) is None


def test_team_activity_projection_rejects_unjournaled_transient_event():
    with pytest.raises(ValueError, match="persisted canonical journal event"):
        team_mission_activity_events.project_run_event_for_subscription(
            {
                "type": "message.delta",
                "transient": True,
                "runtime_source_seq": 1,
                "payload": {"mode": "append", "offset": 0, "delta": "我"},
            },
            "mission:mission-1",
            mission_id_value="mission-1",
        )


def test_nested_text_stream_identity_keeps_message_segments_independent():
    subscription = {}
    common = {
        "type": "message.delta",
        "run_id": "run-segments",
        "turn_id": "turn-segments",
        "runtime_scope_key": "team:mission-1:node:worker",
    }
    run_control_events.remember_stream_delivery(
        subscription,
        {
            **common,
            "transient": True,
            "text_stream": {
                "mode": "append",
                "offset": 0,
                "delta": "first",
                "client_message_id": "segment-1",
            },
        },
    )
    second_segment_checkpoint = {
        **common,
        "seq": 2,
        "text_stream": {
            "mode": "append",
            "offset": 0,
            "delta": "second",
            "client_message_id": "segment-2",
        },
        "payload": {
            "stream_checkpoint": True,
            "text_stream": {
                "mode": "append",
                "offset": 0,
                "delta": "second",
                "client_message_id": "segment-2",
            },
        },
    }

    assert run_control_events.delta_event_for_subscription(
        subscription,
        second_segment_checkpoint,
    ) == second_segment_checkpoint


def test_reasoning_available_seals_reasoning_after_its_durable_checkpoint(tmp_path):
    db = open_cli_session_store(tmp_path / "reasoning-complete.db")
    session_id = "reasoning-complete-session"
    run_id = "reasoning-complete-run"
    client_message_id = "reasoning-complete-turn:assistant-segment:0"
    common = {
        "conversation_session_id": session_id,
        "session_id": "reasoning-complete-runtime",
        "run_id": run_id,
        "turn_id": "reasoning-complete-turn",
        "runtime_scope_key": "profile:agent-default",
    }
    run_control.record_event(
        {
            **common,
            "type": "reasoning.delta",
            "seq": 1,
            "payload": {
                "mode": "append",
                "delta": "先分析问题。",
                "offset": 0,
                "client_message_id": client_message_id,
            },
        },
        db=db,
    )

    assert db.runs.list_events(session_id, run_id=run_id) == []

    run_control.record_event(
        {
            **common,
            "type": "reasoning.available",
            "seq": 2,
            "payload": {
                "mode": "replace",
                "text": "先分析问题。",
                "client_message_id": client_message_id,
            },
        },
        db=db,
    )

    events = db.runs.list_events(session_id, run_id=run_id)
    assert [event["type"] for event in events] == [
        "reasoning.delta",
        "reasoning.available",
    ]
    assert events[0]["payload"]["stream_checkpoint"] is True
    assert events[0]["payload"]["text"] == "先分析问题。"
    assert events[1]["payload"]["mode"] == "replace"
    assert events[1]["payload"]["text"] == "先分析问题。"
    assert all(
        event["payload"]["client_message_id"] == client_message_id
        for event in events
    )
    db.close()


@pytest.mark.parametrize(
    ("session_id", "runtime_scope_key"),
    [
        ("ordinary-interim-session", "profile:agent-default"),
        ("leader-interim-session", "team:conversation-1:leader-conversation"),
        ("member-interim-session", "team:conversation-1:member:agent-reviewer"),
    ],
)
def test_interim_message_seals_covered_delta_after_its_durable_checkpoint(
    tmp_path,
    session_id,
    runtime_scope_key,
):
    db = open_cli_session_store(tmp_path / f"{session_id}.db")
    run_id = "message-interim-run"
    client_message_id = "message-interim-turn:assistant-segment:3"
    common = {
        "conversation_session_id": session_id,
        "session_id": "message-interim-runtime",
        "run_id": run_id,
        "turn_id": "message-interim-turn",
        "runtime_scope_key": runtime_scope_key,
    }
    delta = {
        **common,
        "type": "message.delta",
        "payload": {
            "mode": "append",
            "delta": "先说明处理结果，再继续调用工具。",
            "offset": 0,
            "client_message_id": client_message_id,
        },
    }
    run_control.record_event(delta, db=db)

    assert delta["transient"] is True
    assert db.runs.list_events(session_id, run_id=run_id) == []

    interim = {
        **common,
        "type": "message.interim",
        "payload": {
            "text": "先说明处理结果，再继续调用工具。",
            "already_streamed": True,
            "client_message_id": client_message_id,
        },
    }
    run_control.record_event(interim, db=db)

    events = db.runs.list_events(session_id, run_id=run_id)
    assert [event["type"] for event in events] == [
        "message.delta",
        "message.interim",
    ]
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["payload"]["stream_checkpoint"] is True
    assert events[0]["payload"]["text"] == interim["payload"]["text"]
    assert events[0]["runtime_source_seq"] < events[1]["runtime_source_seq"]

    run_control.record_event(
        {
            **common,
            "type": "tool.start",
            "payload": {"tool_call_id": "tool-after-interim", "name": "terminal"},
        },
        db=db,
    )
    events = db.runs.list_events(session_id, run_id=run_id)
    assert [event["type"] for event in events] == [
        "message.delta",
        "message.interim",
        "tool.start",
    ]
    db.close()


def test_checkpoint_race_delivers_only_unseen_utf16_suffix():
    subscription = {}
    common = {
        "type": "message.delta",
        "run_id": "run-partial",
        "turn_id": "turn-partial",
        "runtime_scope_key": "profile:default",
    }
    run_control_events.remember_stream_delivery(
        subscription,
        {
            **common,
            "transient": True,
            "payload": {"mode": "append", "offset": 0, "delta": "A😀"},
        },
    )
    checkpoint = {
        **common,
        "seq": 1,
        "payload": {
            "mode": "append",
            "offset": 0,
            "delta": "A😀中",
            "text": "A😀中",
            "stream_checkpoint": True,
        },
    }

    projected = run_control_events.delta_event_for_subscription(subscription, checkpoint)

    assert projected is not None
    assert projected["seq"] == 1
    assert projected["payload"]["offset"] == 3
    assert projected["payload"]["delta"] == "中"


def test_durable_first_stream_delivery_suppresses_late_live_fragments():
    subscription = {}
    common = {
        "type": "message.delta",
        "run_id": "run-durable-first",
        "turn_id": "turn-durable-first",
        "runtime_scope_key": "team:mission-1:node:root",
    }
    checkpoint = {
        **common,
        "seq": 10,
        "payload": {
            "mode": "append",
            "offset": 0,
            "delta": "A😀中",
            "text": "A😀中",
            "client_message_id": "turn-durable-first:assistant-segment:0",
            "stream_checkpoint": True,
        },
    }
    assert run_control_events.delta_event_for_subscription(subscription, checkpoint) == checkpoint
    run_control_events.remember_stream_delivery(subscription, checkpoint)

    for offset, fragment in ((0, "A😀"), (3, "中")):
        late_live = {
            **common,
            "transient": True,
            "runtime_source_seq": 100 + offset,
            "payload": {
                "mode": "append",
                "offset": offset,
                "delta": fragment,
                "client_message_id": "turn-durable-first:assistant-segment:0",
            },
        }
        assert run_control_events.delta_event_for_subscription(subscription, late_live) is None


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


def test_subagent_detail_compaction_preserves_causal_order_and_stream_contract():
    events = [
        {
            "type": "subagent.output_delta",
            "seq": 11,
            "runtime_source_seq": 1_000_011,
            "run_id": "parent-run",
            "payload": {
                "subagent_id": "reviewer",
                "mode": "append",
                "offset": 0,
                "text": "before",
            },
        },
        {
            "type": "subagent.output_delta",
            "seq": 12,
            "runtime_source_seq": 1_000_012,
            "run_id": "parent-run",
            "payload": {
                "subagent_id": "reviewer",
                "mode": "append",
                "offset": 6,
                "text": " tool",
            },
        },
        {
            "type": "subagent.tool",
            "seq": 13,
            "runtime_source_seq": 1_000_013,
            "run_id": "parent-run",
            "payload": {
                "subagent_id": "reviewer",
                "tool_call_id": "delegate-call",
                "tool_id": "child-tool",
                "tool_name": "terminal",
            },
        },
        {
            "type": "subagent.output_delta",
            "seq": 14,
            "runtime_source_seq": 1_000_014,
            "run_id": "parent-run",
            "payload": {
                "subagent_id": "reviewer",
                "mode": "append",
                "offset": 11,
                "text": "after",
            },
        },
    ]

    compacted = compact_subagent_detail_events(events)

    assert [event["type"] for event in compacted] == [
        "subagent.output_delta",
        "subagent.tool",
        "subagent.output_delta",
    ]
    assert [event["seq"] for event in compacted] == [12, 13, 14]
    assert [event["runtime_source_seq"] for event in compacted] == [
        1_000_012,
        1_000_013,
        1_000_014,
    ]
    assert compacted[0]["payload"] == {
        "text": "before tool",
        "mode": "append",
        "offset": 0,
        "subagent_id": "reviewer",
    }


def test_subagent_detail_compaction_does_not_concatenate_cumulative_snapshots():
    events = [
        {
            "type": "subagent.output_delta",
            "seq": 21,
            "runtime_source_seq": 1_000_021,
            "payload": {
                "subagent_id": "reviewer",
                "mode": "snapshot",
                "offset": 0,
                "text": "hello",
            },
        },
        {
            "type": "subagent.output_delta",
            "seq": 22,
            "runtime_source_seq": 1_000_022,
            "payload": {
                "subagent_id": "reviewer",
                "mode": "snapshot",
                "offset": 0,
                "text": "hello world",
            },
        },
    ]

    compacted = compact_subagent_detail_events(events)

    assert [event["payload"]["text"] for event in compacted] == ["hello", "hello world"]
    assert [event["payload"]["mode"] for event in compacted] == ["snapshot", "snapshot"]
