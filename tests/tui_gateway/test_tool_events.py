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


def test_tool_complete_emits_deleted_artifact_event(monkeypatch):
    events = []

    def fake_record_artifacts_from_tool_complete(**_kwargs):
        return [
            {
                "id": "artifact-old",
                "path": "/tmp/old.md",
                "relative_path": "old.md",
                "title": "old.md",
                "mime_type": "text/markdown",
                "size_bytes": 12,
                "workspace": {"id": "workspace-test", "path": "/tmp"},
                "origin": {
                    "event": "tool.complete",
                    "tool_id": "tool-delete",
                    "tool_name": "patch",
                    "operation": "deleted",
                },
                "operation": "deleted",
                "availability": "missing",
            }
        ]

    monkeypatch.setattr(
        "tui_gateway.services.tool_events.record_artifacts_from_tool_complete",
        fake_record_artifacts_from_tool_complete,
    )
    bridge = _bridge(events, tool_progress_enabled=False)

    bridge.on_tool_complete(
        "sid",
        "tool-delete",
        "patch",
        {"mode": "patch"},
        json.dumps({"success": True, "files_deleted": ["/tmp/old.md"]}),
    )

    assert [event["type"] for event in events] == ["artifact.deleted"]
    assert events[0]["payload"]["operation"] == "deleted"
    assert events[0]["payload"]["origin"]["operation"] == "deleted"


def test_tool_output_risk_event_never_contains_raw_result():
    events = []
    bridge = _bridge(events)

    bridge.on_tool_progress(
        "sid",
        "tool.output_risk",
        "web_extract",
        tool_call_id="tool-risk",
        risk_metadata={
            "risk": "high",
            "findings": ["prompt_injection"],
            "redacted": False,
        },
    )

    assert events == [
        {
            "type": "tool.output_risk",
            "session_id": "sid",
            "payload": {
                "tool_id": "tool-risk",
                "name": "web_extract",
                "risk": "high",
                "findings": ["prompt_injection"],
                "redacted": False,
            },
        }
    ]
    assert "result" not in events[0]["payload"]


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
        "subagent.spawn_requested",
        None,
        "hello",
        None,
        subagent_id="child-1",
        goal="hello",
        context="## SOUL.md\nhidden profile",
        dispatch_message="hello\n\n## SOUL.md\nhidden profile",
        delegation_tool_name="test_agent_profile",
        tool_count=0,
    )
    bridge.on_tool_progress(
        "sid",
        "subagent.start",
        None,
        "hello",
        None,
        subagent_id="child-1",
        goal="hello",
        context="## SOUL.md\nhidden profile",
        dispatch_message="hello\n\n## SOUL.md\nhidden profile",
        delegation_tool_name="test_agent_profile",
        tool_count=0,
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
        mode="append",
        delta="\npartial answer",
        offset=0,
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
        "terminal",
        "python test_agent_validation.py",
        {"command": "python test_agent_validation.py"},
        subagent_id="child-1",
        goal="hello",
        delegation_tool_name="test_agent_profile",
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
        "agent_profile_test.progress",
        "agent_profile_test.progress",
        "agent_profile_test.output_delta",
        "agent_profile_test.progress",
        "agent_profile_test.tool",
        "agent_profile_test.thinking",
        "tool.complete",
        "agent_profile_test.complete",
    ]
    assert events[2]["payload"]["text"] == "hello"
    assert "context" not in events[2]["payload"]
    assert "dispatch_message" not in events[2]["payload"]
    assert events[3]["payload"]["text"] == "hello"
    assert "context" not in events[3]["payload"]
    assert "dispatch_message" not in events[3]["payload"]
    assert events[4]["payload"]["text"] == "\npartial answer"
    assert events[4]["payload"]["mode"] == "append"
    assert events[4]["payload"]["delta"] == "\npartial answer"
    assert events[4]["payload"]["offset"] == 0
    assert events[4]["payload"]["tool_name"] == "test_agent_profile"
    assert events[5]["payload"]["text"] == "preparing draft runtime"
    assert events[6]["payload"]["tool_name"] == "terminal"
    assert events[6]["payload"]["tool_preview"] == "python test_agent_validation.py"
    assert events[6]["payload"]["arguments"] == {"command": "python test_agent_validation.py"}
    assert events[7]["payload"]["text"] == "thinking"
    assert events[9]["payload"]["result"]["dovie_event"] == "agent_profile_test_completed"


def test_presentation_generation_keeps_structured_result():
    events = []
    bridge = _bridge(events)

    bridge.on_tool_complete(
        "sid",
        "tool-presentation",
        "dovie_presentation_generate",
        {"title": "Review", "page_prompts": ["Cover"]},
        json.dumps(
            {
                "dovie_event": "presentation_generation_completed",
                "status": "completed",
                "title": "Review",
                "total_pages": 1,
                "output_path": "/tmp/review.pptx",
                "artifacts": [],
            }
        ),
    )

    tool_complete = next(event for event in events if event["type"] == "tool.complete")
    assert tool_complete["payload"]["result"]["status"] == "completed"
    assert tool_complete["payload"]["result"]["output_path"] == "/tmp/review.pptx"


def test_presentation_slide_regeneration_keeps_structured_result():
    events = []
    bridge = _bridge(events)

    bridge.on_tool_complete(
        "sid",
        "tool-presentation-revision",
        "dovie_presentation_regenerate_slide",
        {
            "presentation_path": "/tmp/review.pptx",
            "page_number": 2,
            "prompt": "Revised comparison",
        },
        json.dumps(
            {
                "dovie_event": "presentation_slide_regenerated",
                "status": "completed",
                "output_path": "/tmp/review.pptx",
                "page_number": 2,
                "revision": 1,
                "artifacts": [],
            }
        ),
    )

    tool_complete = next(event for event in events if event["type"] == "tool.complete")
    assert tool_complete["payload"]["result"]["page_number"] == 2
    assert tool_complete["payload"]["result"]["revision"] == 1


def test_subagent_events_preserve_persisted_activity_graph_identity():
    events = []
    bridge = _bridge(events)

    bridge.on_tool_progress(
        "sid",
        "subagent.start",
        None,
        "inspect repository",
        None,
        subagent_id="child-1",
        delegate_call_id="delegate-1",
        activity_id="activity-child",
        delegation_activity_id="activity-dispatch",
        owner_activity_id="activity-parent",
    )

    assert events == [
        {
            "type": "subagent.start",
            "session_id": "sid",
            "payload": {
                "task_count": 1,
                "task_index": 0,
                "activity_id": "activity-child",
                "delegation_activity_id": "activity-dispatch",
                "owner_activity_id": "activity-parent",
                "subagent_id": "child-1",
                "delegate_call_id": "delegate-1",
                "text": "inspect repository",
            },
        }
    ]


def test_interrupted_session_still_emits_subagent_terminal_fact():
    events = []
    sessions = {
        "sid": {
            "session_key": "stored",
            "active_run_id": "run-1",
            "active_turn_id": "turn-1",
            "interrupted_run_id": "run-1",
            "interrupted_turn_id": "turn-1",
        }
    }
    bridge = GatewayToolEventBridge(
        sessions=sessions,
        emit=lambda event_type, sid, payload=None: events.append(
            {"type": event_type, "session_id": sid, "payload": payload or {}}
        ),
        tool_progress_enabled=lambda _sid: True,
        session_cwd=lambda _session: "/tmp",
    )

    bridge.on_tool_progress(
        "sid",
        "subagent.reasoning_delta",
        preview="late token",
        subagent_id="sa-1",
    )
    bridge.on_tool_progress(
        "sid",
        "subagent.complete",
        preview="stopped",
        subagent_id="sa-1",
        status="interrupted",
        summary="stopped",
    )

    assert [event["type"] for event in events] == ["subagent.complete"]
    assert events[0]["payload"]["status"] == "interrupted"
    assert events[0]["payload"]["subagent_id"] == "sa-1"


def test_late_subagent_terminal_prefers_immutable_origin_over_new_active_turn():
    events = []
    sessions = {
        "sid": {
            "session_key": "stored",
            "active_run_id": "run-new",
            "active_turn_id": "turn-new",
            "active_runtime_scope_key": "profile:new",
            "pending_turn": {"client_message_id": "client-new"},
        }
    }
    bridge = GatewayToolEventBridge(
        sessions=sessions,
        emit=lambda event_type, sid, payload=None: events.append(
            {"type": event_type, "session_id": sid, "payload": payload or {}}
        ),
        tool_progress_enabled=lambda _sid: True,
        session_cwd=lambda _session: "/tmp",
    )

    bridge.on_tool_progress(
        "sid",
        "subagent.complete",
        preview="done",
        subagent_id="sa-old",
        status="completed",
        summary="done",
        run_id="run-original",
        turn_id="turn-original",
        client_message_id="client-original",
        runtime_scope_key="profile:original",
        activity_id="act-agent_dispatch:original",
    )

    payload = events[0]["payload"]
    assert payload["run_id"] == "run-original"
    assert payload["turn_id"] == "turn-original"
    assert payload["client_message_id"] == "client-original"
    assert payload["runtime_scope_key"] == "profile:original"
    assert payload["activity_id"] == "act-agent_dispatch:original"


def test_agent_profile_design_context_emits_structured_complete_when_tool_progress_disabled():
    events = []
    bridge = _bridge(events, tool_progress_enabled=False)

    bridge.on_tool_complete(
        "sid",
        "tool-1",
        "design_agent_profile",
        {"operation": "inspect_context"},
        json.dumps(
            {
                "dovie_event": "agent_profile_design_context",
                "catalogKind": "overview",
                "catalogQueries": {"toolsets": "inspect toolsets"},
                "rules": {"allowedCategories": ["工作"]},
            }
        ),
    )

    assert [event["type"] for event in events] == ["tool.complete"]
    assert events[0]["payload"]["result"]["dovie_event"] == "agent_profile_design_context"
    assert events[0]["payload"]["result"]["catalogKind"] == "overview"


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
                    "skip_remaining_tool_calls": True,
                    "require_followup_response": False,
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


def test_team_mission_planning_tools_emit_structured_complete_when_tool_progress_disabled():
    events = []
    bridge = _bridge(events, tool_progress_enabled=False)

    bridge.on_tool_complete(
        "sid",
        "tool-1",
        "team_mission_node_create",
        {"node_id": "node-1"},
        json.dumps(
            {
                "dovie_event": "team_mission_node_created",
                "success": True,
                "mission_id": "mission-1",
                "node": {"node_id": "node-1", "title": "Plan node"},
                "graph_summary": {"node_count": 2},
                "graph": {"large": "raw graph should not be forwarded"},
            }
        ),
    )
    bridge.on_tool_complete(
        "sid",
        "tool-2",
        "team_mission_edge_create",
        {"from_node_id": "node-1", "to_node_id": "node-2"},
        json.dumps(
            {
                "dovie_event": "team_mission_edge_created",
                "success": True,
                "mission_id": "mission-1",
                "edge": {"edge_id": "edge-1", "from_node_id": "node-1", "to_node_id": "node-2"},
                "graph_summary": {"edge_count": 1},
            }
        ),
    )
    bridge.on_tool_complete(
        "sid",
        "tool-3",
        "team_mission_plan_complete",
        {},
        json.dumps(
            {
                "dovie_event": "team_mission_plan_completed",
                "success": True,
                "mission_id": "mission-1",
                "mission_status": "waiting_approval",
                "approval_requests": [{"scope": "whole_graph"}],
                "auto_start_ready_nodes": False,
                "graph_summary": {"node_count": 3},
                "graph": {"large": "raw graph should not be forwarded"},
            }
        ),
    )

    assert [event["type"] for event in events] == ["tool.complete", "tool.complete", "tool.complete"]
    assert events[0]["payload"]["result"] == {
        "dovie_event": "team_mission_node_created",
        "success": True,
        "mission_id": "mission-1",
        "node": {"node_id": "node-1", "title": "Plan node"},
        "graph_summary": {"node_count": 2},
    }
    assert events[1]["payload"]["result"]["dovie_event"] == "team_mission_edge_created"
    assert events[2]["payload"]["result"]["dovie_event"] == "team_mission_plan_completed"
    assert "graph" not in events[2]["payload"]["result"]


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


def test_tool_generating_emits_stable_invocation_identity():
    events = []
    bridge = _bridge(events)
    callbacks = bridge.agent_callbacks(
        "sid",
        block=lambda *_args, **_kwargs: "",
        status_update=lambda *_args, **_kwargs: None,
    )

    callbacks["tool_gen_callback"]("write_file", "call-write-1")

    assert events == [
        {
            "type": "tool.generating",
            "session_id": "sid",
            "payload": {
                "name": "write_file",
                "tool_id": "call-write-1",
            },
        }
    ]


def test_aborted_tool_generation_emits_terminal_failure():
    events = []
    bridge = _bridge(events)
    callbacks = bridge.agent_callbacks(
        "sid",
        block=lambda *_args, **_kwargs: "",
        status_update=lambda *_args, **_kwargs: None,
    )

    callbacks["tool_gen_callback"]("write_file", "call-write-1")
    callbacks["tool_gen_abort_callback"](
        "write_file",
        "call-write-1",
        "failed",
        "upstream connection closed",
        "provider_stream_aborted",
    )

    assert [event["type"] for event in events] == [
        "tool.generating",
        "tool.complete",
    ]
    assert events[1]["payload"] == {
        "tool_id": "call-write-1",
        "name": "write_file",
        "status": "failed",
        "error": "upstream connection closed",
        "error_code": "provider_stream_aborted",
        "result_text": "upstream connection closed",
        "summary": "Tool generation stopped before execution",
    }
