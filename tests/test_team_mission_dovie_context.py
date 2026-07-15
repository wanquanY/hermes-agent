from __future__ import annotations

import json
from pathlib import Path

from tests.team_mission_gateway_test_support import team_mission_gateway


def _workspace_payload(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(workspace)}


def _workspace_kwargs(tmp_path: Path) -> dict[str, str]:
    workspace = _workspace_payload(tmp_path)
    return {"workspace_id": workspace["workspace_id"], "workspace_path": workspace["workspace_path"]}


def _leader_product_context() -> dict:
    return {
        "cloud_query": {
            "query_id": "query-leader-1",
            "root_query_id": "root-query-1",
            "agent_run_id": "agent-run-leader-1",
            "query_context_token": "query-token-1",
            "query_source": "dovie_web",
        },
        "sourceAgentProfileId": "profile-leader",
        "sourceSessionId": "team-session-1",
        "sourceRunId": "run-leader-1",
        "sourceTurnId": "turn-leader-1",
        "sourceClientMessageId": "client-message-1",
    }


def _member(tmp_path: Path) -> dict:
    return {
        "member_id": "member-builder",
        "profile_id": "profile-builder",
        "profile_version_id": "version-builder",
        "role": "builder",
        "runtime_scope_key": "profile:profile-builder:version:version-builder",
        "dovie_profile": {
            "id": "profile-builder",
            "agentProfileVersionId": "version-builder",
            "runtimeScopeKey": "profile:profile-builder:version:version-builder",
            "hermesHomePath": str(tmp_path / "builder-home"),
        },
    }


def _session_context_from_submit(submitted: dict) -> dict:
    return json.loads(json.dumps(submitted["dovie_product_context"], ensure_ascii=False))


def test_member_submit_inherits_leader_cloud_query_and_sets_executing_profile(monkeypatch, tmp_path: Path):
    from hermes_agent.storage.cli_session_store import open_cli_session_store
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    member = _member(tmp_path)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="成员会话",
        objective="检查",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"conversation_session_id": "team-session-1"},
        members=[member],
    )
    submitted: dict = {}

    def fake_proxy_run_submit(params):
        submitted.update(params)
        return {"ok": True}

    monkeypatch.setattr(team_mission, "_proxy_run_submit_via_worker", fake_proxy_run_submit)

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-1",
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "workspace": _workspace_payload(tmp_path),
            "text": "@Builder 帮我检查",
            "target_member_id": "member-builder",
            "members": [member],
            "dovie_product_context": _leader_product_context(),
        },
    )

    assert "error" not in response
    session_context = _session_context_from_submit(submitted)
    assert session_context["cloud_query"] == _leader_product_context()["cloud_query"]
    assert session_context["root_agent_profile_id"] == "profile-leader"
    assert session_context["executing_agent_profile_id"] == "profile-builder"
    assert session_context["agent_role"] == "team_member"
    assert session_context["team_mission"]["kind"] == "member_chat"


def test_node_start_inherits_mission_cloud_query_and_sets_executing_profile(monkeypatch, tmp_path: Path):
    from hermes_agent.storage.cli_session_store import open_cli_session_store
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    member = _member(tmp_path)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="自主执行",
        objective="自动执行节点",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        leader_session_id="team-session-1",
        metadata={
            "conversation_session_id": "team-session-1",
            "dovie_product_context": _leader_product_context(),
        },
        members=[member],
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="完成交付",
        status="ready",
        assignee_profile_id="profile-builder",
        assignee_profile_version_id="version-builder",
        output_contract={"format": "deliverable"},
    )
    submitted: dict = {}

    def fake_run_submit(rid, params):
        submitted.update(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-worker",
                "conversation_session_id": params["conversation_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.node.start"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "run_id": "run-worker",
            "turn_id": "turn-worker",
        },
    )

    assert "error" not in response
    session_context = _session_context_from_submit(submitted)
    assert session_context["cloud_query"] == _leader_product_context()["cloud_query"]
    assert session_context["root_agent_profile_id"] == "profile-leader"
    assert session_context["executing_agent_profile_id"] == "profile-builder"
    assert session_context["agent_role"] == "team_member"
    assert session_context["team_mission"]["kind"] == "mission_node"


def test_camelcase_cloudquery_is_normalized_to_snake_case():
    """Desktop serializes snake_case cloud_query; the camelCase shim in
    _team_dovie_product_context must normalize legacy/alternate payloads."""
    from hermes_team_mission.gateway.common import _team_dovie_product_context

    leader_context = _leader_product_context()
    camel = dict(leader_context)
    camel["cloudQuery"] = camel.pop("cloud_query")

    normalized = _team_dovie_product_context(
        {"dovie_product_context": camel},
        team_mission={"kind": "member_chat"},
        executing_agent_profile_id="profile-builder",
    )

    assert normalized["cloud_query"] == leader_context["cloud_query"]
