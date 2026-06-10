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
