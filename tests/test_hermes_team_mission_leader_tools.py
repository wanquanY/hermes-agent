import importlib
import json
from types import SimpleNamespace

from tests.team_mission_gateway_test_support import team_mission_gateway


def _seed_team(db, tmp_path):
    db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader",
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "profiles" / "leader"),
        default_toolsets=["file", "terminal"],
        current_version_id="version-leader",
        current_version_number=1,
    )
    db.upsert_agent_team(
        team_id="team-1",
        name="Team",
        lead_agent_profile_id="profile-leader",
        default_mode="supervised_mission",
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning"],
    )


def test_start_task_does_not_inherit_non_planning_conversation_mode(monkeypatch, tmp_path):
    import tools.team_mission_leader_tools  # noqa: F401
    from channels import session_context
    from hermes_state import SessionDB
    from tools.registry import registry
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server._methods["team_mission.create"](
        1,
        {
            "mission_id": "conversation-1",
            "team_id": "team-1",
            "title": "团队会话",
            "objective": "讨论入口",
            "mode": "discussion",
            "conversation_only": True,
            "workspace": {"workspace_id": "workspace-1", "workspace_path": str(workspace)},
            "metadata": {"conversationTeamSessionId": "team-session-1"},
        },
    )
    submitted = {}

    def fake_run_submit(rid, params):
        submitted.update(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "streaming",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-planning",
                "conversation_session_id": params["conversation_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    context_tokens = session_context.set_session_vars(
        dovie_product_context=json.dumps({
            "team_mission": {
                "kind": "leader_conversation",
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
                "team_id": "team-1",
                "workspace_id": "workspace-1",
                "workspace_path": str(workspace),
                "mode": "discussion",
            },
        }),
    )
    try:
        result = json.loads(
            registry.dispatch(
                "team_mission_start_task",
                {
                    "task_id": "task-1",
                    "title": "简单测试任务",
                    "objective": "创建一个简单测试任务并先规划",
                    "execution_mode": "autonomous_mission",
                },
                parent_agent=SimpleNamespace(_session_db=db, _hermes_active_run_id="run-leader-conversation"),
            )
        )
    finally:
        session_context.clear_session_vars(context_tokens)
        for var in session_context._VAR_MAP.values():
            var.set(session_context._UNSET)

    assert result["success"] is True
    assert result["task_status"] == "planning"
    assert submitted["enabled_toolsets"] == ["team_mission_read", "team_mission_planning", "clarify", "file_readonly"]
    assert submitted["dovie_product_context"]["team_mission"]["node_phase"] == "planning"
    graph = db.get_team_mission_graph(result["mission_id"])
    assert graph["mission"]["mode"] == "supervised_mission"
    assert graph["mission"]["status"] == "planning"
    assert graph["mission"]["metadata"]["conversation_mode"] == "discussion"
    assert graph["mission"]["metadata"]["task_execution_mode"] == "supervised_mission"
    assert [node["kind"] for node in graph["nodes"]] == ["root"]
