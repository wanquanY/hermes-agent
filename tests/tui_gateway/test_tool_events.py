import json

from tui_gateway.services.tool_events import GatewayToolEventBridge


def _bridge(events, *, tool_progress_enabled=True, before_tool_boundary=None):
    sessions = {"sid": {"session_key": "stored", "tool_started_at": {}}}
    return GatewayToolEventBridge(
        sessions=sessions,
        emit=lambda event_type, sid, payload=None: events.append(
            {"type": event_type, "session_id": sid, "payload": payload or {}}
        ),
        tool_progress_enabled=lambda _sid: tool_progress_enabled,
        session_cwd=lambda _session: "/tmp",
        before_tool_boundary=before_tool_boundary,
    )


def test_agent_profile_test_uses_dedicated_stream_events():
    events = []
    bridge = _bridge(events)

    bridge.on_tool_start(
        "sid",
        "tool-1",
        "test_agent_profile",
        {"draftId": "draft-1", "message": "hello"},
    )
    bridge.on_tool_progress(
        "sid",
        "subagent.output_delta",
        "test_agent_profile",
        "\npartial answer",
        None,
        subagent_id="child-1",
        goal="hello",
        tool_count=0,
    )
    bridge.on_tool_progress(
        "sid",
        "subagent.progress",
        "test_agent_profile",
        "preparing draft runtime",
        None,
        subagent_id="child-1",
        goal="hello",
        tool_count=0,
    )
    bridge.on_tool_progress(
        "sid",
        "subagent.tool",
        "test_agent_profile",
        "read draft memory",
        None,
        subagent_id="child-1",
        goal="hello",
        tool_count=1,
    )
    bridge.on_tool_progress(
        "sid",
        "subagent.reasoning_delta",
        "test_agent_profile",
        "thinking",
        None,
        subagent_id="child-1",
        goal="hello",
        tool_count=1,
    )
    bridge.on_tool_complete(
        "sid",
        "tool-1",
        "test_agent_profile",
        {"draftId": "draft-1", "message": "hello"},
        json.dumps(
            {
                "dovie_event": "agent_profile_test_completed",
                "draftId": "draft-1",
                "response": "final answer",
                "status": "completed",
            }
        ),
    )

    assert [event["type"] for event in events] == [
        "tool.start",
        "agent_profile_test.start",
        "agent_profile_test.output_delta",
        "agent_profile_test.progress",
        "agent_profile_test.tool",
        "agent_profile_test.thinking",
        "tool.complete",
        "agent_profile_test.complete",
    ]
    assert events[2]["payload"]["text"] == "\npartial answer"
    assert events[2]["payload"]["tool_name"] == "test_agent_profile"
    assert events[3]["payload"]["text"] == "preparing draft runtime"
    assert events[4]["payload"]["tool_preview"] == "read draft memory"
    assert events[5]["payload"]["text"] == "thinking"
    assert events[7]["payload"]["result"]["dovie_event"] == "agent_profile_test_completed"


def test_team_mission_start_task_emits_structured_complete_when_tool_progress_disabled():
    events = []
    bridge = _bridge(events, tool_progress_enabled=False)

    bridge.on_tool_complete(
        "sid",
        "tool-1",
        "team_mission_start_task",
        {"objective": "create a tiny test task"},
        json.dumps(
            {
                "success": True,
                "mission_id": "mission-1",
                "conversation_id": "conversation-1",
                "task_id": "task-1",
                "submission_status": "accepted",
                "task_status": "planning",
                "node": {"node_id": "node-1"},
                "run": {"run_id": "run-node-1"},
                "graph_summary": {"node_count": 1},
                "await_final_deliverable": True,
                "hermes_control": {
                    "kind": "team_mission_started",
                    "end_current_turn": True,
                    "await_final_deliverable": True,
                },
            }
        ),
    )

    assert [event["type"] for event in events] == ["tool.complete"]
    result = events[0]["payload"]["result"]
    assert result["dovie_event"] == "team_mission_started"
    assert result["mission_id"] == "mission-1"
    assert result["conversation_id"] == "conversation-1"
    assert result["node"] == {"node_id": "node-1"}
    assert result["run"] == {"run_id": "run-node-1"}


def test_tool_boundary_hook_runs_even_when_progress_events_are_disabled():
    events = []
    boundaries = []
    bridge = _bridge(
        events,
        tool_progress_enabled=False,
        before_tool_boundary=lambda sid, event_type: boundaries.append((sid, event_type)),
    )

    callbacks = bridge.agent_callbacks(
        "sid",
        block=lambda *_args, **_kwargs: "",
        status_update=lambda *_args, **_kwargs: None,
    )
    callbacks["tool_gen_callback"]("terminal")
    callbacks["tool_start_callback"]("tool-1", "terminal", {"command": "pwd"})

    assert events == []
    assert boundaries == [
        ("sid", "tool.generating"),
        ("sid", "tool.start"),
    ]
