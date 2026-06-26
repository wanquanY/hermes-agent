from pathlib import Path

import pytest

from hermes_state import SessionDB
from hermes_team_mission.domain.modes import (
    MODE_AUTONOMOUS_MISSION,
    MODE_DISCUSSION,
    MODE_MANUAL_GRAPH,
    MODE_SUPERVISED_MISSION,
    TeamMissionNodeSpec,
    strategy_for_mode,
)


MEMBERS = [
    {
        "member_id": "leader",
        "profile_id": "profile-leader",
        "display_name": "Leader",
        "role": "leader",
    },
    {
        "member_id": "worker-a",
        "profile_id": "profile-worker-a",
        "display_name": "Worker A",
        "role": "worker",
    },
    {
        "member_id": "worker-b",
        "profile_id": "profile-worker-b",
        "display_name": "Worker B",
        "role": "worker",
    },
]


DOVIE_MEMBERS = [
    {
        "memberId": "member-leader",
        "agentProfileId": "profile-leader",
        "agentProfileVersionId": "version-leader",
        "displayName": "Leader",
        "role": "lead",
        "dovieProfile": {
            "id": "profile-leader",
            "agentProfileVersionId": "version-leader",
            "runtimeScopeKey": "profile:profile-leader:version:version-leader",
            "hermesHomePath": "/tmp/profile-leader-runtime",
        },
    },
    {
        "memberId": "member-worker",
        "agentProfileId": "profile-worker",
        "agentProfileVersionId": "version-worker",
        "displayName": "Worker",
        "role": "worker",
        "dovieProfile": {
            "id": "profile-worker",
            "agentProfileVersionId": "version-worker",
            "runtimeScopeKey": "profile:profile-worker:version:version-worker",
            "hermesHomePath": "/tmp/profile-worker-runtime",
        },
    },
]


def test_supervised_strategy_preserves_dovie_member_profile_identity():
    strategy = strategy_for_mode(MODE_SUPERVISED_MISSION)

    patch = strategy.initialize_graph(
        mission_id="mission-dovie",
        title="执行任务",
        objective="规划后审批再执行",
        members=DOVIE_MEMBERS,
    )

    root = patch.nodes[0]
    assert root.assignee_profile_id == "profile-leader"
    assert root.assignee_profile_version_id == "version-leader"


def test_discussion_strategy_creates_parallel_contribution_nodes():
    strategy = strategy_for_mode(MODE_DISCUSSION)

    patch = strategy.initialize_graph(
        mission_id="mission-1",
        title="讨论 AI 方向",
        objective="形成观点和建议",
        members=MEMBERS,
    )

    assert patch.mission_status == "running"
    assert patch.start_leader is True
    assert patch.auto_start_ready_nodes is True
    assert patch.requires_whole_graph_approval is False
    assert strategy.risk_policy(risk_level="high").action == "deny"

    nodes_by_kind = {}
    for node in patch.nodes:
        nodes_by_kind.setdefault(node.kind, []).append(node)
    assert [node.status for node in nodes_by_kind["discussion"]] == ["ready", "ready"]
    assert nodes_by_kind["synthesis"][0].status == "todo"
    assert strategy.can_mutate_graph(actor="leader", phase="discussion") is True


def test_supervised_strategy_waits_for_whole_graph_approval_after_planning():
    strategy = strategy_for_mode(MODE_SUPERVISED_MISSION)

    patch = strategy.initialize_graph(
        mission_id="mission-1",
        title="执行任务",
        objective="规划后审批再执行",
        members=MEMBERS,
    )
    actions = strategy.on_plan_completed(
        mission_id="mission-1",
        planned_nodes=(
            TeamMissionNodeSpec(
                node_id="node-worker",
                kind="worker",
                title="执行节点",
                status="ready",
                metadata={"risk_level": "low"},
            ),
        ),
    )

    assert patch.mission_status == "planning"
    assert patch.start_leader is True
    assert patch.requires_whole_graph_approval is True
    assert strategy.requires_whole_graph_approval() is True
    assert actions.mission_status == "waiting_approval"
    assert actions.auto_start_ready_nodes is False
    assert actions.approval_requests[0]["scope"] == "whole_graph"
    assert any(node.kind == "approval_gate" for node in actions.nodes)
    assert ("team-mission:mission-1:approval-plan", "node-worker") in {
        (edge.from_node_id, edge.to_node_id)
        for edge in actions.edges
        if edge.metadata.get("approval_gate")
    }
    assert actions.events[0]["type"] == "mission.approval.requested"
    leader_text = strategy.leader_start_text(
        mission_id="mission-1",
        title="执行任务",
        objective="规划后审批再执行",
        members=MEMBERS,
    )
    assert "team_mission_node_create" in leader_text
    assert "team_mission_team_profile" in leader_text
    assert "team_mission_plan_complete" in leader_text
    assert "task_brief.background" in leader_text
    assert "task_brief.execution" in leader_text
    assert "task_brief.acceptance_criteria" in leader_text
    assert "Hermes" not in leader_text
    assert "DoXie team Leader Planner" in leader_text
    assert "Keep the same persona, identity, tone, and memory as the underlying DoXie profile" in leader_text
    assert "worker-a" not in leader_text
    assert "profile-worker-a" not in leader_text
    assert "Worker A" not in leader_text
    assert "best_for=" not in leader_text
    assert leader_text != "规划后审批再执行"
    assert strategy.can_mutate_graph(actor="leader", phase="planning") is True
    assert strategy.can_mutate_graph(actor="leader", phase="running") is False


def test_autonomous_strategy_auto_starts_only_low_risk_ready_nodes():
    strategy = strategy_for_mode(MODE_AUTONOMOUS_MISSION)

    patch = strategy.initialize_graph(
        mission_id="mission-1",
        title="自主执行",
        objective="自动推进低风险节点",
        members=MEMBERS,
    )
    actions = strategy.on_plan_completed(
        mission_id="mission-1",
        planned_nodes=(
            TeamMissionNodeSpec(
                node_id="node-low",
                kind="worker",
                title="低风险节点",
                status="ready",
                metadata={"risk_level": "low"},
            ),
            TeamMissionNodeSpec(
                node_id="node-high",
                kind="worker",
                title="高风险节点",
                status="ready",
                metadata={"risk_level": "high"},
            ),
        ),
    )

    assert patch.mission_status == "planning"
    assert patch.requires_whole_graph_approval is False
    assert actions.mission_status == "running"
    assert actions.start_node_ids == ("node-low",)
    assert actions.auto_start_ready_nodes is True
    assert strategy.select_ready_nodes({
        "nodes": [
            {"node_id": "node-low", "status": "ready", "metadata": {"risk_level": "low"}},
            {"node_id": "node-high", "status": "ready", "metadata": {"risk_level": "high"}},
        ],
    }) == ("node-low",)
    assert strategy.risk_policy(risk_level="high").action == "require_approval"
    assert strategy.can_mutate_graph(actor="leader", phase="running") is True


def test_manual_graph_strategy_uses_user_graph_without_leader_planning():
    strategy = strategy_for_mode(MODE_MANUAL_GRAPH)

    patch = strategy.initialize_graph(
        mission_id="mission-1",
        title="手动任务图",
        objective="按用户图执行",
        members=MEMBERS,
        graph_payload={
            "nodes": [
                {"id": "node-a", "title": "A", "status": "ready", "x": 10, "y": 20},
                {"id": "node-b", "title": "B", "status": "todo"},
            ],
            "edges": [{"source": "node-a", "target": "node-b"}],
        },
    )

    assert patch.mission_status == "ready"
    assert patch.start_leader is False
    assert patch.auto_start_ready_nodes is False
    assert [node.node_id for node in patch.nodes] == ["node-a", "node-b"]
    assert patch.edges[0].from_node_id == "node-a"
    assert strategy.can_mutate_graph(actor="leader", phase="planning") is False
    assert strategy.can_mutate_graph(actor="user", phase="draft") is True


def test_manual_graph_rejects_edges_that_reference_missing_nodes():
    strategy = strategy_for_mode(MODE_MANUAL_GRAPH)

    with pytest.raises(ValueError, match="unknown node"):
        strategy.initialize_graph(
            mission_id="mission-1",
            title="手动任务图",
            objective="",
            graph_payload={
                "nodes": [{"id": "node-a", "title": "A"}],
                "edges": [{"source": "node-a", "target": "missing"}],
            },
        )


def test_session_db_initializes_team_mission_through_mode_strategy(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    graph = db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="规划审批后执行",
        mode=MODE_SUPERVISED_MISSION,
        members=MEMBERS,
    )

    assert graph["mission"]["mode"] == MODE_SUPERVISED_MISSION
    assert graph["mission"]["status"] == "planning"
    assert graph["mission"]["metadata"]["mode_strategy"] == MODE_SUPERVISED_MISSION
    assert graph["mission"]["metadata"]["requires_whole_graph_approval"] is True
    assert [node["kind"] for node in graph["nodes"]] == ["root"]
    assert graph["nodes"][0]["runtime_scope_key"] == "team:mission-1:leader"


def test_session_db_stores_dovie_member_runtime_profiles_for_team_mission(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    graph = db.initialize_team_mission_from_strategy(
        mission_id="mission-dovie",
        team_id="team-1",
        title="监督执行",
        objective="规划审批后执行",
        mode=MODE_SUPERVISED_MISSION,
        members=DOVIE_MEMBERS,
    )

    members = graph["mission"]["metadata"]["members"]
    assert members[0]["member_id"] == "member-leader"
    assert members[0]["profile_id"] == "profile-leader"
    assert members[0]["profile_version_id"] == "version-leader"
    assert members[0]["hermes_home_path"] == "/tmp/profile-leader-runtime"
    assert graph["nodes"][0]["assignee_profile_id"] == "profile-leader"
    assert graph["nodes"][0]["metadata"]["assignee_member_id"] == "member-leader"
