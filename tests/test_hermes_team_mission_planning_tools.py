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

    assert created["node"]["node_id"] == "node-worker"
    assert created["node"]["assignee_member_id"] == "builder"
    assert created["node"]["assignee_profile_id"] == "profile-builder"
    assert synthesis["node"]["assignee_member_id"] == "leader"
    assert synthesis["node"]["assignee_profile_id"] == "profile-leader"
    assert edge["edge"]["to_node_id"] == "node-worker"
    assert completed["mission_status"] == "waiting_approval"
    assert completed["approval_requests"][0]["scope"] == "whole_graph"

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
            },
            parent_agent=agent,
        )
    )

    node = created["node"]
    assert node["kind"] == "worker"
    assert node["assignee_member_id"] == "builder"
    assert node["assignee_profile_id"] == "profile-builder"
    assert node["metadata"]["original_kind"] == "verification"
    assert node["metadata"]["work_type"] == "verification"
    assert db.get_team_mission_node("mission-1", "node-verification-work")["kind"] == "worker"
