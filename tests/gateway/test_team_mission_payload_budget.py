from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_state import SessionDB
from hermes_team_mission.context.worker_context import TOOL_ARGS_BUDGET_CHARS
from tools.registry import registry


def _json_result(raw: str) -> dict:
    parsed = json.loads(raw)
    assert "error" not in parsed
    return parsed


def _json_error(raw: str) -> dict:
    parsed = json.loads(raw)
    assert "error" in parsed
    return parsed


def _byte_len(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _task_brief(label: str = "deliverable") -> dict:
    return {
        "background": f"Create a bounded {label} for a Team Mission node.",
        "execution": [
            f"Inspect inputs for {label}.",
            f"Produce the assigned {label}.",
        ],
        "goal": f"Complete the {label}.",
        "acceptance_criteria": [
            "The result satisfies the node objective.",
            "The result names validation performed.",
        ],
        "deliverables": [f"{label} summary"],
    }


def _context(tmp_path: Path, *, member_count: int = 2):
    import tools.team_mission_planning_tools  # noqa: F401
    import tools.team_mission_profile_tools  # noqa: F401

    members = [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}]
    for index in range(1, member_count):
        members.append({
            "member_id": f"member-{index}",
            "profile_id": f"profile-{index}",
            "role": f"role-{index}",
            "display_name": f"Member {index}",
            "profile_summary": "Handles bounded Team Mission work without returning full profile blobs.",
            "capability_tags": [f"capability-{index}", "bounded-output"],
            "default_toolsets": ["file_readonly", "terminal"],
            "strengths": ["small payloads", "explicit handoff"],
        })
    db = SessionDB(tmp_path / "state.db")
    graph = db.initialize_team_mission_from_strategy(
        mission_id="mission-budget",
        title="Payload budget mission",
        objective="Exercise Team Mission compact tool contracts.",
        mode="supervised_mission",
        members=members,
    )
    root_node_id = graph["nodes"][0]["node_id"]
    db.bind_team_mission_run(
        mission_id="mission-budget",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-budget:leader",
        role="leader",
    )
    return db, root_node_id, SimpleNamespace(_session_db=db, _hermes_active_run_id="run-leader")


def _create_worker(
    agent,
    *,
    node_id: str = "node-worker",
    kind: str = "worker",
    idempotency_key: str = "",
) -> dict:
    args = {
        "node_id": node_id,
        "kind": kind,
        "title": "Worker",
        "objective": "Produce a bounded deliverable.",
        "status": "ready",
        "task_brief": _task_brief(node_id),
    }
    if idempotency_key:
        args["idempotency_key"] = idempotency_key
    return _json_result(registry.dispatch("team_mission_node_create", args, parent_agent=agent))


def test_team_profile_default_compact_under_8kb(tmp_path: Path):
    _db, _root, agent = _context(tmp_path, member_count=16)

    result = _json_result(registry.dispatch("team_mission_team_profile", {}, parent_agent=agent))

    assert result["success"] is True
    assert _byte_len(result) < 8192
    assert len(result["snapshot"].get("member_profiles") or []) <= 12


def test_node_create_returns_ack_only_and_under_4kb(tmp_path: Path):
    _db, _root, agent = _context(tmp_path)

    result = _create_worker(agent)

    assert result["success"] is True
    assert "node" not in result
    assert "graph" not in result
    assert "task_brief" not in json.dumps(result, ensure_ascii=False)
    assert _byte_len(result) < 4096


def test_plan_complete_excludes_full_graph_and_stays_under_8kb(tmp_path: Path):
    db, root, agent = _context(tmp_path)
    _create_worker(agent, node_id="node-worker")
    _create_worker(agent, node_id="node-verifier", kind="verifier")
    _create_worker(agent, node_id="node-synthesis", kind="synthesis")
    _json_result(
        registry.dispatch(
            "team_mission_edge_create",
            {"from_node_id": root, "to_node_id": "node-worker"},
            parent_agent=agent,
        )
    )
    _json_result(
        registry.dispatch(
            "team_mission_edge_create",
            {"from_node_id": "node-worker", "to_node_id": "node-verifier"},
            parent_agent=agent,
        )
    )
    _json_result(
        registry.dispatch(
            "team_mission_edge_create",
            {"from_node_id": "node-verifier", "to_node_id": "node-synthesis"},
            parent_agent=agent,
        )
    )

    result = _json_result(registry.dispatch("team_mission_plan_complete", {}, parent_agent=agent))

    assert result["success"] is True
    assert "graph" not in result
    assert "edges" not in result
    assert "task_brief" not in json.dumps(result, ensure_ascii=False)
    assert result["graph_summary"]["node_count"] >= 2
    assert db.get_team_mission_graph("mission-budget")["mission"]["status"] == "waiting_approval"
    assert _byte_len(result) < 8192


def test_graph_summary_under_8kb_with_many_nodes(tmp_path: Path):
    _db, _root, agent = _context(tmp_path)
    for index in range(30):
        _create_worker(agent, node_id=f"node-{index}")

    result = _json_result(registry.dispatch("team_mission_graph_summary", {"limit": 50}, parent_agent=agent))

    assert result["success"] is True
    assert result["graph_summary"]["node_count"] == 31
    assert "task_brief" not in json.dumps(result, ensure_ascii=False)
    assert _byte_len(result) < 8192


def test_tool_args_over_budget_does_not_execute_mutation(tmp_path: Path):
    db, _root, agent = _context(tmp_path)
    before_node_ids = {node["node_id"] for node in db.get_team_mission_graph("mission-budget")["nodes"]}

    result = _json_error(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "node-too-large",
                "kind": "worker",
                "title": "Large",
                "objective": "Should not be persisted.",
                "status": "ready",
                "task_brief": _task_brief("too large"),
                "oversized": "x" * (TOOL_ARGS_BUDGET_CHARS + 1),
            },
            parent_agent=agent,
        )
    )
    after_node_ids = {node["node_id"] for node in db.get_team_mission_graph("mission-budget")["nodes"]}

    assert "too large" in result["error"]
    assert before_node_ids == after_node_ids
    assert "node-too-large" not in after_node_ids


def test_idempotency_key_reuses_existing_node(tmp_path: Path):
    db, _root, agent = _context(tmp_path)

    first = _create_worker(agent, node_id="node-idempotent-a", idempotency_key="create-worker-once")
    second = _create_worker(agent, node_id="node-idempotent-a", idempotency_key="create-worker-once")
    nodes = db.get_team_mission_graph("mission-budget")["nodes"]

    assert first["node_id"] == "node-idempotent-a"
    assert second["node_id"] == "node-idempotent-a"
    assert second["idempotent"] is True
    assert [node["node_id"] for node in nodes].count("node-idempotent-a") == 1
    assert "node-idempotent-b" not in {node["node_id"] for node in nodes}
