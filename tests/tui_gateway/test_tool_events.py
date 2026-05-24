import json

from tui_gateway.services.tool_events import GatewayToolEventBridge


def _bridge(events):
    sessions = {"sid": {"session_key": "stored", "tool_started_at": {}}}
    return GatewayToolEventBridge(
        sessions=sessions,
        emit=lambda event_type, sid, payload=None: events.append(
            {"type": event_type, "session_id": sid, "payload": payload or {}}
        ),
        tool_progress_enabled=lambda _sid: True,
        session_cwd=lambda _session: "/tmp",
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
        "subagent.thinking",
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
                "doxie_event": "agent_profile_test_completed",
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
    assert events[7]["payload"]["result"]["doxie_event"] == "agent_profile_test_completed"
