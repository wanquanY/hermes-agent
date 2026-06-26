from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_state import SessionDB
from tools.registry import registry


def _json_tool_result(raw: str) -> dict:
    parsed = json.loads(raw)
    assert "error" not in parsed
    return parsed


def _json_tool_error(raw: str) -> dict:
    parsed = json.loads(raw)
    assert "error" in parsed
    return parsed


def _task_brief(label: str = "deliverable") -> dict:
    return {
        "background": f"The user requested a team mission that needs a concrete {label}.",
        "execution": [
            f"Inspect the relevant mission context for the {label}.",
            f"Produce the assigned {label} with explicit assumptions.",
        ],
        "goal": f"Deliver a complete {label} for the team mission.",
        "acceptance_criteria": [
            "The output directly addresses the assigned node objective.",
            "The output names assumptions and verification performed.",
        ],
    }


def _create_supervised_planning_context(tmp_path: Path):
    import tools.team_mission_planning_tools  # noqa: F401

    db = SessionDB(tmp_path / "state.db")
    graph = db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="Supervised mission",
        objective="Plan before execution",
        mode="supervised_mission",
        members=[
            {
                "member_id": "leader",
                "profile_id": "profile-leader",
                "role": "leader",
            },
            {
                "member_id": "builder",
                "profile_id": "profile-builder",
                "role": "builder",
            },
        ],
    )
    root_node_id = graph["nodes"][0]["node_id"]
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )
    agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="run-leader")
    return db, root_node_id, agent


def test_team_mission_planning_tools_mutate_graph_from_bound_leader_run(tmp_path: Path):
    import tools.team_mission_planning_tools  # noqa: F401

    db = SessionDB(tmp_path / "state.db")
    graph = db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="Supervised mission",
        objective="Plan before execution",
        mode="supervised_mission",
        members=[
            {
                "member_id": "leader",
                "profile_id": "profile-leader",
                "role": "leader",
            },
            {
                "member_id": "builder",
                "profile_id": "profile-builder",
                "role": "builder",
            },
        ],
    )
    root_node_id = graph["nodes"][0]["node_id"]
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )
    agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="run-leader")

    created = _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Produce the deliverable",
                "status": "ready",
                "task_brief": _task_brief("worker deliverable"),
            },
            parent_agent=agent,
        )
    )
    edge = _json_tool_result(
        registry.dispatch(
            "team_mission_edge_create",
            {
                "from_node_id": root_node_id,
                "to_node_id": "node-worker",
                "kind": "delegates",
            },
            parent_agent=agent,
        )
    )
    synthesis = _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-synthesis",
                "kind": "synthesis",
                "title": "Synthesis",
                "objective": "Review and deliver the final answer",
                "status": "todo",
                "task_brief": _task_brief("final synthesis"),
            },
            parent_agent=agent,
        )
    )
    _json_tool_result(
        registry.dispatch(
            "team_mission_edge_create",
            {
                "from_node_id": "node-worker",
                "to_node_id": "node-synthesis",
            },
            parent_agent=agent,
        )
    )
    completed = _json_tool_result(
        registry.dispatch(
            "team_mission_plan_complete",
            {},
            parent_agent=agent,
        )
    )

    assert created["dovie_event"] == "team_mission_node_created"
    assert created["node_id"] == "node-worker"
    assert "node" not in created
    node_worker = db.get_team_mission_node("mission-1", "node-worker")
    assert node_worker["assignee_member_id"] == "builder"
    assert node_worker["assignee_profile_id"] == "profile-builder"
    assert node_worker["metadata"]["task_brief"]["goal"] == "Deliver a complete worker deliverable for the team mission."
    assert node_worker["output_contract"]["requires_clarification_when_blocked"] is True
    assert edge["dovie_event"] == "team_mission_edge_created"
    synthesis_node = db.get_team_mission_node("mission-1", "node-synthesis")
    assert synthesis_node["assignee_member_id"] == "leader"
    assert synthesis_node["assignee_profile_id"] == "profile-leader"
    assert edge["edge_summary"]["to_node_id"] == "node-worker"
    assert completed["dovie_event"] == "team_mission_plan_completed"
    assert completed["mission_status"] == "waiting_approval"
    assert completed["approval_requests"][0]["scope"] == "whole_graph"
    assert "graph" not in completed

    final_graph = db.get_team_mission_graph("mission-1")
    assert final_graph["mission"]["status"] == "waiting_approval"
    approval = next(node for node in final_graph["nodes"] if node["kind"] == "approval_gate")
    assert approval["assignee_member_id"] == "leader"
    assert approval["assignee_profile_id"] == "profile-leader"
    assert ("team-mission:mission-1:approval-plan", "node-worker") in {
        (item["from_node_id"], item["to_node_id"])
        for item in final_graph["edges"]
        if item["metadata"].get("approval_gate")
    }


def test_team_mission_planning_tools_reject_unbound_run(tmp_path: Path):
    import tools.team_mission_planning_tools  # noqa: F401

    db = SessionDB(tmp_path / "state.db")
    agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="missing-run")

    result = json.loads(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "title": "Worker",
                "objective": "Do work",
            },
            parent_agent=agent,
        )
    )

    assert "not bound" in result["error"]


def test_team_mission_submit_deliverable_persists_hidden_handoff_and_returns_ack_only(tmp_path: Path):
    import tools.team_mission_deliverable_tools  # noqa: F401

    db, _root_node_id, _leader_agent = _create_supervised_planning_context(tmp_path)
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Produce structured output",
        status="running",
        output_contract={
            "format": "structured_deliverable",
            "delivery_channel": "handoff",
            "requires_explicit_handoff": True,
        },
        metadata={"task_id": "task-worker"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="run-worker")

    result = _json_tool_result(
        registry.dispatch(
            "team_mission_submit_deliverable",
            {
                "status": "completed",
                "result": "PASS",
                "summary": "Worker produced the hidden handoff.",
                "deliverable": {
                    "node_id": "node-worker",
                    "status": "completed",
                    "verification": {"passed": True},
                },
                "artifact_refs": [{"path": "/tmp/workspace/output.md", "kind": "file"}],
                "confidence": "not-a-number",
            },
            parent_agent=agent,
        )
    )

    stored = db.latest_team_mission_deliverable_for_run("run-worker")
    node = db.get_team_mission_node("mission-1", "node-worker")
    source_event_types = [
        event["payload"].get("source_event_type")
        for event in db.list_team_mission_run_events("mission-1")
        if event.get("type") == "team_mission.runtime.event"
    ]

    assert result["success"] is True
    assert result["channel"] == "handoff"
    assert result["visibility"] == "handoff"
    assert "payload" not in result
    assert stored["payload"]["verification"]["passed"] is True
    assert stored["confidence"] == 0.9
    assert stored["artifact_refs"][0]["path"] == "/tmp/workspace/output.md"
    assert node["metadata"]["last_deliverable_id"] == stored["deliverable_id"]
    assert "mission.node.deliverable.recorded" in source_event_types


def test_team_mission_submit_deliverable_marks_event_emit_failure(monkeypatch, tmp_path: Path):
    import tools.team_mission_deliverable_tools  # noqa: F401

    db, _root_node_id, _leader_agent = _create_supervised_planning_context(tmp_path)
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Produce structured output",
        status="running",
        output_contract={
            "format": "structured_deliverable",
            "delivery_channel": "handoff",
            "requires_explicit_handoff": True,
        },
        metadata={"task_id": "task-worker"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    def fail_append_team_mission_run_event(**_kwargs):
        raise RuntimeError("event log unavailable")

    monkeypatch.setattr(db, "append_team_mission_run_event", fail_append_team_mission_run_event)
    agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="run-worker")

    result = _json_tool_result(
        registry.dispatch(
            "team_mission_submit_deliverable",
            {
                "status": "completed",
                "result": "PASS",
                "summary": "Worker produced the hidden handoff.",
                "deliverable": {"node_id": "node-worker", "status": "completed"},
            },
            parent_agent=agent,
        )
    )

    stored = db.latest_team_mission_deliverable_for_run("run-worker")
    node = db.get_team_mission_node("mission-1", "node-worker")
    assert result["success"] is True
    assert stored["deliverable_id"] == result["deliverable_id"]
    assert node["metadata"]["deliverable_event_emit_failed"] is True
    assert node["metadata"]["pending_deliverable_event_id"] == stored["deliverable_id"]
    assert "event log unavailable" in node["metadata"]["deliverable_event_emit_error"]


def test_team_mission_node_heartbeat_updates_bound_node_metadata(tmp_path: Path):
    import tools.team_mission_deliverable_tools  # noqa: F401

    db, _root_node_id, _leader_agent = _create_supervised_planning_context(tmp_path)
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Long running node",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="run-worker")

    result = _json_tool_result(
        registry.dispatch(
            "team_mission_node_heartbeat",
            {"note": "halfway through validation"},
            parent_agent=agent,
        )
    )

    node = db.get_team_mission_node("mission-1", "node-worker")
    source_event_types = [
        event["payload"].get("source_event_type")
        for event in db.list_team_mission_run_events("mission-1")
        if event.get("type") == "team_mission.runtime.event"
    ]
    assert result["success"] is True
    assert result["visibility"] == "internal"
    assert node["metadata"]["heartbeat_note"] == "halfway through validation"
    assert node["metadata"]["heartbeat_at"] > 0
    assert "mission.node.heartbeat" in source_event_types


def test_team_mission_leader_tools_work_for_leader_run_in_non_planning_phase(tmp_path: Path):
    import tools.team_mission_leader_tools  # noqa: F401
    import tools.team_mission_profile_tools  # noqa: F401

    db, root_node_id, agent = _create_supervised_planning_context(tmp_path)
    root = db.get_team_mission_node("mission-1", root_node_id)
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id=root_node_id,
        kind=root["kind"],
        title=root["title"],
        objective=root["objective"],
        status="running",
        assignee_profile_id=root["assignee_profile_id"],
        assignee_profile_version_id=root["assignee_profile_version_id"],
        runtime_scope_key=root["runtime_scope_key"],
        output_contract=root["output_contract"],
        metadata={**root["metadata"], "phase": "verifying"},
        position_x=root["position_x"],
        position_y=root["position_y"],
    )

    profile = _json_tool_result(registry.dispatch("team_mission_team_profile", {}, parent_agent=agent))
    status = _json_tool_result(registry.dispatch("team_mission_status", {}, parent_agent=agent))

    assert profile["source"] == "mission_metadata_members"
    assert profile["node"]["phase"] == "verifying"
    assert {member["member_id"] for member in profile["snapshot"]["member_profiles"]} == {"leader", "builder"}
    assert status["mission_id"] == "mission-1"
    assert status["graph_summary"]["node_count"] == 1


def test_team_mission_planning_tools_reject_unknown_assignee_member_id(tmp_path: Path):
    db, _root_node_id, agent = _create_supervised_planning_context(tmp_path)

    result = _json_tool_error(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-synthesis",
                "kind": "synthesis",
                "title": "Synthesis",
                "objective": "Review and deliver the final answer",
                "status": "todo",
                "assignee_member_id": "run-leader",
            },
            parent_agent=agent,
        )
    )

    assert "assignee_member_id 'run-leader' is not a Team Mission member" in result["error"]
    assert "leader(leader)" in result["error"]
    assert db.get_team_mission_node("mission-1", "node-synthesis") == {}


def test_team_mission_planning_tools_require_structured_task_brief(tmp_path: Path):
    db, _root_node_id, agent = _create_supervised_planning_context(tmp_path)

    result = _json_tool_error(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Do the work",
                "status": "ready",
                "assignee_member_id": "builder",
            },
            parent_agent=agent,
        )
    )

    assert "task_brief is required" in result["error"]
    assert "clarify" in result["error"]
    assert db.get_team_mission_node("mission-1", "node-worker") == {}


def test_team_mission_planning_tools_reject_oversized_task_brief_items(tmp_path: Path):
    db, _root_node_id, agent = _create_supervised_planning_context(tmp_path)
    oversized_brief = _task_brief("oversized deliverable")
    oversized_brief["acceptance_criteria"] = [f"Criterion {index}" for index in range(10)]

    result = _json_tool_error(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Do the work",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": oversized_brief,
            },
            parent_agent=agent,
        )
    )

    assert "task_brief.acceptance_criteria has too many items" in result["error"]
    assert db.get_team_mission_node("mission-1", "node-worker") == {}


def test_team_mission_node_brief_append_updates_existing_node_with_budget(tmp_path: Path):
    db, _root_node_id, agent = _create_supervised_planning_context(tmp_path)
    _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Do the work",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("appendable deliverable"),
            },
            parent_agent=agent,
        )
    )

    appended = _json_tool_result(
        registry.dispatch(
            "team_mission_node_brief_append",
            {
                "node_id": "node-worker",
                "field": "constraints",
                "items": ["Keep the generated report under 500 words."],
            },
            parent_agent=agent,
        )
    )
    rejected = _json_tool_error(
        registry.dispatch(
            "team_mission_node_brief_append",
            {
                "node_id": "node-worker",
                "field": "constraints",
                "items": [f"Extra constraint {index}" for index in range(20)],
            },
            parent_agent=agent,
        )
    )

    node = db.get_team_mission_node("mission-1", "node-worker")
    assert appended["success"] is True
    assert appended["task_brief_field"] == "constraints"
    assert "Keep the generated report under 500 words." in node["metadata"]["task_brief"]["constraints"]
    assert "too many items" in rejected["error"]


def test_team_mission_planning_tools_normalize_work_type_kind(tmp_path: Path):
    db, _root_node_id, agent = _create_supervised_planning_context(tmp_path)

    created = _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-verification-work",
                "kind": "verification",
                "title": "Verify implementation details",
                "objective": "Check implementation details before final verification.",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("verification report"),
            },
            parent_agent=agent,
        )
    )

    node = db.get_team_mission_node("mission-1", "node-verification-work")
    assert created["node_id"] == "node-verification-work"
    assert "node" not in created
    assert node["kind"] == "worker"
    assert node["assignee_member_id"] == "builder"
    assert node["assignee_profile_id"] == "profile-builder"
    assert node["metadata"]["original_kind"] == "verification"
    assert node["metadata"]["work_type"] == "verification"
    assert db.get_team_mission_node("mission-1", "node-verification-work")["kind"] == "worker"


def test_team_mission_planning_tools_are_ack_only_and_idempotent(tmp_path: Path):
    db, _root_node_id, agent = _create_supervised_planning_context(tmp_path)

    first = _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Produce a bounded deliverable",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("bounded deliverable"),
                "idempotency_key": "plan:node-worker",
            },
            parent_agent=agent,
        )
    )
    retry = _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Produce a bounded deliverable",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("bounded deliverable"),
                "idempotency_key": "plan:node-worker",
            },
            parent_agent=agent,
        )
    )
    summary = _json_tool_result(registry.dispatch("team_mission_graph_summary", {}, parent_agent=agent))
    graph_slice = _json_tool_result(
        registry.dispatch(
            "team_mission_graph_slice",
            {"node_ids": ["node-worker"], "include_brief": "summary"},
            parent_agent=agent,
        )
    )

    assert first["node_id"] == "node-worker"
    assert retry["node_id"] == "node-worker"
    assert retry["idempotent"] is True
    assert "graph" not in first
    assert summary["graph_summary"]["node_count"] == 2
    assert graph_slice["graph_slice"]["nodes"][0]["node_id"] == "node-worker"
    assert "task_brief_summary" in graph_slice["graph_slice"]["nodes"][0]
    assert db.get_team_mission_node("mission-1", "node-worker-retry-would-duplicate") == {}


def test_team_mission_node_create_rejects_idempotency_key_with_different_args(tmp_path: Path):
    db, _root_node_id, agent = _create_supervised_planning_context(tmp_path)

    _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Produce a bounded deliverable",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("bounded deliverable"),
                "idempotency_key": "plan:node-worker",
            },
            parent_agent=agent,
        )
    )

    conflict = _json_tool_error(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-worker-renamed",
                "kind": "worker",
                "title": "Worker renamed",
                "objective": "Produce a bounded deliverable",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("bounded deliverable"),
                "idempotency_key": "plan:node-worker",
            },
            parent_agent=agent,
        )
    )

    assert "different arguments" in conflict["error"]
    assert db.get_team_mission_node("mission-1", "node-worker-renamed") == {}


def test_team_mission_node_create_scopes_idempotency_key_by_planner_run(tmp_path: Path):
    db, root_node_id, agent = _create_supervised_planning_context(tmp_path)
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader-2",
        session_id="session-leader-2",
        runtime_scope_key="team:mission-1:leader-2",
        role="leader",
    )
    second_agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="run-leader-2")

    first = _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-from-run-1",
                "kind": "worker",
                "title": "Worker one",
                "objective": "Produce the first bounded deliverable",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("first bounded deliverable"),
                "idempotency_key": "same-user-key",
            },
            parent_agent=agent,
        )
    )
    second = _json_tool_result(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-from-run-2",
                "kind": "worker",
                "title": "Worker two",
                "objective": "Produce the second bounded deliverable",
                "status": "ready",
                "assignee_member_id": "builder",
                "task_brief": _task_brief("second bounded deliverable"),
                "idempotency_key": "same-user-key",
            },
            parent_agent=second_agent,
        )
    )

    assert first["node_id"] == "node-from-run-1"
    assert second["node_id"] == "node-from-run-2"
    assert db.get_team_mission_node("mission-1", "node-from-run-1")
    assert db.get_team_mission_node("mission-1", "node-from-run-2")
