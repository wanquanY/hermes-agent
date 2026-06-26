import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from tests.team_mission_gateway_test_support import team_mission_gateway, team_mission_history_gateway


class _MemoryTransport:
    def __init__(self):
        self.frames = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


def _workspace_payload(tmp_path: Path, workspace_id: str = "workspace-1") -> dict:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": workspace_id, "workspace_path": str(workspace)}


def _workspace_kwargs(tmp_path: Path, workspace_id: str = "workspace-1") -> dict:
    workspace = _workspace_payload(tmp_path, workspace_id=workspace_id)
    return {"workspace_id": workspace["workspace_id"], "workspace_path": workspace["workspace_path"]}


def _team_task_brief(label: str = "deliverable") -> dict:
    return {
        "background": f"用户请求团队协作完成 {label}，需要成员基于任务图上下文执行。",
        "execution": [
            f"梳理 {label} 的输入和限制。",
            f"完成分配给本节点的 {label} 工作。",
        ],
        "goal": f"交付可被 Leader 验收和汇总的 {label}。",
        "acceptance_criteria": [
            "结果直接覆盖本节点目标。",
            "说明关键假设、验证方式和未解决问题。",
        ],
    }


def test_team_mission_conversation_runtime_session_ids_gateway_is_lightweight(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        team_id="team-1",
        workspace_id="workspace-1",
        stable_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="测试任务",
        objective="测试任务",
        mode="supervised_mission",
        status="running",
        leader_session_id="team-session-1",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker-1",
        runtime_scope_key="team:mission-1:node:worker",
        role="worker",
    )

    response = server._methods["team_mission.conversation.runtime_session_ids"](
        1,
        {"team_id": "team-1", "workspace_id": "workspace-1", "mission_id": "mission-1"},
    )

    assert "error" not in response
    assert response["result"]["session_ids"] == ["team-session-1", "worker-session-1", "runtime-worker-1"]
    assert response["result"]["runtime_session_ids"] == response["result"]["session_ids"]
    assert "conversations" not in response["result"]


def _seed_registry_team(db, tmp_path: Path) -> None:
    db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader",
        description="Plans and verifies team work.",
        category="product",
        tags=["planning", "quality"],
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "leader" / "version-leader"),
        default_toolsets=["file", "terminal"],
        recommended_skills=["planning"],
        current_version_id="version-leader",
        current_version_number=1,
    )
    db.upsert_agent_profile(
        profile_id="profile-builder",
        slug="builder",
        name="Builder",
        description="Builds the implementation.",
        category="engineering",
        tags=["engineering"],
        hermes_profile_name="builder",
        hermes_home_path=str(tmp_path / "builder" / "version-builder"),
        default_toolsets=["file", "terminal"],
        recommended_skills=["implementation"],
        current_version_id="version-builder",
        current_version_number=1,
    )
    db.upsert_agent_team(
        team_id="team-1",
        name="Registry Team",
        description="Team from Hermes registry.",
        lead_agent_profile_id="profile-leader",
        default_mode="supervised_mission",
        policy={"planApproval": "always"},
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning"],
    )
    db.upsert_agent_team_member(
        member_id="member-builder",
        team_id="team-1",
        agent_profile_id="profile-builder",
        agent_profile_version_id="version-builder",
        role="builder",
        capability_tags=["engineering"],
    )


def test_team_capability_gateway_get_refresh_and_bind(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    get_response = server._methods["team_capability.snapshot.get"](
        1,
        {"team_id": "team-1"},
    )
    refresh_response = server._methods["team_capability.snapshot.refresh"](
        2,
        {"team_id": "team-1"},
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="工程任务",
        objective="实现并验收",
        mode="supervised_mission",
    )
    bind_response = server._methods["team_capability.snapshot.bind"](
        3,
        {
            "mission_id": "mission-1",
            "conversation_id": "conversation-1",
            "snapshot_id": refresh_response["result"]["snapshot"]["snapshot_id"],
        },
    )
    profile_response = server._methods["team_mission.team_profile.get"](
        4,
        {"mission_id": "mission-1"},
    )

    assert get_response["result"]["snapshot"]["team_id"] == "team-1"
    assert refresh_response["result"]["snapshot"]["version"] == 2
    assert bind_response["result"]["binding"]["snapshot_id"] == refresh_response["result"]["snapshot"]["snapshot_id"]
    assert profile_response["result"]["snapshot"]["snapshot_id"] == refresh_response["result"]["snapshot"]["snapshot_id"]


def test_team_capability_gateway_builds_snapshot_from_registry_team_id(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setitem(server._methods, "run.submit", lambda rid, params: {
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "status": "running",
            "run_id": params["run_id"],
            "turn_id": params["turn_id"],
            "session_id": "runtime-leader",
            "stored_session_id": params["stored_session_id"],
            "runtime_scope_key": params["runtime_scope_key"],
        },
    })

    get_response = server._methods["team_capability.snapshot.get"](1, {"team_id": "team-1"})
    refresh_response = server._methods["team_capability.snapshot.refresh"](2, {"team_id": "team-1"})
    create_response = server._methods["team_mission.create"](
        3,
        {
            "mission_id": "mission-registry",
            "team_id": "team-1",
            "title": "Registry-owned task",
            "objective": "Verify registry-owned team mission creation.",
            "workspace": {"workspace_id": "workspace-1", "workspace_path": str(tmp_path)},
            "mode": "supervised_mission",
        },
    )

    snapshot = get_response["result"]["snapshot"]
    assert snapshot["team_id"] == "team-1"
    assert [item["member_id"] for item in snapshot["member_profiles"]] == ["member-leader", "member-builder"]
    assert refresh_response["result"]["snapshot"]["team_id"] == "team-1"
    graph = create_response["result"]["graph"]
    root = next(node for node in graph["nodes"] if node["kind"] == "root")
    assert root["assignee_profile_id"] == "profile-leader"
    assert root["assignee_profile_version_id"] == "version-leader"
    assert graph["mission"]["metadata"]["team_capability_snapshot"]["snapshot_id"].startswith("team-capability:team-1:")


def test_team_mission_leader_node_toolsets_are_surface_scoped():
    import importlib

    team_mission = team_mission_gateway()

    # Planning toolset is intentionally write-graph + ask-user + read-only-workspace.
    # See _start_toolsets: clarify and file_readonly belong in the same exact set.
    assert team_mission._start_toolsets(
        {},
        {"mode": "supervised_mission"},
        {"kind": "root", "metadata": {"role": "leader", "phase": "planning"}},
    ) == ["team_mission_planning", "clarify", "file_readonly"]
    assert team_mission._start_toolsets(
        {},
        {"mode": "supervised_mission"},
        {"kind": "root", "metadata": {"role": "leader", "phase": "verifying"}},
    ) == ["team_mission_read"]


def test_team_mission_worker_toolsets_follow_current_member_profile(monkeypatch):
    import importlib

    team_mission = team_mission_gateway()
    monkeypatch.setattr(team_mission, "_load_enabled_toolsets", lambda: ["hermes-cli"], raising=False)
    params = {}
    mission = {
        "metadata": {
            "members": [
                {
                    "member_id": "qa-member",
                    "profile_id": "profile-qa",
                    "profile_version_id": "version-current",
                    "runtime_scope_key": "profile:profile-qa:version:version-current",
                    "default_toolsets": ["file", "terminal", "skills"],
                }
            ]
        }
    }
    node = {
        "kind": "worker",
        "assignee_profile_id": "profile-qa",
        "assignee_profile_version_id": "version-old",
        "metadata": {"role": "member"},
    }
    profile_params = team_mission._node_profile_params(params, mission, node)

    assert profile_params["agent_profile_version_id"] == "version-current"
    assert team_mission._start_toolsets(
        params,
        {"mode": "supervised_mission"},
        node,
        profile_params=profile_params,
    ) == ["hermes-cli", "clarify"]


def test_team_mission_node_profile_params_accept_dovie_member_fields():
    import importlib

    team_mission = team_mission_gateway()
    params = {}
    mission = {
        "metadata": {
            "members": [
                {
                    "memberId": "member-leader",
                    "agentProfileId": "profile-leader",
                    "agentProfileVersionId": "version-leader",
                    "role": "lead",
                    "dovieProfile": {
                        "id": "profile-leader",
                        "agentProfileVersionId": "version-leader",
                        "runtimeScopeKey": "profile:profile-leader:version:version-leader",
                        "hermesHomePath": "/tmp/profile-leader-runtime",
                    },
                }
            ]
        }
    }
    node = {
        "kind": "root",
        "assignee_profile_id": "profile-leader",
        "metadata": {"role": "leader"},
    }

    profile_params = team_mission._node_profile_params(params, mission, node)

    assert profile_params["agent_profile_id"] == "profile-leader"
    assert profile_params["agent_profile_version_id"] == "version-leader"
    assert profile_params["runtime_scope_key"] == "profile:profile-leader:version:version-leader"
    assert profile_params["dovie_profile"]["hermesHomePath"] == "/tmp/profile-leader-runtime"


def test_team_profile_get_resolves_conversation_registry_without_active_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="监督执行",
    )

    response = server._methods["team_mission.team_profile.get"](
        1,
        {
            "conversation_id": "conversation-1",
            "team_id": "team-1",
        },
    )

    result = response["result"]
    assert result["mission_id"] == ""
    assert result["team_id"] == "team-1"
    assert result["source"] == "team_registry"
    assert result["snapshot"]["team_id"] == "team-1"
    assert [member["display_name"] for member in result["snapshot"]["member_profiles"]] == ["Leader", "Builder"]


def test_leader_team_profile_tool_resolves_conversation_registry(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB

    team_mission = team_mission_gateway()
    leader_tools = importlib.import_module("hermes_team_mission.tools.leader")
    profile_tools = importlib.import_module("hermes_team_mission.tools.profile")
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="监督执行",
    )
    monkeypatch.setattr(
        profile_tools,
        "_session_context",
        lambda: {
            "team_mission": {
                "kind": "leader_conversation",
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
                "team_id": "team-1",
            }
        },
    )

    payload = json.loads(leader_tools._handle_team_profile({}, SimpleNamespace(_session_db=db)))

    assert payload["success"] is True
    assert payload["mission_id"] == ""
    assert payload["snapshot"]["team_id"] == "team-1"
    assert [member["display_name"] for member in payload["snapshot"]["member_profiles"]] == ["Leader", "Builder"]
    assert "evidence_refs" not in payload["snapshot"]
    assert all("evidence_refs" not in member for member in payload["snapshot"]["member_profiles"])


def test_team_mission_gateway_methods_create_graph_and_replay_events(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    submitted = {}

    def fake_run_submit(rid, params):
        submitted.update(params)
        db.upsert_run(
            run_id=params["run_id"],
            session_id=params["stored_session_id"],
            runtime_scope_key=params["runtime_scope_key"],
            status="running",
        )
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-leader",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    create_response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "team_id": "team-1",
            "title": "监督执行",
            "objective": "规划审批后执行",
            "mode": "supervised_mission",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert create_response["result"]["mission_id"] == "mission-1"
    graph = create_response["result"]["graph"]
    assert graph["mission"]["status"] == "planning"
    assert graph["mission"]["metadata"]["team_capability_snapshot"]["snapshot_id"]
    assert graph["mission"]["metadata"]["requires_whole_graph_approval"] is True
    assert [node["kind"] for node in graph["nodes"]] == ["root"]
    assert graph["run_bindings"][0]["role"] == "leader"
    assert submitted["text"] != "规划审批后执行"
    assert "team_mission_node_create" in submitted["text"]
    assert "team_mission_plan_complete" in submitted["text"]
    assert submitted["enabled_toolsets"] == ["team_mission_planning", "clarify", "file_readonly"]
    assert "delegation" in submitted["disabled_toolsets"]
    assert submitted["toolset_scope"] == "exact"
    assert submitted["dovie_product_context"]["team_mission"]["node_role"] == "leader"
    assert submitted["dovie_product_context"]["team_mission"]["node_phase"] == "planning"
    assert submitted["dovie_product_context"]["team_mission"]["tool_policy"]["blocked_tools"] == ["delegate_task"]
    assert submitted["dovie_product_context"]["team_mission"]["tool_policy"]["toolset_scope"] == "exact"

    graph_response = server._methods["team_mission.graph"](2, {"missionId": "mission-1"})
    assert graph_response["result"]["graph"]["mission"]["mission_id"] == "mission-1"

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id=graph["run_bindings"][0]["run_id"],
        event={
            "type": "message.delta",
            "seq": 2,
            "payload": {"delta": "规划中"},
        },
    )

    events_response = server._methods["team_mission.events"](3, {"mission_id": "mission-1"})
    events = events_response["result"]["events"]
    message_events = [
        event
        for event in events
        if (
            event["type"] == "team_mission.runtime.event"
            and event["payload"]["source_event_type"] == "message.delta"
        )
    ]
    assert events_response["result"]["last_event_seq"] >= message_events[-1]["seq"]
    assert message_events[-1]["payload"]["mission_id"] == "mission-1"
    assert message_events[-1]["payload"]["source_event"]["payload"]["delta"] == "规划中"


def test_team_mission_create_rejects_autonomous_override_for_supervised_team(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-autonomous",
            "team_id": "team-1",
            "title": "不应自主执行",
            "objective": "监督团队不能由请求载荷改成自主执行",
            "mode": "autonomous_mission",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert response["error"]["code"] == 4094
    assert "team policy requires supervised_mission" in response["error"]["message"]
    assert db.get_team_mission_graph("mission-autonomous") == {}


def test_team_mission_graph_returns_conversation_graph_when_conversation_id_is_present(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="连续任务",
        active_mission_id="mission-2",
    )
    for mission_id, title, created_at in (
        ("mission-1", "第一轮", 1.0),
        ("mission-2", "第二轮", 2.0),
    ):
        db.upsert_team_mission(
            mission_id=mission_id,
            conversation_id="conversation-1",
            team_id="team-1",
            title=title,
            objective=title,
            mode="supervised_mission",
            leader_session_id="team-session-1",
            created_at=created_at,
            updated_at=created_at,
            metadata={"task_id": f"task-{mission_id}"},
        )
        db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id="root",
            kind="root",
            title=f"{title}根节点",
            status="completed",
            metadata={"task_id": f"task-{mission_id}"},
        )

    response = server._methods["team_mission.graph"](
        1,
        {"mission_id": "mission-2", "conversation_id": "conversation-1"},
    )

    result = response["result"]
    graph = result["graph"]
    assert result["mission_id"] == "mission-2"
    assert result["conversation_id"] == "conversation-1"
    assert graph["mission"]["mission_id"] == "mission-2"
    assert [frame["missionId"] for frame in graph["task_frames"]] == [
        "mission-1",
        "mission-2",
    ]
    assert [node["node_id"] for node in graph["nodes"]] == ["mission-1:root", "mission-2:root"]


def test_team_mission_graph_rejects_mission_from_another_conversation(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        active_mission_id="mission-1",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="第一轮",
        objective="第一轮",
        leader_session_id="team-session-1",
    )
    db.upsert_team_mission(
        mission_id="mission-other",
        conversation_id="conversation-other",
        team_id="team-1",
        title="其他会话",
        objective="其他会话",
        leader_session_id="team-session-other",
    )

    response = server._methods["team_mission.graph"](
        1,
        {"mission_id": "mission-other", "conversation_id": "conversation-1"},
    )

    assert response["error"]["code"] == 4040
    assert response["error"]["message"] == "team mission not found in conversation"


def test_team_mission_create_conversation_only_does_not_create_or_start_graph(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    submitted = []

    def fake_run_submit(rid, params):
        submitted.append(params)
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "running"}}

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "team_id": "team-1",
            "title": "监督执行",
            "objective": "你好啊",
            "mode": "supervised_mission",
            "conversation_only": True,
            "workspace": _workspace_payload(tmp_path),
            "metadata": {"stableTeamSessionId": "team-session-1"},
        },
    )

    graph = response["result"]["graph"]
    assert graph["mission"] == {}
    assert response["result"]["conversation_id"] == "mission-1"
    assert graph["conversation"]["conversation_id"] == "mission-1"
    assert graph["conversation"]["stable_session_id"] == "team-session-1"
    assert graph["conversation"]["active_mission_id"] == ""
    assert graph["nodes"] == []
    assert graph["run_bindings"] == []
    assert submitted == []
    assert db.get_session("team-session-1") is not None
    assert db.get_messages("team-session-1") == []
    assert db.get_team_mission_graph("mission-1") == {}

    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:profile-leader")
    submit_response = server._methods["team_mission.message.submit"](
        2,
        {
            "conversation_id": "mission-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "text": "你好啊",
        },
    )

    assert submit_response["result"]["conversation_session_id"] == "team-session-1"
    assert submit_response["result"]["conversation_id"] == "mission-1"
    assert submitted[0]["stored_session_id"] == "team-session-1"
    assert submitted[0]["agent_profile_id"] == "profile-leader"
    assert submitted[0]["persist_user_message"] == "你好啊"
    assert db.get_team_mission_graph("mission-1") == {}

    resolve_response = server._methods["team_mission.conversation.resolve"](
        3,
        {"conversation_id": "mission-1"},
    )

    assert resolve_response["result"]["conversation"]["conversation_id"] == "mission-1"
    assert resolve_response["result"]["mission"] == {}


def test_team_mission_message_submit_derives_conversation_title_from_first_user_message(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    workspace = _workspace_payload(tmp_path)
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team Mission",
        objective="占位会话",
        workspace_id=workspace["workspace_id"],
        workspace_path=workspace["workspace_path"],
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "title": "DoXie 不应该拥有这个标题",
            "text": "内部增强后的提示",
            "draft_text": "你是谁？ 我是谁？",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "workspace": workspace,
        },
    )

    assert "error" not in response
    conversation = db.get_team_mission_conversation("conversation-1")
    assert conversation["title"] == "你是谁？ 我是谁？"
    assert conversation["display_title_source"] == "first_user_message"
    assert submitted["persist_user_message"] == "你是谁？ 我是谁？"


def test_team_mission_message_submit_rejects_session_id_as_conversation_identity(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "text": "你好",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert response["error"]["code"] == 4006
    assert "mission_id or conversation_id required" in response["error"]["message"]
    assert db.resolve_team_mission_conversation("team-session-1") == {}


def test_team_mission_message_submit_conversation_only_does_not_bind_previous_active_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="老团队会话",
        active_mission_id="mission-old",
        workspace_id="workspace-1",
        workspace_path=str(tmp_path),
    )
    db.upsert_team_mission(
        mission_id="mission-old",
        conversation_id="conversation-1",
        team_id="team-1",
        title="已完成旧任务",
        objective="历史任务",
        mode="supervised_mission",
        status="completed",
        **_workspace_kwargs(tmp_path),
        metadata={"conversation_id": "conversation-1", "stableTeamSessionId": "team-session-1"},
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "text": "继续这个团队会话，启动一个新的测试任务",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert "error" not in response
    assert response["result"]["mission_id"] == ""
    assert response["result"]["graph"]["mission"] == {}
    assert "mission_id" not in submitted
    team_context = submitted["dovie_product_context"]["team_mission"]
    assert team_context["conversation_id"] == "conversation-1"
    assert team_context["conversation_session_id"] == "team-session-1"
    assert "mission_id" not in team_context
    assert "mission-old" in submitted["text"]

    submitted.clear()
    response = server._methods["team_mission.message.submit"](
        2,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "text": "解释一下之前团队任务的结果，不要启动团队任务",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert "error" not in response
    assert response["result"]["mission_id"] == ""
    assert "mission_id" not in submitted
    assert submitted["enabled_toolsets"] == []
    assert "mission-old" in submitted["text"]
    team_context = submitted["dovie_product_context"]["team_mission"]
    assert team_context["conversation_id"] == "conversation-1"
    assert team_context["conversation_session_id"] == "team-session-1"
    assert "mission_id" not in team_context


def test_team_conversation_detail_returns_registry_team_members(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    db.upsert_agent_profile(
        profile_id="profile-reviewer",
        slug="reviewer",
        name="Reviewer",
        description="Reviews team work.",
        category="quality",
        tags=["review"],
        hermes_profile_name="reviewer",
        hermes_home_path=str(tmp_path / "reviewer" / "version-reviewer"),
        default_toolsets=["file"],
        recommended_skills=["review"],
        current_version_id="version-reviewer",
        current_version_number=1,
    )
    db.upsert_agent_team_member(
        member_id="member-reviewer",
        team_id="team-1",
        agent_profile_id="profile-reviewer",
        agent_profile_version_id="version-reviewer",
        role="reviewer",
        capability_tags=["review"],
    )
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    workspace = _workspace_payload(tmp_path)
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        objective="协作",
        workspace_id=workspace["workspace_id"],
        workspace_path=workspace["workspace_path"],
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    db.append_message("team-session-1", "user", "请开始团队协作")

    resolve_response = server._methods["team_mission.conversation.resolve"](
        1,
        {"identifier": "conversation-1"},
    )
    render_response = server._methods["team_mission.conversation.render"](
        2,
        {"identifier": "conversation-1"},
    )

    assert "error" not in resolve_response
    resolved_team = resolve_response["result"]["team"]
    assert resolved_team["projection"] == "detail"
    assert [member["member_id"] for member in resolved_team["members"]] == [
        "member-leader",
        "member-builder",
        "member-reviewer",
    ]
    assert len(resolved_team["display_members"]) == 2
    assert resolve_response["result"]["graph"]["team"]["members"] == resolved_team["members"]
    assert "error" not in render_response
    assert render_response["result"]["team"]["members"] == resolved_team["members"]


def test_team_mission_node_create_requires_existing_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.node.create"](
        1,
        {
            "mission_id": "missing-mission",
            "node": {
                "id": "node-1",
                "title": "Should not create an orphan node",
            },
        },
    )

    assert response["error"]["code"] == 4040
    assert response["error"]["message"] == "team mission not found"
    assert db.get_team_mission_node("missing-mission", "node-1") == {}


def test_team_conversation_resolve_returns_empty_when_conversation_is_missing(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.conversation.resolve"](
        1,
        {"identifier": "missing-conversation"},
    )

    assert "error" not in response
    assert response["result"] == {"conversation": {}, "mission": {}, "graph": {}}


def test_team_mission_create_records_user_task_in_stable_team_session(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    def fake_run_submit(rid, params):
        db.upsert_run(
            run_id=params["run_id"],
            session_id=params["stored_session_id"],
            runtime_scope_key=params["runtime_scope_key"],
            status="running",
        )
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-leader",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "team_id": "team-1",
            "title": "监督执行",
            "objective": "继续做第二个任务",
            "mode": "supervised_mission",
            "workspace": _workspace_payload(tmp_path),
            "metadata": {"stableTeamSessionId": "team-session-1"},
        },
    )

    assert response["result"]["graph"]["mission"]["workspace_id"] == "workspace-1"
    assert response["result"]["graph"]["mission"]["workspace_path"] == _workspace_payload(tmp_path)["workspace_path"]
    assert response["result"]["graph"]["mission"]["metadata"]["conversation_session_id"] == "team-session-1"

    messages = db.get_messages("team-session-1")
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "继续做第二个任务"),
    ]
    assert messages[0]["metadata"]["team_mission"]["kind"] == "user_task"


def test_team_mission_message_submit_routes_to_leader_without_starting_node(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"conversation_session_id": "team-session-1", "active_task_id": "task-1"},
        members=[{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:profile-leader")

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-1",
            "text": "你好，上一轮进度怎么样？",
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
            "attachments": [
                {
                    "id": "upload-1",
                    "name": "requirements.pdf",
                    "mimeType": "application/pdf",
                    "size": 1200,
                    "path": "/tmp/requirements.pdf",
                    "kind": "file",
                },
                {
                    "id": "upload-2",
                    "name": "screen.png",
                    "mimeType": "image/png",
                    "size": 8,
                    "path": str(image_path),
                    "kind": "image",
                }
            ],
        },
    )

    assert response["result"]["conversation_session_id"] == "team-session-1"
    assert submitted["stored_session_id"] == "team-session-1"
    assert submitted["persist_user_message"] == "你好，上一轮进度怎么样？"
    assert submitted["enabled_toolsets"] == [
        "team_mission_conversation_leader",
        "clarify",
        "vision",
        "file",
        "terminal",
        "todo",
    ]
    assert "delegation" in submitted["disabled_toolsets"]
    assert submitted["toolset_scope"] == "exact"
    assert "team_mission_start_task" in submitted["text"]
    assert submitted["dovie_product_context"]["team_mission"]["kind"] == "leader_conversation"
    assert submitted["dovie_product_context"]["team_mission"]["tool_policy"]["disabled_toolsets"] == ["delegation"]
    assert submitted["dovie_product_context"]["team_mission"]["tool_policy"]["toolset_scope"] == "exact"
    assert submitted["attachments"][1]["path"] == str(image_path)
    assert submitted["attachments"][1]["kind"] == "image"
    assert submitted["attachments"][0]["path"] == "/tmp/requirements.pdf"
    conversation = db.get_team_mission_conversation("mission-1")
    assert conversation["title"] == "你好，上一轮进度怎么样？"
    assert conversation["display_title_source"] == "first_user_message"
    memory_items = db.list_team_mission_memory_items(
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        kinds=["artifact"],
        statuses=["committed"],
    )
    assert len(memory_items) == 1
    assert memory_items[0]["task_id"] == "task-1"
    assert memory_items[0]["source_run_ids"] == [submitted["run_id"]]
    assert memory_items[0]["artifact_refs"][0]["path"] == "/tmp/requirements.pdf"
    assert memory_items[0]["artifact_refs"][0]["kind"] == "file"
    assert len(db.get_team_mission_graph("mission-1")["nodes"]) == 1


def test_team_mission_message_submit_direct_reply_disables_tools_and_reasoning(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "team:conversation-1:leader-conversation")

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-conversation-1",
            "text": "我测试功能，你写一篇不少于800字的科幻作文，不要启动团队任务，你自己完成",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert response["result"]["conversation_session_id"] == "team-session-conversation-1"
    assert submitted["enabled_toolsets"] == []
    assert submitted["toolset_scope"] == "exact"
    assert submitted["reasoning_config"] == {"enabled": False}
    assert "team_mission_start_task" not in submitted["text"]
    assert "Answer directly" in submitted["text"]
    assert submitted["persist_user_message"] == "我测试功能，你写一篇不少于800字的科幻作文，不要启动团队任务，你自己完成"
    assert db.get_team_mission_graph("mission-1") == {}


def test_team_mission_message_submit_explicit_start_task_overrides_negated_direct_reply(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "team:conversation-1:leader-conversation")

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-conversation-1",
            "team_id": "team-1",
            "text": "请必须启动团队任务，不要直接自己完成。让团队创建 hello_team_stream.txt 并验证文件存在，然后由汇总节点给出最终结论。",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert response["result"]["conversation_session_id"] == "team-session-conversation-1"
    assert submitted["enabled_toolsets"] == [
        "team_mission_conversation_leader",
        "clarify",
        "vision",
        "file",
        "terminal",
        "todo",
    ]
    assert submitted["toolset_scope"] == "exact"
    assert "team_mission_start_task" in submitted["text"]
    assert "The user explicitly asked you not to start or launch a team task" not in submitted["text"]
    assert "Do not call tools, do not create tasks" not in submitted["text"]
    assert "reasoning_config" not in submitted
    assert submitted["persist_user_message"].startswith("请必须启动团队任务")
    assert submitted["dovie_product_context"]["team_mission"]["team_id"] == "team-1"


def test_team_mission_message_submit_registers_worker_runtime_session_shell(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    control_home = tmp_path / "control"
    profile_home = tmp_path / "profile"
    control_home.mkdir()
    profile_home.mkdir()
    control_db = SessionDB(control_home / "state.db")
    runtime_db = SessionDB(profile_home / "state.db")
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "team:conversation-1:leader-conversation")
    monkeypatch.setattr(team_mission, "_get_db", lambda: control_db)
    monkeypatch.setattr(server, "_get_db", lambda: runtime_db)

    control_db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        objective="继续沟通",
    )
    assert control_db.get_session("team-session-1") is not None
    assert runtime_db.get_session("team-session-1") is None

    submitted = {}

    def fake_run_submit(rid, params):
        assert runtime_db.get_session("team-session-1") is not None
        submitted.update(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "streaming",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "text": "哈哈哈",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert "error" not in response
    assert response["result"]["conversation_session_id"] == "team-session-1"
    assert submitted["stored_session_id"] == "team-session-1"
    assert runtime_db.get_session("team-session-1")["source"] == "team_mission"


def test_team_mission_message_submit_forwards_leader_profile_context(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    leader_member = {
        "member_id": "leader",
        "profile_id": "profile-leader",
        "profile_version_id": "version-leader",
        "role": "leader",
        "runtime_scope_key": "profile:profile-leader:version:version-leader",
        "dovie_profile": {
            "id": "profile-leader",
            "agentProfileVersionId": "version-leader",
            "runtimeScopeKey": "profile:profile-leader:version:version-leader",
            "hermesHomePath": str(tmp_path / "leader-home"),
        },
    }
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"conversation_session_id": "team-session-1"},
        members=[leader_member],
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:profile-leader:version:version-leader")

    server._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-1",
            "text": "我叫什么名字？",
            "members": [leader_member],
            "leader_runtime_scope_key": "team:mission-1:leader-conversation",
        },
    )

    assert submitted["agent_profile_id"] == "profile-leader"
    assert submitted["agent_profile_version_id"] == "version-leader"
    assert submitted["runtime_scope_key"] == "team:mission-1:leader-conversation"
    assert submitted["dovie_profile"]["hermesHomePath"] == str(tmp_path / "leader-home")
    assert submitted["dovie_profile"]["agentProfileVersionId"] == "version-leader"
    assert submitted["dovie_profile"]["runtimeScopeKey"] == "profile:profile-leader:version:version-leader"


def test_team_mission_message_submit_keeps_team_scope_out_of_profile_owner_check(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:agent-default")

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "text": "团队会话继续聊",
            "agentProfileId": "agent-default",
            "runtimeScopeKey": "profile:agent-default",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "leader_runtime_scope_key": "team:conversation-1:leader-conversation",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert "error" not in response
    assert submitted["agent_profile_id"] == "agent-default"
    assert submitted["runtimeScopeKey"] == "profile:agent-default"
    assert submitted["runtime_scope_key"] == "team:conversation-1:leader-conversation"


def test_team_mission_message_submit_allows_control_plane_outer_call_to_owner_runtime_scope(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.delenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", raising=False)
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "team-conversation-193ea1df-2fa7-49f6-a4e8-80de90f9e5e0",
            "conversation_session_id": "team-session-team-conversation-193ea1df-2fa7-49f6-a4e8-80de90f9e5e0",
            "team_id": "team-1",
            "text": "你好",
            "agentProfileId": "agent-default",
            "profileRuntimeScopeKey": "profile:agent-default",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert "error" not in response
    assert response["result"]["conversation_id"] == "team-conversation-193ea1df-2fa7-49f6-a4e8-80de90f9e5e0"
    assert response["result"]["conversation_session_id"] == "team-session-team-conversation-193ea1df-2fa7-49f6-a4e8-80de90f9e5e0"
    assert submitted["runtime_scope_key"] == "team:team-conversation-193ea1df-2fa7-49f6-a4e8-80de90f9e5e0:leader-conversation"
    assert submitted["stored_session_id"] == "team-session-team-conversation-193ea1df-2fa7-49f6-a4e8-80de90f9e5e0"
    assert submitted["agent_profile_id"] == "agent-default"


def test_team_mission_conversation_ensure_keeps_team_scope_out_of_profile_owner_check(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:agent-default")

    response = server._methods["team_mission.conversation.ensure"](
        1,
        {
            "conversation_id": "conversation-1",
            "stable_team_session_id": "team-session-1",
            "team_id": "team-1",
            "title": "团队会话",
            "agentProfileId": "agent-default",
            "runtimeScopeKey": "profile:agent-default",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "leader_runtime_scope_key": "team:conversation-1:leader-conversation",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert "error" not in response
    assert response["result"]["conversation_id"] == "conversation-1"
    assert response["result"]["conversation_session_id"] == "team-session-1"


def test_team_mission_conversation_ensure_uses_conversation_scope_for_bound_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="功能测试",
        objective="验证历史团队任务会话",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-conversation-1",
        conversation_id="conversation-1",
        metadata={"conversation_id": "conversation-1", "conversation_session_id": "team-session-conversation-1"},
        members=[{
            "member_id": "leader",
            "profile_id": "profile-leader",
            "profile_version_id": "version-leader",
            "runtime_scope_key": "profile:profile-leader:version:version-leader",
            "role": "leader",
        }],
    )
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "team:conversation-1:leader-conversation")

    response = server._methods["team_mission.conversation.ensure"](
        1,
        {
            "mission_id": "mission-1",
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-conversation-1",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "profile_runtime_scope_key": "profile:profile-leader:version:version-leader",
            "members": [{
                "member_id": "leader",
                "profile_id": "profile-leader",
                "profile_version_id": "version-leader",
                "runtime_scope_key": "profile:profile-leader:version:version-leader",
                "role": "leader",
            }],
        },
    )

    assert "error" not in response
    assert response["result"]["conversation_id"] == "conversation-1"
    assert response["result"]["conversation_session_id"] == "team-session-conversation-1"


def test_team_mission_message_submit_rejects_wrong_owner_runtime_scope(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"conversation_session_id": "team-session-1"},
        members=[{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
    )
    submitted = {}

    def fake_run_submit(rid, params):
        submitted.update(params)
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}}

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:other")

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-1",
            "text": "这条不能写到错误 profile",
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
        },
    )

    assert response["error"]["code"] == 4094
    assert "team:mission-1:leader-conversation" in response["error"]["message"]
    assert submitted == {}


def test_team_mission_message_submit_rejects_profile_scope_as_team_execution_scope(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "team:conversation-1:leader-conversation")
    submitted = {}

    def fake_run_submit(rid, params):
        submitted.update(params)
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}}

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "text": "团队会话继续聊",
            "agentProfileId": "agent-default",
            "runtimeScopeKey": "profile:agent-default",
            "profileRuntimeScopeKey": "profile:agent-default",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert response["error"]["code"] == 4094
    assert "must use the team conversation runtime scope as runtimeScopeKey" in response["error"]["message"]
    assert submitted == {}


def test_team_mission_message_submit_merges_requested_leader_conversation_toolsets(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"conversation_session_id": "team-session-1"},
        members=[{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:profile-leader")

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-1",
            "text": "先看看当前目录再决定任务怎么规划",
            "enabled_toolsets": ["terminal", "skills"],
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
        },
    )

    assert response["result"]["conversation_session_id"] == "team-session-1"
    assert submitted["enabled_toolsets"] == [
        "team_mission_conversation_leader",
        "clarify",
        "vision",
        "file",
        "terminal",
        "todo",
        "skills",
    ]
    assert submitted["toolset_scope"] == "exact"
    assert "delegation" in submitted["disabled_toolsets"]
    assert submitted["agent_context_mode"] == "team_leader"
    assert "Hermes" not in submitted["text"]
    assert "DoXie team conversation" in submitted["text"]
    assert "Keep the same persona, identity, tone, and memory as the underlying DoXie profile" in submitted["text"]
    assert "Never expose internal runtime" in submitted["text"]


def test_team_mission_member_node_start_keeps_delegation_available(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"conversation_session_id": "team-session-1"},
        members=[
            {"member_id": "leader", "profile_id": "profile-leader", "role": "leader"},
            {"member_id": "builder", "profile_id": "profile-builder", "role": "builder"},
        ],
    )
    server._methods["team_mission.node.create"](
        1,
        {
            "mission_id": "mission-1",
            "node": {
                "id": "node-builder",
                "kind": "worker",
                "title": "成员执行节点",
                "objective": "完成实际交付",
                "status": "ready",
                "assignee_profile_id": "profile-builder",
                "metadata": {"role": "member", "phase": "execution"},
            },
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
                "session_id": "runtime-builder",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.node.start"](
        2,
        {"mission_id": "mission-1", "node_id": "node-builder"},
    )

    assert response["result"]["node"]["node_id"] == "node-builder"
    assert submitted["agent_profile_id"] == "profile-builder"
    assert "delegation" not in submitted.get("disabled_toolsets", [])
    assert submitted["toolset_scope"] == "exact"
    assert "tool_policy" not in submitted["dovie_product_context"]["team_mission"]


def test_team_mission_message_submit_does_not_inject_other_conversation_memory(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-old",
        team_id="team-1",
        title="Old mission",
        objective="Build filescan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-old", "task_id": "task-old"},
    )
    db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-old",
        conversation_session_id="team-session-old",
        task_id="task-old",
        scope="mission_task",
        kind="summary",
        content="Old delivery: filescan.py was implemented and verified.",
        source_node_ids=["node-old"],
        source_run_ids=["run-old"],
        visibility="team",
    )
    db.initialize_team_mission_from_strategy(
        mission_id="mission-new",
        team_id="team-1",
        title="Fresh conversation",
        objective="你好",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-new",
        metadata={"conversation_session_id": "team-session-new"},
        members=[{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
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
                "session_id": "runtime-leader-conversation",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:profile-leader")

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-new",
            "text": "你好",
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
        },
    )

    assert response["result"]["conversation_session_id"] == "team-session-new"
    assert submitted["stored_session_id"] == "team-session-new"
    assert "Team Conversation Memory Pack" not in submitted["text"]
    assert "filescan.py" not in submitted["text"]
    memory_context = submitted["dovie_product_context"]["team_mission"]["memory"]
    assert memory_context["kind"] == "leader_conversation_memory_pack"
    assert memory_context["item_ids"] == []


def test_team_mission_conversation_ensure_creates_missing_stable_session(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id="team-session-legacy",
        metadata={"conversation_session_id": "team-session-legacy"},
        members=[{"member_id": "leader", "role": "leader"}],
    )
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "team:mission-1:leader-conversation")

    assert db.get_session("team-session-legacy")["source"] == "team_mission"

    response = server._methods["team_mission.conversation.ensure"](
        1,
        {
            "mission_id": "mission-1",
        },
    )

    assert response["result"]["conversation_session_id"] == "team-session-legacy"
    assert response["result"]["created"] is False
    assert db.get_session("team-session-legacy")["source"] == "team_mission"

    second = server._methods["team_mission.conversation.ensure"](
        2,
        {
            "mission_id": "mission-1",
        },
    )
    assert second["result"]["created"] is False


def test_team_mission_conversation_ensure_can_repair_session_without_graph(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.conversation.ensure"](
        1,
        {
            "mission_id": "mission-missing-in-hermes",
            "conversation_session_id": "team-session-from-dovie",
            "workspace": _workspace_payload(tmp_path),
        },
    )

    assert response["result"]["mission_id"] == "mission-missing-in-hermes"
    assert response["result"]["conversation_session_id"] == "team-session-from-dovie"
    assert response["result"]["created"] is True
    assert db.get_session("team-session-from-dovie")["source"] == "team_mission"


def test_archived_team_history_is_readable_but_team_writes_are_rejected(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    _seed_registry_team(db, tmp_path)
    workspace = _workspace_payload(tmp_path)
    db.ensure_team_mission_conversation(
        conversation_id="conversation-archived-team",
        stable_session_id="team-session-archived",
        team_id="team-1",
        title="历史团队会话",
        objective="历史内容",
        workspace_id=workspace["workspace_id"],
        workspace_path=workspace["workspace_path"],
    )
    db.create_session("team-session-archived", source="team_mission", transient=False)
    db.append_message("team-session-archived", "user", "历史消息仍可查看")
    db.archive_agent_team("team-1")

    resolve_response = server._methods["team_mission.conversation.resolve"](
        1,
        {"conversation_id": "conversation-archived-team"},
    )
    assert "error" not in resolve_response
    assert resolve_response["result"]["conversation"]["conversation_id"] == "conversation-archived-team"
    assert resolve_response["result"]["messages"][0]["content"] == "历史消息仍可查看"

    ensure_response = server._methods["team_mission.conversation.ensure"](
        2,
        {
            "conversation_id": "conversation-archived-team",
            "conversation_session_id": "team-session-archived",
            "team_id": "team-1",
            "workspace": workspace,
        },
    )
    create_response = server._methods["team_mission.create"](
        3,
        {
            "mission_id": "mission-archived-team",
            "team_id": "team-1",
            "title": "不应创建",
            "objective": "不应创建",
            "workspace": workspace,
        },
    )
    submit_response = server._methods["team_mission.message.submit"](
        4,
        {
            "conversation_id": "conversation-archived-team",
            "conversation_session_id": "team-session-archived",
            "team_id": "team-1",
            "text": "不能继续对话",
            "workspace": workspace,
        },
    )

    for response in (ensure_response, create_response, submit_response):
        assert response["error"]["code"] == 4023
        assert response["error"]["message"] == "team archived: team-1"


def test_team_mission_conversation_rename_gateway_updates_canonical_state(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="旧团队任务",
        objective="初始任务",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )

    response = server._methods["team_mission.conversation.rename"](
        1,
        {
            "conversation_id": "conversation-1",
            "title": "新团队任务",
        },
    )

    assert response["result"]["conversation_id"] == "conversation-1"
    assert response["result"]["conversation"]["title"] == "新团队任务"
    assert db.get_session("team-session-1")["source"] == "team_mission"
    assert db.get_session("team-session-1")["title"] is None
    assert db.get_team_mission_graph("mission-1")["mission"]["title"] == "旧团队任务"


def test_team_mission_conversation_delete_gateway_blocks_active_leader_run(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="监督执行",
        objective="初始任务",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_run(
        run_id="run-leader",
        session_id="team-session-1",
        runtime_scope_key="team:mission-1:leader-conversation",
        status="running",
    )

    response = server._methods["team_mission.conversation.delete"](
        1,
        {"conversation_id": "conversation-1"},
    )

    assert response["error"]["code"] == 4023
    assert response["error"]["message"] == "cannot delete a conversation with an active leader run"
    assert db.resolve_team_mission_conversation("conversation-1")["conversation"]["conversation_id"] == "conversation-1"


def test_team_mission_conversation_delete_gateway_removes_canonical_conversation(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services.artifacts import list_artifacts, record_artifacts_from_tool_complete
    from tui_gateway.services.persistence import gateway_store
    from tui_gateway.services.workspaces import bind_session_workspace, session_workspace_binding

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path / "hermes-home")
    gateway_store._DEFAULT_STORES.clear()
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="监督执行",
        objective="初始任务",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="completed",
    )
    db.create_session("worker-session-1", source="team_mission", transient=False)
    db.create_session("runtime-worker-1", source="team_mission", transient=False)
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker-1",
        runtime_scope_key="team:mission-1:node-worker",
        role="worker",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "worker-output.md"
    artifact.write_text("# worker output\n", encoding="utf-8")
    workspace_payload = {
        "id": "workspace-test",
        "name": "workspace",
        "path": str(workspace),
        "kind": "local",
    }
    bind_session_workspace(
        session_id="team-session-1",
        cwd=str(workspace),
        workspace=workspace_payload,
    )
    record_artifacts_from_tool_complete(
        session_id="worker-session-1",
        tool_call_id="tool-worker",
        name="write_file",
        args={"path": "worker-output.md"},
        result=json.dumps({"bytes_written": artifact.stat().st_size}),
        cwd=str(workspace),
        workspace=workspace_payload,
    )

    response = server._methods["team_mission.conversation.delete"](
        1,
        {"conversation_id": "conversation-1"},
    )

    assert response["result"]["deleted"] is True
    assert response["result"]["conversation_id"] == "conversation-1"
    assert response["result"]["stable_session_id"] == "team-session-1"
    assert response["result"]["run_session_ids"] == ["worker-session-1", "runtime-worker-1"]
    assert response["result"]["deleted_session_ids"] == [
        "team-session-1",
        "worker-session-1",
        "runtime-worker-1",
    ]
    assert response["result"]["deleted_artifact_links"] == 1
    assert response["result"]["deleted_artifacts"] == 1
    assert response["result"]["physical_files_deleted"] == 0
    assert response["result"]["deleted_workspace_binding_count"] == 2
    assert db.resolve_team_mission_conversation("conversation-1") == {}
    assert db.get_session("team-session-1") is None
    assert db.get_session("worker-session-1") is None
    assert db.get_session("runtime-worker-1") is None
    assert list_artifacts(session_id="worker-session-1") == []
    assert list_artifacts(workspace_id="workspace-test") == []
    assert session_workspace_binding("team-session-1") is None
    assert session_workspace_binding("worker-session-1") is None
    assert artifact.exists()


def test_team_mission_leader_start_task_tool_starts_planning_node(monkeypatch, tmp_path: Path):
    import importlib
    import json

    import tools.team_mission_leader_tools  # noqa: F401
    import tools.team_mission_planning_tools  # noqa: F401
    from hermes_state import SessionDB
    from tools.registry import registry
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    graph = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "team_id": "team-1",
            "title": "监督执行",
            "objective": "初始任务",
            "mode": "supervised_mission",
            "conversation_only": True,
            "workspace": _workspace_payload(tmp_path),
            "metadata": {"stableTeamSessionId": "team-session-1"},
        },
    )["result"]["graph"]
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
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    from gateway import session_context

    context_tokens = session_context.set_session_vars(
        dovie_product_context=json.dumps({
                "team_mission": {
                    "kind": "leader_conversation",
                    "conversation_id": "mission-1",
                    "conversation_session_id": "team-session-1",
                    "team_id": "team-1",
                    "workspace_id": "workspace-1",
                    "workspace_path": str(tmp_path / "workspace"),
                }
        }),
    )
    try:
        agent = SimpleNamespace(_session_db=db, _hermes_active_run_id="run-leader-conversation")

        result = json.loads(
            registry.dispatch(
                "team_mission_start_task",
                {
                    "task_id": "task-2",
                    "title": "第二个任务",
                    "objective": "规划并执行第二个任务",
                },
                parent_agent=agent,
            )
        )
    finally:
        session_context.clear_session_vars(context_tokens)
        for var in session_context._VAR_MAP.values():
            var.set(session_context._UNSET)

    assert result["success"] is True
    assert result["intent"] == "start_team_task"
    assert result["submission_status"] == "accepted"
    assert result["task_status"] == "planning"
    assert result["completion_status"] == "pending"
    assert result["final_result_available"] is False
    assert result["await_final_deliverable"] is True
    assert result["hermes_control"]["kind"] == "team_mission_started"
    assert result["hermes_control"]["end_current_turn"] is True
    assert result["hermes_control"]["await_final_deliverable"] is True
    assert result["mission_id"] != "mission-1"
    assert result["conversation_id"] == "mission-1"
    assert result["node"]["node_id"] == f"team-mission:{result['mission_id']}:root"
    assert submitted["record_user_task_message"] is False
    assert submitted["agent_profile_id"] == "profile-leader"
    assert submitted["enabled_toolsets"] == ["team_mission_planning", "clarify", "file_readonly"]
    assert "delegation" in submitted["disabled_toolsets"]
    assert submitted["toolset_scope"] == "exact"
    assert submitted["dovie_product_context"]["team_mission"]["node_phase"] == "planning"
    assert "Mission objective: 规划并执行第二个任务" in submitted["text"]
    assert "Mission objective: 初始任务" not in submitted["text"]
    updated_graph = db.get_team_mission_graph(result["mission_id"])
    assert updated_graph["mission"]["title"] == "第二个任务"
    assert updated_graph["mission"]["objective"] == "规划并执行第二个任务"
    assert updated_graph["mission"]["status"] == "planning"
    assert updated_graph["mission"]["conversation_id"] == "mission-1"
    nodes = updated_graph["nodes"]
    assert len(nodes) == 1
    assert any(node["node_id"] == f"team-mission:{result['mission_id']}:root" for node in nodes)
    planning_agent = SimpleNamespace(_session_db=db, _hermes_active_run_id=submitted["run_id"])
    created_worker = json.loads(
        registry.dispatch(
            "team_mission_node_create",
            {
                "node_id": "worker-second-task",
                "title": "执行第二个任务",
                "objective": "完成第二个任务的执行交付",
                "task_brief": _team_task_brief("第二个任务交付物"),
            },
            parent_agent=planning_agent,
        )
    )
    assert created_worker.get("success") is True, created_worker
    persisted_worker = db.get_team_mission_node(result["mission_id"], "worker-second-task")
    assert created_worker["node_id"] == "worker-second-task"
    assert "node" not in created_worker
    assert persisted_worker["metadata"]["task_id"] == "task-2"
    assert persisted_worker["metadata"]["task_title"] == "第二个任务"
    assert persisted_worker["metadata"]["task_objective"] == "规划并执行第二个任务"
    completed = json.loads(
        registry.dispatch(
            "team_mission_plan_complete",
            {},
            parent_agent=planning_agent,
        )
    )
    assert completed["success"] is True
    assert completed["approval_requests"][0]["task_id"] == "task-2"
    assert "graph" not in completed
    completed_nodes = db.get_team_mission_graph(result["mission_id"])["nodes"]
    approval_node = next(node for node in completed_nodes if node["kind"] == "approval_gate")
    assert approval_node["metadata"]["task_id"] == "task-2"
    rejected = server._methods["team_mission.plan.reject"](
        2,
        {
            "mission_id": result["mission_id"],
            "task_id": "task-2",
            "rejected_by": "user",
            "reason": "用户拒绝当前计划",
        },
    )
    assert rejected["result"]["task_id"] == "task-2"
    rejected_graph = rejected["result"]["graph"]
    assert rejected_graph["mission"]["status"] == "draft"
    rejected_task_nodes = [
        node for node in rejected_graph["nodes"]
        if (node.get("metadata") or {}).get("task_id") == "task-2"
    ]
    assert rejected_task_nodes
    assert {node["status"] for node in rejected_task_nodes} == {"cancelled"}


def test_team_mission_plan_approve_uses_requested_mission_native_graph(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    try:
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            stable_session_id="team-session-1",
            team_id="team-1",
            active_mission_id="mission-current",
            title="团队会话",
        )
        db.upsert_team_mission(
            mission_id="mission-old",
            conversation_id="conversation-1",
            team_id="team-1",
            title="旧任务",
            objective="旧任务",
            mode="supervised_mission",
            status="waiting_approval",
            metadata={"conversation_id": "conversation-1"},
        )
        db.upsert_team_mission_node(
            mission_id="mission-old",
            node_id="team-mission:mission-old:approval-plan",
            kind="approval_gate",
            title="旧审批",
            status="waiting_approval",
            metadata={"task_id": "task-old"},
        )
        db.upsert_team_mission(
            mission_id="mission-current",
            conversation_id="conversation-1",
            team_id="team-1",
            title="当前任务",
            objective="当前任务",
            mode="supervised_mission",
            status="waiting_approval",
            metadata={"conversation_id": "conversation-1"},
        )
        db.upsert_team_mission_node(
            mission_id="mission-current",
            node_id="team-mission:mission-current:approval-plan",
            kind="approval_gate",
            title="当前审批",
            status="waiting_approval",
            metadata={"task_id": "task-current"},
        )

        response = server._methods["team_mission.plan.approve"](
            1,
            {
                "mission_id": "mission-current",
                "conversation_id": "conversation-1",
                "approved_by": "user",
            },
        )

        assert "error" not in response
        assert response["result"]["node"]["node_id"] == "team-mission:mission-current:approval-plan"
        assert response["result"]["node"]["status"] == "completed"
        assert response["result"]["node"]["metadata"]["approved_by"] == "user"
        assert db.get_team_mission_node(
            "mission-current",
            "team-mission:mission-current:approval-plan",
        )["status"] == "completed"
        old_node = db.get_team_mission_node("mission-old", "team-mission:mission-old:approval-plan")
        assert old_node["status"] == "waiting_approval"
        assert "approved_by" not in (old_node.get("metadata") or {})
        assert {
            node["node_id"]
            for node in response["result"]["graph"]["nodes"]
        } == {"team-mission:mission-current:approval-plan"}
    finally:
        db.close()


def test_team_mission_plan_approve_starts_ready_worker_with_runtime_projection(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        active_mission_id="mission-current",
        title="团队会话",
    )
    db.upsert_team_mission(
        mission_id="mission-current",
        conversation_id="conversation-1",
        team_id="team-1",
        title="当前任务",
        objective="当前任务",
        workspace_id="workspace-1",
        workspace_path=str(workspace_path),
        mode="supervised_mission",
        status="waiting_approval",
        leader_session_id="team-session-1",
        metadata={"conversation_id": "conversation-1", "stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-current",
        node_id="team-mission:mission-current:approval-plan",
        kind="approval_gate",
        title="当前审批",
        status="waiting_approval",
        metadata={"task_id": "task-current"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-current",
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="执行节点任务",
        status="ready",
        runtime_scope_key="profile:worker-a",
        output_contract={"format": "artifact"},
        metadata={"task_id": "task-current"},
    )
    db.upsert_team_mission_edge(
        mission_id="mission-current",
        from_node_id="team-mission:mission-current:approval-plan",
        to_node_id="node-worker",
        kind="depends_on",
        metadata={"approval_gate": True, "approval_scope": "whole_graph"},
    )

    def fake_run_submit(rid, params):
        for seq, event_type, payload in (
            (1, "message.start", {}),
            (2, "message.delta", {"delta": "worker-live", "text": "worker-live"}),
        ):
            run_control.record_event(
                {
                    "type": event_type,
                    "session_id": "runtime-worker",
                    "stored_session_id": params["stored_session_id"],
                    "run_id": params["run_id"],
                    "turn_id": params["turn_id"],
                    "runtime_scope_key": params["runtime_scope_key"],
                    "seq": seq,
                    "payload": payload,
                },
                db=db,
            )
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-worker",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    response = server._methods["team_mission.plan.approve"](
        1,
        {
            "mission_id": "mission-current",
            "conversation_id": "conversation-1",
            "task_id": "task-current",
            "approved_by": "user",
        },
    )

    assert "error" not in response
    scheduled = response["result"]["scheduled"]
    assert scheduled["started"][0]["node_id"] == "node-worker"
    worker = db.get_team_mission_node("mission-current", "node-worker")
    assert worker["status"] == "running"
    assert worker["run_id"]
    assert worker["runtime_stable_session_id"] == "team:mission-current:node:node-worker"
    assert worker["runtime_session_id"] == "runtime-worker"
    assert worker["runtime_scope_key"] == "profile:worker-a"

    events = db.list_team_mission_run_events("mission-current")
    delta_event = next(
        event
        for event in events
        if event["type"] == "team_mission.runtime.event"
        and event["payload"]["source_event_type"] == "message.delta"
        and event["payload"]["node_id"] == "node-worker"
    )
    assert delta_event["payload"]["kind"] == "node.output.delta"
    assert delta_event["payload"]["subject"]["runtime_stable_session_id"] == "team:mission-current:node:node-worker"
    assert delta_event["payload"]["text_stream"]["delta"] == "worker-live"
    status_events = [event for event in events if event["type"] == "team_mission.conversation.status"]
    assert status_events
    latest_status = status_events[-1]["payload"]["conversation"]
    assert latest_status["conversation_id"] == "conversation-1"
    assert latest_status["active_mission_id"] == "mission-current"
    assert latest_status["running"] is True
    assert latest_status["run_state"] == "running"


def test_team_mission_direct_root_task_activation_replaces_draft_objective(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    server._methods["team_mission.create"](
        1,
        {
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "title": "你好",
            "mode": "supervised_mission",
            "conversation_only": True,
            "workspace": _workspace_payload(tmp_path),
            "metadata": {"stableTeamSessionId": "team-session-1"},
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
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    started = server._methods["team_mission.create"](
        2,
        {
            "mission_id": "mission-filescan",
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "title": "创建文件扫描工具",
            "objective": "在当前工作目录下创建 filescan.py 并完成验证",
            "mode": "supervised_mission",
            "workspace": _workspace_payload(tmp_path),
            "task_id": "task-filescan",
            "record_user_task_message": False,
        },
    )

    assert started["result"]["mission_id"] == "mission-filescan"
    assert started["result"]["leader_start"]["node"]["metadata"]["task_id"] == "task-filescan"
    assert "Mission title: 创建文件扫描工具" in submitted["text"]
    assert "Mission objective: 在当前工作目录下创建 filescan.py 并完成验证" in submitted["text"]
    assert "Mission objective: 你好" not in submitted["text"]
    graph = db.get_team_mission_graph("mission-filescan")
    assert graph["mission"]["title"] == "创建文件扫描工具"
    assert graph["mission"]["objective"] == "在当前工作目录下创建 filescan.py 并完成验证"
    assert graph["mission"]["metadata"]["active_task_id"] == "task-filescan"
    resolved = db.resolve_team_mission_conversation("conversation-1")
    assert resolved["conversation"]["title"] == "你好"
    assert resolved["conversation"]["active_mission_id"] == "mission-filescan"


def test_team_mission_runtime_output_stays_inside_node_session(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    root_node_id = db.get_team_mission_graph("mission-1")["nodes"][0]["node_id"]
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="node-session-1",
        runtime_session_id="runtime-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-leader",
            "stored_session_id": "node-session-1",
            "run_id": "run-leader",
            "turn_id": "turn-leader",
            "runtime_scope_key": "team:mission-1:leader",
            "seq": 1,
            "payload": {"text": "Leader 已完成任务图规划", "status": "complete"},
        },
        db=db,
    )

    assert db.list_run_events("team-session-1") == []
    assert db.get_messages("team-session-1") == []

    node_events = db.list_run_events("node-session-1")
    assert "message.complete" in [event["type"] for event in node_events]
    team_events = db.list_team_mission_run_events("mission-1")
    message_complete_events = [
        event
        for event in team_events
        if (
            event["type"] == "team_mission.runtime.event"
            and event["payload"]["source_event_type"] == "message.complete"
        )
    ]
    assert len(message_complete_events) == 1
    assert message_complete_events[0]["run_id"] == "run-leader"
    assert message_complete_events[0]["payload"]["source_event"]["payload"]["text"] == "Leader 已完成任务图规划"


def test_team_mission_synthesis_output_is_mirrored_as_conversation_deliverable(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="过程节点",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesizer",
        title="汇总交付",
        status="running",
        metadata={},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-worker",
            "stored_session_id": "worker-session-1",
            "run_id": "run-worker",
            "turn_id": "turn-worker",
            "runtime_scope_key": "team:mission-1:node:node-worker",
            "seq": 1,
            "payload": {"text": "过程节点输出", "status": "complete"},
        },
        db=db,
    )
    run_control.record_event(
        {
            "type": "message.start",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 1,
            "payload": {},
        },
        db=db,
    )
    run_control.record_event(
        {
            "type": "message.delta",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 2,
            "payload": {"delta": "最终汇总", "text": "最终汇总"},
        },
        db=db,
    )
    mirrored_events = db.list_run_events("team-session-1")
    assert [event["type"] for event in mirrored_events] == ["message.start", "message.delta"]
    assert mirrored_events[1]["payload"]["team_mission_final_deliverable"] is True
    assert mirrored_events[1]["payload"]["team_mission_conversation_mirror"] is True
    assert mirrored_events[1]["payload"]["mode"] == "append"
    assert mirrored_events[1]["payload"]["delta"] == "最终汇总"
    assert "snapshot" not in mirrored_events[1]["payload"]
    assert db.get_messages("team-session-1") == []

    run_control.record_event(
        {
            "type": "message.delta",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 3,
            "payload": {"delta": "交付内容", "text": "最终汇总交付内容"},
        },
        db=db,
    )
    mirrored_events = db.list_run_events("team-session-1")
    assert [event["type"] for event in mirrored_events] == [
        "message.start",
        "message.delta",
        "message.delta",
    ]
    assert mirrored_events[2]["payload"]["mode"] == "append"
    assert mirrored_events[2]["payload"]["delta"] == "交付内容"
    assert mirrored_events[2]["payload"]["text"] == "交付内容"
    assert "snapshot" not in mirrored_events[2]["payload"]

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 4,
            "payload": {"text": "最终汇总交付内容", "status": "complete"},
        },
        db=db,
    )

    mirrored_events = db.list_run_events("team-session-1")
    assert [event["type"] for event in mirrored_events] == [
        "message.start",
        "message.delta",
        "message.delta",
        "message.complete",
    ]
    mirrored = mirrored_events[3]
    assert mirrored["run_id"] == "team-mission:mission-1:conversation:run-synthesis"
    assert mirrored["stored_session_id"] == "team-session-1"
    assert mirrored["payload"]["text"] == "最终汇总交付内容"
    assert mirrored["payload"]["source_run_id"] == "run-synthesis"
    assert mirrored["payload"]["source_session_id"] == "synthesis-session-1"
    assert mirrored["payload"]["node_id"] == "team-mission:mission-1:synthesis"
    assert mirrored["payload"]["team_mission_final_deliverable"] is True
    assert mirrored["payload"]["team_mission_conversation_mirror"] is True
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "最终汇总交付内容"
    assert messages[0]["metadata"]["team_mission"]["kind"] == "final_deliverable"
    assert db.get_team_mission_node("mission-1", "team-mission:mission-1:synthesis")["status"] == "completed"
    resolved = db.resolve_team_mission_conversation("mission-1")
    assert resolved["messages"][0]["text"] == "最终汇总交付内容"
    assert resolved["graph"]["last_message"]["text"] == "最终汇总交付内容"
    assert messages[0]["metadata"]["team_mission"]["source_run_id"] == "run-synthesis"


def test_team_mission_synthesis_mirror_streams_to_conversation_subscriber(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesizer",
        title="汇总交付",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )

    transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )
    try:
        run_control.record_event(
            {
                "type": "message.start",
                "session_id": "runtime-synthesis",
                "stored_session_id": "synthesis-session-1",
                "run_id": "run-synthesis",
                "turn_id": "turn-synthesis",
                "runtime_scope_key": "team:mission-1:synthesis",
                "seq": 1,
                "payload": {},
            },
            db=db,
        )
        run_control.record_event(
            {
                "type": "message.delta",
                "session_id": "runtime-synthesis",
                "stored_session_id": "synthesis-session-1",
                "run_id": "run-synthesis",
                "turn_id": "turn-synthesis",
                "runtime_scope_key": "team:mission-1:synthesis",
                "seq": 2,
                "payload": {"delta": "实时最终交付", "text": "实时最终交付"},
            },
            db=db,
        )
        run_control.record_event(
            {
                "type": "message.delta",
                "session_id": "runtime-synthesis",
                "stored_session_id": "synthesis-session-1",
                "run_id": "run-synthesis",
                "turn_id": "turn-synthesis",
                "runtime_scope_key": "team:mission-1:synthesis",
                "seq": 3,
                "payload": {"delta": "第二段", "text": "实时最终交付第二段"},
            },
            db=db,
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)

    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    assert [event["type"] for event in streamed] == ["message.start", "message.delta", "message.delta"]
    assert all(event["stored_session_id"] == "team-session-1" for event in streamed)
    assert all(int(event.get("seq") or 0) > 0 for event in streamed)
    assert streamed[1]["payload"]["mode"] == "append"
    assert streamed[1]["payload"]["delta"] == "实时最终交付"
    assert "snapshot" not in streamed[1]["payload"]
    assert streamed[2]["payload"]["mode"] == "append"
    assert streamed[2]["payload"]["delta"] == "第二段"
    assert "snapshot" not in streamed[2]["payload"]
    assert streamed[1]["payload"]["team_mission_final_deliverable"] is True
    assert streamed[1]["payload"]["team_mission_conversation_mirror"] is True


def test_team_mission_tools_use_control_plane_db_inside_profile_worker(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_team_mission.runtime.profile_scope import gateway_call
    from tools import team_mission_leader_tools, team_mission_planning_tools
    from tui_gateway import server

    team_mission = team_mission_gateway()
    control_home = tmp_path / "control-home"
    profile_home = tmp_path / "profile-home"
    control_home.mkdir()
    profile_home.mkdir()
    profile_db = SessionDB(profile_home / "state.db")
    parent_agent = SimpleNamespace(_session_db=profile_db)
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))

    assert Path(team_mission_leader_tools._get_db(parent_agent).db_path) == control_home / "state.db"
    assert Path(team_mission_planning_tools._get_db(parent_agent).db_path) == control_home / "state.db"

    def read_current_db_path(rid, _params):
        db = team_mission._get_db()
        return {"jsonrpc": "2.0", "id": rid, "result": {"db_path": str(db.db_path)}}

    monkeypatch.setitem(server._methods, "test.team_mission.db_path", read_current_db_path)
    response = gateway_call("test.team_mission.db_path", {})
    assert response["result"]["db_path"] == str(control_home / "state.db")


def test_team_mission_node_start_prebinds_run_before_fast_synthesis_events(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="监督执行",
        objective="规划审批后执行",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        status="running",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-synthesis-delivery",
        kind="synthesis",
        title="汇总交付",
        status="ready",
        runtime_scope_key="team:mission-1:synthesis",
    )
    transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )

    def fake_run_submit(rid, params):
        for seq, event_type, payload in (
            (1, "message.start", {}),
            (2, "message.delta", {"delta": "实时", "text": "实时"}),
            (3, "message.complete", {"text": "实时最终交付", "status": "complete"}),
        ):
            run_control.record_event(
                {
                    "type": event_type,
                    "session_id": "runtime-synthesis",
                    "stored_session_id": params["stored_session_id"],
                    "run_id": params["run_id"],
                    "turn_id": params["turn_id"],
                    "runtime_scope_key": params["runtime_scope_key"],
                    "seq": seq,
                    "payload": payload,
                },
                db=db,
            )
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-synthesis",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    try:
        started = server._methods["team_mission.node.start"](
            1,
            {
                "mission_id": "mission-1",
                "node_id": "node-synthesis-delivery",
                "run_id": "run-synthesis",
                "turn_id": "turn-synthesis",
                "stored_session_id": "synthesis-session-1",
            },
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)

    assert "error" not in started
    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    assert [event["type"] for event in streamed] == ["message.start", "message.delta", "message.complete"]
    assert all(event["stored_session_id"] == "team-session-1" for event in streamed)
    assert streamed[2]["payload"]["text"] == "实时最终交付"
    assert streamed[2]["payload"]["team_mission_final_deliverable"] is True
    assert streamed[2]["payload"]["team_mission_conversation_mirror"] is True
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["content"] == "实时最终交付"


def test_team_mission_synthesis_failed_complete_with_text_still_mirrors_deliverable(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesizer",
        title="汇总交付",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 1,
            "payload": {
                "text": "最终汇总交付内容",
                "status": "failed",
                "message": "nonfatal tool cleanup failed",
            },
        },
        db=db,
    )

    mirrored_events = db.list_run_events("team-session-1")
    assert [event["type"] for event in mirrored_events] == ["message.complete"]
    mirrored = mirrored_events[0]
    assert mirrored["payload"]["status"] == "complete"
    assert mirrored["payload"]["text"] == "最终汇总交付内容"
    assert mirrored["payload"]["team_mission_final_deliverable"] is True
    assert mirrored["payload"]["team_mission_terminal_status_recovered"] == "failed"
    assert mirrored["payload"]["nonfatal_error"] == "nonfatal tool cleanup failed"
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "最终汇总交付内容"
    assert messages[0]["metadata"]["team_mission"]["kind"] == "final_deliverable"
    assert db.get_team_mission_node("mission-1", "team-mission:mission-1:synthesis")["status"] == "completed"
    resolved = db.resolve_team_mission_conversation("mission-1")
    assert resolved["messages"][0]["text"] == "最终汇总交付内容"
    assert resolved["graph"]["last_message"]["text"] == "最终汇总交付内容"


def test_final_deliverable_recovery_does_not_rewrite_legacy_snapshot_mirror_events(tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_team_mission.runtime.conversation_mirror import recover_final_deliverable_messages

    db = SessionDB(tmp_path / "state.db")
    db.create_session("team-session-1", source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesizer",
        title="汇总交付",
        status="completed",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )
    db.append_run_event(
        "synthesis-session-1",
        {
            "type": "message.delta",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 1,
            "payload": {"mode": "append", "delta": "最终汇总交付内容", "text": "最终汇总交付内容"},
        },
    )
    mirror_payload = {
        "run_id": "team-mission:mission-1:conversation:run-synthesis",
        "source_run_id": "run-synthesis",
        "source_session_id": "synthesis-session-1",
        "mission_id": "mission-1",
        "node_id": "team-mission:mission-1:synthesis",
        "team_mission_final_deliverable": True,
        "team_mission_conversation_mirror": True,
    }
    db.append_run_event(
        "team-session-1",
        {
            "type": "message.delta",
            "session_id": "runtime-synthesis",
            "stored_session_id": "team-session-1",
            "run_id": "team-mission:mission-1:conversation:run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team_mission:mission-1",
            "seq": 1,
            "payload": {
                **mirror_payload,
                "mode": "snapshot",
                "snapshot": "旧 snapshot 内容",
                "text": "旧 snapshot 内容",
            },
        },
    )
    db.append_run_event(
        "team-session-1",
        {
            "type": "message.delta",
            "session_id": "runtime-synthesis",
            "stored_session_id": "team-session-1",
            "run_id": "team-mission:mission-1:conversation:run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team_mission:mission-1",
            "seq": 2,
            "payload": {
                **mirror_payload,
                "mode": "snapshot",
                "snapshot": "重复旧 snapshot 内容",
                "text": "重复旧 snapshot 内容",
            },
        },
    )
    db.append_run_event(
        "team-session-1",
        {
            "type": "message.complete",
            "session_id": "runtime-synthesis",
            "stored_session_id": "team-session-1",
            "run_id": "team-mission:mission-1:conversation:run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team_mission:mission-1",
            "seq": 3,
            "payload": {
                **mirror_payload,
                "text": "旧完成内容",
                "status": "complete",
            },
        },
    )

    assert recover_final_deliverable_messages(db, {"stable_session_id": "team-session-1"}) == 1

    # Terminal-run retention prunes replay-redundant stream deltas from the
    # durable run_events log. Recovery must therefore use the source run or
    # complete payload, not rewrite/keep old legacy snapshot mirror deltas.
    deliverable = db.latest_team_mission_deliverable_for_run("run-synthesis")
    assert deliverable["source"] == "legacy_imported"
    assert deliverable["summary"] == "最终汇总交付内容"
    assert db.team_mission_run_has_deliverable("run-synthesis") is True
    delta_events = [
        event
        for event in db.list_run_events("team-session-1")
        if event["type"] == "message.delta"
    ]
    assert delta_events == []
    messages = db.get_messages("team-session-1")
    assert messages[-1]["content"] == "最终汇总交付内容"


def test_team_mission_cancel_marks_graph_and_cancels_active_runs(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="执行任务",
        objective="完成任务图",
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="交付结果",
        status="running",
        runtime_scope_key="team:mission-1:worker",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="node-session-1",
        runtime_scope_key="team:mission-1:worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="node-session-1",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:worker",
        role="worker",
    )
    canceled = []

    def fake_run_cancel(rid, params):
        canceled.append(params)
        db.upsert_run(
            run_id=params["run_id"],
            session_id=params["stored_session_id"],
            runtime_scope_key=params["runtime_scope_key"],
            status="cancelled",
        )
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "cancelled",
                "run_id": params["run_id"],
                "stored_session_id": params["stored_session_id"],
            },
        }

    monkeypatch.setitem(server._methods, "run.cancel", fake_run_cancel)

    response = server._methods["team_mission.cancel"](
        1,
        {
            "mission_id": "mission-1",
            "canceled_by": "user",
            "reason": "用户终止团队任务",
        },
    )

    assert response["result"]["mission_status"] == "cancelled"
    assert response["result"]["graph"]["mission"]["status"] == "cancelled"
    assert response["result"]["graph"]["nodes"][0]["status"] == "cancelled"
    assert response["result"]["cancel_errors"] == []
    assert response["result"]["canceled_runs"][0]["run_id"] == "run-worker"
    assert canceled == [
        {
            "run_id": "run-worker",
            "stored_session_id": "node-session-1",
            "runtime_session_id": "runtime-worker",
            "runtime_scope_key": "team:mission-1:worker",
            "reason": "用户终止团队任务",
        }
    ]


def test_team_mission_cancel_reaps_zombie_run_on_already_terminal_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    # Mission already terminal, but the scheduler started a member node run
    # afterwards that is still 'running' (the zombie the user observed).
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="执行任务",
        mode="supervised_mission",
        status="cancelled",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="verify-stats-report",
        kind="worker",
        title="验证",
        status="running",
    )
    db.upsert_run(
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="verify-stats-report",
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
        role="worker",
    )
    canceled = []

    def fake_run_cancel(rid, params):
        canceled.append(params["run_id"])
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "cancelled", "run_id": params["run_id"]}}

    monkeypatch.setitem(server._methods, "run.cancel", fake_run_cancel)

    response = server._methods["team_mission.cancel"](1, {"mission_id": "mission-1", "canceled_by": "user"})

    # The leftover run is surfaced for worker termination AND reaped terminal.
    assert "run-verify" in canceled
    assert db.get_run("run-verify")["status"] == "cancelled"


def test_gateway_emit_publishes_terminal_event_to_session_subscribers(monkeypatch):
    from tui_gateway import server
    from tui_gateway.services import run_control

    stable_session_id = "team-session-live"
    runtime_session_id = "runtime-live"
    subscriber_transport = _MemoryTransport()
    owner_transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id=stable_session_id,
        transport=subscriber_transport,
        active_only=True,
    )
    previous_session = None
    with server._sessions_lock:
        previous_session = server._sessions.get(runtime_session_id)
        server._sessions[runtime_session_id] = {
            "session_key": stable_session_id,
            "active_run_id": "run-live",
            "active_turn_id": "turn-live",
            "active_runtime_scope_key": "team:mission-live:leader-conversation",
            "transport": owner_transport,
        }
    token = server.bind_transport(owner_transport)
    try:
        server._emit(
            "message.complete",
            runtime_session_id,
            {
                "run_id": "run-live",
                "turn_id": "turn-live",
                "runtime_scope_key": "team:mission-live:leader-conversation",
                "text": "done",
                "status": "complete",
            },
        )
    finally:
        server.reset_transport(token)
        run_control.unsubscribe_session(subscription_id=subscription_id)
        with server._sessions_lock:
            if previous_session is None:
                server._sessions.pop(runtime_session_id, None)
            else:
                server._sessions[runtime_session_id] = previous_session

    assert any(
        frame.get("method") == "event"
        and (frame.get("params") or {}).get("type") == "message.complete"
        and (frame.get("params") or {}).get("stored_session_id") == stable_session_id
        and ((frame.get("params") or {}).get("payload") or {}).get("text") == "done"
        for frame in subscriber_transport.frames
    )


def test_event_bus_delivers_explicit_subscription_on_owner_transport(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-owner-live",
        transport=transport,
        active_only=False,
        db=db,
    )
    try:
        delivered = run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-owner-live",
                "stored_session_id": "team-session-owner-live",
                "run_id": "run-owner-live",
                "turn_id": "turn-owner-live",
                "runtime_scope_key": "team:conversation-owner:leader-conversation",
                "seq": 1,
                "payload": {"mode": "append", "delta": "实时", "text": "实时"},
            },
            owner_transport=transport,
            db=db,
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    assert any(item is transport for item in delivered)
    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    assert [event["type"] for event in streamed] == ["message.delta"]
    assert streamed[0]["stored_session_id"] == "team-session-owner-live"
    assert streamed[0]["payload"]["delta"] == "实时"


@pytest.mark.skip(reason="Phase 6: legacy bridge relay (_record_relayed_runtime_event) deleted; events flow through WorkerSupervisor → WorkerFrameRouter.on_event now")
def test_runtime_proxy_relay_uses_persisted_event_bus_for_owner_subscription(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control, runtime_proxy

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-relay-live",
        transport=transport,
        active_only=False,
        db=db,
    )
    try:
        delivered = runtime_proxy._persist_relayed_runtime_event(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.delta",
                    "session_id": "runtime-relay-live",
                    "stored_session_id": "team-session-relay-live",
                    "run_id": "run-relay-live",
                    "turn_id": "turn-relay-live",
                    "runtime_scope_key": "team:conversation-relay:leader-conversation",
                    "seq": 77,
                    "payload": {"mode": "append", "delta": "同步", "text": "同步"},
                },
            },
            owner_transport=transport,
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    assert any(item is transport for item in delivered)
    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    assert [event["type"] for event in streamed] == ["message.delta"]
    assert streamed[0]["stored_session_id"] == "team-session-relay-live"
    assert streamed[0]["seq"] == 1
    assert streamed[0]["payload"]["runtime_source_seq"] == 77
    assert streamed[0]["payload"]["delta"] == "同步"


@pytest.mark.skip(reason="Phase 6: legacy bridge relay deleted")
def test_runtime_proxy_drops_already_relayed_runtime_event(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import runtime_proxy

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "session_id": "runtime-relay-loop",
            "stored_session_id": "team-session-relay-loop",
            "run_id": "run-relay-loop",
            "turn_id": "turn-relay-loop",
            "runtime_scope_key": "team:conversation-relay:leader-conversation",
            "seq": 110,
            "runtime_source_seq": 103,
            "payload": {
                "mode": "append",
                "delta": "重复",
                "text": "重复",
                "runtime_source_seq": 103,
            },
        },
    }

    result = runtime_proxy._record_relayed_runtime_event(frame)

    assert result.delivered_transports == []
    assert result.allow_direct_relay is False
    assert db.list_run_events("team-session-relay-loop") == []
    db.close()


@pytest.mark.skip(reason="Phase 6: legacy bridge relay deleted")
def test_runtime_proxy_disables_direct_relay_for_duplicate_source_seq(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import runtime_proxy

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "session_id": "runtime-source-once",
            "stored_session_id": "team-session-source-once",
            "run_id": "run-source-once",
            "turn_id": "turn-source-once",
            "runtime_scope_key": "team:conversation-source:leader-conversation",
            "seq": 77,
            "payload": {"mode": "append", "delta": "一次", "text": "一次"},
        },
    }

    first = runtime_proxy._record_relayed_runtime_event(frame)
    second = runtime_proxy._record_relayed_runtime_event(frame)

    assert first.allow_direct_relay is True
    assert second.delivered_transports == []
    assert second.allow_direct_relay is False
    events = db.list_run_events("team-session-source-once")
    assert len(events) == 1
    assert events[0]["payload"]["runtime_source_seq"] == 77
    db.close()


@pytest.mark.skip(reason="Phase 6: legacy bridge relay deleted")
def test_runtime_proxy_does_not_duplicate_runtime_frame_already_in_state(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import runtime_proxy

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    db.append_run_event(
        "team-session-source-state",
        {
            "type": "message.delta",
            "session_id": "runtime-source-state",
            "stored_session_id": "team-session-source-state",
            "run_id": "run-source-state",
            "turn_id": "turn-source-state",
            "runtime_scope_key": "team:conversation-source:leader-conversation",
            "seq": 77,
            "payload": {"mode": "append", "delta": "原始", "text": "原始", "offset": 0},
        },
    )
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "session_id": "runtime-source-state",
            "stored_session_id": "team-session-source-state",
            "run_id": "run-source-state",
            "turn_id": "turn-source-state",
            "runtime_scope_key": "team:conversation-source:leader-conversation",
            "seq": 77,
            "payload": {"mode": "append", "delta": "原始", "text": "原始", "offset": 0},
        },
    }

    result = runtime_proxy._record_relayed_runtime_event(frame)

    assert result.delivered_transports == []
    assert result.allow_direct_relay is True
    events = db.list_run_events("team-session-source-state")
    assert len(events) == 1
    assert events[0]["seq"] == 77
    assert "runtime_source_seq" not in events[0]["payload"]
    db.close()


def test_gateway_emit_stream_subscription_persists_append_chunks_without_coalescing(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    stable_session_id = "team-session-stream"
    runtime_session_id = "runtime-stream"
    owner_transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id=stable_session_id,
        transport=owner_transport,
        active_only=False,
        db=db,
    )
    previous_session = None
    monkeypatch.setattr(server, "_get_db", lambda: db)
    with server._sessions_lock:
        previous_session = server._sessions.get(runtime_session_id)
        server._sessions[runtime_session_id] = {
            "session_key": stable_session_id,
            "active_run_id": "run-stream",
            "active_turn_id": "turn-stream",
            "active_runtime_scope_key": "team:mission-stream:leader-conversation",
            "transport": owner_transport,
        }
    token = server.bind_transport(owner_transport)
    try:
        server._emit(
            "message.delta",
            runtime_session_id,
            {
                "run_id": "run-stream",
                "turn_id": "turn-stream",
                "runtime_scope_key": "team:mission-stream:leader-conversation",
                "mode": "append",
                "delta": "你",
                "text": "你",
            },
        )
        server._emit(
            "message.delta",
            runtime_session_id,
            {
                "run_id": "run-stream",
                "turn_id": "turn-stream",
                "runtime_scope_key": "team:mission-stream:leader-conversation",
                "mode": "append",
                "delta": "好",
                "text": "好",
            },
        )
        time.sleep(0.7)
    finally:
        server.reset_transport(token)
        run_control.unsubscribe_session(subscription_id=subscription_id)
        with server._sessions_lock:
            if previous_session is None:
                server._sessions.pop(runtime_session_id, None)
            else:
                server._sessions[runtime_session_id] = previous_session

    delta_payloads = [
        (frame.get("params") or {}).get("payload") or {}
        for frame in owner_transport.frames
        if (frame.get("params") or {}).get("type") == "message.delta"
    ]
    assert [payload.get("delta") for payload in delta_payloads] == ["你", "好"]
    persisted_deltas = [
        event
        for event in db.list_run_events(stable_session_id)
        if event.get("type") == "message.delta"
    ]
    assert [event["seq"] for event in persisted_deltas] == [1, 2]
    assert [(event.get("payload") or {}).get("delta") for event in persisted_deltas] == ["你", "好"]


def test_run_control_subscription_poll_delivers_new_append_after_direct_delivery(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    transport = _MemoryTransport()
    stable_session_id = "stored-direct-stream"
    run_id = "run-direct-stream"
    turn_id = "turn-direct-stream"
    runtime_scope_key = "profile:agent-default"
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id=stable_session_id,
        transport=transport,
        active_only=False,
        runtime_scope_key=runtime_scope_key,
        db=db,
    )
    direct_event = {
        "type": "message.delta",
        "session_id": "runtime-direct-stream",
        "stored_session_id": stable_session_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        "seq": 1,
        "payload": {"mode": "append", "text": "你", "delta": "你", "offset": 0},
    }
    try:
        run_control.remember_transport_delivery(transport, direct_event)
        db.append_run_event(
            stable_session_id,
            {
                **direct_event,
                "seq": 2,
                "payload": {"mode": "append", "text": "好", "delta": "好", "offset": 1},
            },
        )
        time.sleep(0.7)
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    delta_payloads = [
        (frame.get("params") or {}).get("payload") or {}
        for frame in transport.frames
        if (frame.get("params") or {}).get("type") == "message.delta"
    ]
    assert [payload.get("delta") for payload in delta_payloads] == ["好"]
    assert [payload.get("text") for payload in delta_payloads] == ["好"]
    assert delta_payloads[0].get("offset") == 1


def test_run_control_subscription_poll_delivers_persisted_append_events_without_suffix_cropping(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    transport = _MemoryTransport()
    stable_session_id = "stored-coalesced-stream"
    run_id = "run-coalesced-stream"
    turn_id = "turn-coalesced-stream"
    runtime_scope_key = "profile:agent-default"
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id=stable_session_id,
        transport=transport,
        active_only=False,
        runtime_scope_key=runtime_scope_key,
        db=db,
    )

    def delta_payloads() -> list[dict]:
        return [
            (frame.get("params") or {}).get("payload") or {}
            for frame in transport.frames
            if (frame.get("params") or {}).get("type") == "message.delta"
        ]

    def wait_for_delta_count(expected: int) -> None:
        deadline = time.time() + 2
        while time.time() < deadline:
            if len(delta_payloads()) >= expected:
                return
            time.sleep(0.05)

    try:
        db.append_run_event(
            stable_session_id,
            {
                "type": "message.delta",
                "session_id": "runtime-coalesced-stream",
                "stored_session_id": stable_session_id,
                "run_id": run_id,
                "turn_id": turn_id,
                "runtime_scope_key": runtime_scope_key,
                "seq": 1,
                "payload": {"mode": "append", "text": "你", "delta": "你", "offset": 0},
            },
        )
        wait_for_delta_count(1)
        db.append_run_event(
            stable_session_id,
            {
                "type": "message.delta",
                "session_id": "runtime-coalesced-stream",
                "stored_session_id": stable_session_id,
                "run_id": run_id,
                "turn_id": turn_id,
                "runtime_scope_key": runtime_scope_key,
                "seq": 2,
                "payload": {"mode": "append", "text": "好", "delta": "好", "offset": 1},
            },
        )
        wait_for_delta_count(2)
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    payloads = delta_payloads()
    assert [payload.get("delta") for payload in payloads] == ["你", "好"]
    assert [payload.get("text") for payload in payloads] == ["你", "好"]
    assert payloads[-1].get("offset") == 1


def test_active_only_subscription_keeps_seen_live_run_for_terminal_polling(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    transport = _MemoryTransport()
    owner_transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-poll",
        transport=transport,
        active_only=True,
        db=db,
    )
    try:
        run_control.publish_recorded_event(
            {
                "type": "message.start",
                "session_id": "runtime-poll",
                "stored_session_id": "team-session-poll",
                "run_id": "run-poll",
                "turn_id": "turn-poll",
                "runtime_scope_key": "team:mission-poll:leader-conversation",
                "seq": 1,
            },
            owner_transport=owner_transport,
            db=db,
        )
        run_control.record_event(
            {
                "type": "message.complete",
                "session_id": "runtime-poll",
                "stored_session_id": "team-session-poll",
                "run_id": "run-poll",
                "turn_id": "turn-poll",
                "runtime_scope_key": "team:mission-poll:leader-conversation",
                "seq": 2,
                "payload": {"text": "done from log", "status": "complete"},
            },
            owner_transport=owner_transport,
            db=db,
        )

        deadline = time.time() + 2
        while time.time() < deadline:
            if any(
                (frame.get("params") or {}).get("type") == "message.complete"
                and ((frame.get("params") or {}).get("payload") or {}).get("text") == "done from log"
                for frame in transport.frames
            ):
                break
            time.sleep(0.05)

        assert any(
            (frame.get("params") or {}).get("type") == "message.complete"
            and ((frame.get("params") or {}).get("payload") or {}).get("text") == "done from log"
            for frame in transport.frames
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)


def test_team_mission_plan_approval_event_is_mirrored_to_stable_team_session(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    root_node_id = db.get_team_mission_graph("mission-1")["nodes"][0]["node_id"]
    db.upsert_run(
        run_id="run-leader",
        session_id="node-session-1",
        runtime_scope_key="team:mission-1:leader",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="node-session-1",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )
    server._methods["team_mission.node.create"](
        1,
        {
            "mission_id": "mission-1",
            "run_id": "run-leader",
            "node": {
                "id": "node-worker",
                "kind": "worker",
                "title": "执行节点",
                "objective": "交付结果",
                "status": "ready",
            },
        },
    )
    server._methods["team_mission.edge.create"](
        2,
        {
            "mission_id": "mission-1",
            "run_id": "run-leader",
            "edge": {"source": root_node_id, "target": "node-worker", "kind": "delegates"},
        },
    )

    completed = server._methods["team_mission.plan.complete"](
        3,
        {"mission_id": "mission-1", "run_id": "run-leader"},
    )

    assert completed["result"]["mission_status"] == "waiting_approval"
    mirrored_types = [event["type"] for event in db.list_run_events("team-session-1")]
    assert "mission.approval.requested" in mirrored_types


def test_team_mission_gateway_rejects_invalid_manual_graph(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "team_id": "team-1",
            "mode": "manual_graph",
            "workspace": _workspace_payload(tmp_path),
            "graph_payload": {
                "nodes": [{"id": "node-a", "title": "A"}],
                "edges": [{"source": "node-a", "target": "missing"}],
            },
        },
    )

    assert response["error"]["code"] == 4004
    assert "unknown node" in response["error"]["message"]


def test_team_mission_subscribe_replays_and_streams_mission_events(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
    )
    root_node_id = db.get_team_mission_graph("mission-1")["nodes"][0]["node_id"]
    db.upsert_run(
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={"type": "message.delta", "seq": 1, "payload": {"delta": "先前事件"}},
    )
    skipped_snapshot = db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={
            "type": "message.delta",
            "seq": 2,
            "payload": {"mode": "snapshot", "snapshot": "历史 snapshot", "text": "历史 snapshot"},
        },
    )
    assert skipped_snapshot["type"] == "message.delta"
    assert skipped_snapshot["payload"]["mode"] == "snapshot"
    assert len(db.list_team_mission_events("mission-1")) == 1

    transport = _MemoryTransport()
    token = server.bind_transport(transport)
    try:
        subscribed = server._methods["team_mission.subscribe"](
            1,
            {"mission_id": "mission-1"},
        )
    finally:
        server.reset_transport(token)

    subscription_id = subscribed["result"]["subscription_id"]
    assert subscription_id
    assert len(subscribed["result"]["events"]) == 1
    assert subscribed["result"]["events"][0]["type"] == "team_mission.runtime.event"
    assert subscribed["result"]["events"][0]["payload"]["source_event_type"] == "message.delta"
    assert subscribed["result"]["events"][0]["payload"]["source_event"]["payload"]["delta"] == "先前事件"

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={"type": "message.delta", "seq": 3, "payload": {"delta": "实时事件"}},
    )

    deadline = time.time() + 2
    while time.time() < deadline:
        if any(
            (((frame.get("params") or {}).get("payload") or {}).get("source_event") or {}).get("payload", {}).get("delta") == "实时事件"
            for frame in transport.frames
        ):
            break
        time.sleep(0.05)

    assert any(
        (((frame.get("params") or {}).get("payload") or {}).get("source_event") or {}).get("payload", {}).get("delta") == "实时事件"
        for frame in transport.frames
    )

    skipped_live_snapshot = db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={
            "type": "message.delta",
            "seq": 4,
            "payload": {"mode": "snapshot", "snapshot": "实时 snapshot", "text": "实时 snapshot"},
        },
    )
    assert skipped_live_snapshot["type"] == "message.delta"
    assert skipped_live_snapshot["payload"]["mode"] == "snapshot"
    assert len(db.list_team_mission_events("mission-1")) == 2
    assert not any(
        (((frame.get("params") or {}).get("payload") or {}).get("source_event") or {}).get("payload", {}).get("snapshot") == "实时 snapshot"
        for frame in transport.frames
    )

    removed = run_control.unsubscribe_session(subscription_id=subscription_id)
    assert removed == 1


def test_team_mission_subscribe_replay_is_byte_paged(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="监督执行",
        objective="验证 replay 分页",
        mode="supervised_mission",
    )
    root_node_id = db.get_team_mission_graph("mission-1")["nodes"][0]["node_id"]
    db.upsert_run(
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )
    for idx in range(3):
        db.append_team_mission_run_event(
            mission_id="mission-1",
            run_id="run-leader",
            event={
                "type": "message.delta",
                "seq": idx + 1,
                "payload": {"delta": f"{idx}-" + ("x" * 40000)},
            },
        )

    transport = _MemoryTransport()
    token = server.bind_transport(transport)
    try:
        subscribed = server._methods["team_mission.subscribe"](
            1,
            {"mission_id": "mission-1", "limit": 10, "byte_limit": 65536},
        )
    finally:
        server.reset_transport(token)

    result = subscribed["result"]
    subscription_id = result["subscription_id"]
    assert subscription_id
    assert result["has_more"] is True
    assert len(result["events"]) == 1
    assert result["last_event_seq"] == result["events"][0]["seq"]

    next_page = server._methods["team_mission.events"](
        2,
        {
            "mission_id": "mission-1",
            "after_seq": result["last_event_seq"],
            "limit": 10,
            "byte_limit": 65536,
        },
    )["result"]
    assert next_page["events"]
    assert next_page["events"][0]["seq"] > result["last_event_seq"]

    removed = run_control.unsubscribe_session(subscription_id=subscription_id)
    assert removed == 1


def test_team_mission_subscribe_streams_conversation_status_projection(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Create launch plan",
        workspace_id="workspace-1",
        workspace_path=_workspace_payload(tmp_path)["workspace_path"],
        mode="autonomous_mission",
        leader_session_id="team-session-1",
        metadata={"task_id": "task-1", "stableTeamSessionId": "team-session-1"},
    )
    mission = db.get_team_mission_graph("mission-1")["mission"]
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        mission=mission,
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        objective="Create launch plan",
        workspace_id="workspace-1",
        workspace_path=_workspace_payload(tmp_path)["workspace_path"],
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
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

    transport = _MemoryTransport()
    token = server.bind_transport(transport)
    try:
        subscribed = server._methods["team_mission.subscribe"](
            1,
            {"mission_id": "mission-1"},
        )
    finally:
        server.reset_transport(token)

    subscription_id = subscribed["result"]["subscription_id"]
    assert subscription_id

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={"type": "message.complete", "seq": 1, "payload": {"status": "complete"}},
    )

    deadline = time.time() + 2
    status_payload = {}
    while time.time() < deadline:
        for frame in transport.frames:
            params = frame.get("params") or {}
            if params.get("type") == "team_mission.conversation.status":
                status_payload = params.get("payload") or {}
                break
        if status_payload:
            break
        time.sleep(0.05)

    conversation = status_payload.get("conversation") or {}
    assert status_payload["conversation_id"] == "conversation-1"
    assert status_payload["stable_session_id"] == "team-session-1"
    assert status_payload["source_event_type"] == "message.complete"
    assert conversation["mission_status"] == "ready"
    assert conversation["running"] is False

    removed = run_control.unsubscribe_session(subscription_id=subscription_id)
    assert removed == 1


def test_team_mission_node_history_reads_runtime_from_hermes_store(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    team_mission_history = team_mission_history_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission_history, "_get_db", lambda: db)

    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        objective="Build",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        runtime_scope_key="team:mission-1:node:node-worker",
    )
    db.create_session("session-worker", source="team_mission")
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        runtime_session_id="runtime-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    db.append_message(
        "session-worker",
        role="assistant",
        content="final answer",
        platform_message_id="msg-worker",
        metadata={"run_id": "run-worker", "turn_id": "turn-worker"},
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 7,
            "turn_id": "turn-worker",
            "payload": {"text": "final answer"},
        },
    )

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "include_run_events": True,
            "run_events_limit": 20,
        },
    )

    assert "error" not in response
    result = response["result"]
    assert result["source"]["session_id"] == "session-worker"
    assert result["source"]["runtime_session_id"] == "runtime-worker"
    assert result["source"]["run_id"] == "run-worker"
    assert result["messages"][0]["message_id"] == "msg-worker"
    assert result["messages"][0]["text"] == "final answer"
    assert result["messages"][0]["metadata"]["run_id"] == "run-worker"
    assert result["run_events"][0]["type"] == "message.complete"
    assert result["run_events"][0]["seq"] == 7
    assert result["run_events"][0]["payload"]["mission_id"] == "mission-1"
    assert result["run_events"][0]["payload"]["node_id"] == "node-worker"


def test_team_mission_node_history_filters_stream_chunks(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    team_mission_history = team_mission_history_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission_history, "_get_db", lambda: db)

    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        objective="Build",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        runtime_scope_key="team:mission-1:node:node-worker",
    )
    db.create_session("session-worker", source="team_mission")
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        runtime_session_id="runtime-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    for seq, event_type, payload in (
        (1, "message.delta", {"mode": "append", "text": "raw", "delta": "raw", "offset": 0}),
        (2, "thinking.delta", {"text": "thinking"}),
        (3, "tool.complete", {"tool_name": "terminal", "status": "completed"}),
        (4, "message.complete", {"status": "completed", "text": "final"}),
    ):
        db.append_run_event(
            "session-worker",
            {
                "type": event_type,
                "session_id": "runtime-worker",
                "stored_session_id": "session-worker",
                "run_id": "run-worker",
                "turn_id": "turn-worker",
                "runtime_scope_key": "team:mission-1:node:node-worker",
                "seq": seq,
                "payload": payload,
            },
        )

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "include_run_events": True,
            "run_events_limit": 20,
            "exclude_run_event_types": ["message.delta", "thinking.delta"],
        },
    )

    assert "error" not in response
    assert [event["type"] for event in response["result"]["run_events"]] == [
        "tool.complete",
        "message.complete",
    ]


def test_team_mission_node_history_resolves_active_conversation_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    team_mission_history = team_mission_history_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission_history, "_get_db", lambda: db)

    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team",
        active_mission_id="mission-active",
    )
    db.upsert_team_mission(
        mission_id="mission-active",
        conversation_id="conversation-1",
        title="Mission",
        objective="Build",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-active",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        runtime_scope_key="team:mission-active:node:node-worker",
    )
    db.create_session("session-worker", source="team_mission")
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-active:node:node-worker",
        status="completed",
    )
    db.bind_team_mission_run(
        mission_id="mission-active",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-active:node:node-worker",
        role="worker",
    )
    db.append_message(
        "session-worker",
        role="assistant",
        content="active mission output",
        platform_message_id="msg-worker",
        metadata={"run_id": "run-worker"},
    )

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-stale",
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "node_id": "node-worker",
            "session_id": "session-worker",
        },
    )

    assert "error" not in response
    result = response["result"]
    assert result["mission_id"] == "mission-active"
    assert result["source"]["session_id"] == "session-worker"
    assert result["messages"][0]["text"] == "active mission output"


def test_team_mission_node_history_keeps_transcript_readable_when_graph_is_missing(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission_history = team_mission_history_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission_history, "_get_db", lambda: db)

    db.create_session("session-worker", source="team_mission")
    db.append_message(
        "session-worker",
        role="assistant",
        content="preserved failed node output",
        platform_message_id="msg-worker",
        metadata={"run_id": "run-worker"},
    )

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-missing",
            "node_id": "node-worker",
            "session_id": "session-worker",
            "include_run_events": True,
            "run_events_limit": 20,
        },
    )

    assert "error" not in response
    result = response["result"]
    assert result["mission_id"] == "mission-missing"
    assert result["source"]["session_id"] == "session-worker"
    assert result["source"]["node_id"] == "node-worker"
    assert result["messages"][0]["text"] == "preserved failed node output"


def test_team_mission_planner_methods_mutate_graph_and_emit_events(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
    )
    root_node_id = db.get_team_mission_graph("mission-1")["nodes"][0]["node_id"]
    db.upsert_run(
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id=root_node_id,
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )

    created = server._methods["team_mission.node.create"](
        1,
        {
            "mission_id": "mission-1",
            "run_id": "run-leader",
            "node": {
                "id": "node-worker",
                "kind": "worker",
                "title": "执行节点",
                "objective": "交付结果",
                "status": "ready",
                "metadata": {"risk_level": "low"},
            },
        },
    )
    edge = server._methods["team_mission.edge.create"](
        2,
        {
            "mission_id": "mission-1",
            "run_id": "run-leader",
            "edge": {
                "source": root_node_id,
                "target": "node-worker",
                "kind": "delegates",
            },
        },
    )
    updated = server._methods["team_mission.node.update"](
        3,
        {
            "mission_id": "mission-1",
            "run_id": "run-leader",
            "node": {"id": "node-worker", "status": "blocked", "metadata": {"block_reason": "等待审批"}},
        },
    )
    completed = server._methods["team_mission.plan.complete"](
        4,
        {"mission_id": "mission-1", "run_id": "run-leader"},
    )

    assert created["result"]["node"]["node_id"] == "node-worker"
    assert edge["result"]["edge"]["to_node_id"] == "node-worker"
    assert updated["result"]["node"]["status"] == "blocked"
    assert updated["result"]["node"]["metadata"]["risk_level"] == "low"
    assert updated["result"]["node"]["metadata"]["block_reason"] == "等待审批"
    assert completed["result"]["mission_status"] == "waiting_approval"
    assert completed["result"]["approval_requests"][0]["scope"] == "whole_graph"

    graph = db.get_team_mission_graph("mission-1")
    assert graph["mission"]["status"] == "waiting_approval"
    assert any(node["kind"] == "approval_gate" for node in graph["nodes"])
    assert ("team-mission:mission-1:approval-plan", "node-worker") in {
        (edge["from_node_id"], edge["to_node_id"])
        for edge in graph["edges"]
        if edge["metadata"].get("approval_gate")
    }

    events = db.list_team_mission_run_events("mission-1")
    source_events = [
        event["payload"]["source_event_type"]
        for event in events
        if event["type"] == "team_mission.runtime.event"
    ]
    assert source_events == [
        "mission.node.created",
        "mission.edge.created",
        "mission.node.updated",
        "mission.approval.requested",
        "mission.strategy.actions",
    ]
    approval_event = next(
        event for event in events
        if (
            event["type"] == "team_mission.runtime.event"
            and event["payload"]["source_event_type"] == "mission.approval.requested"
        )
    )
    assert approval_event["payload"]["subject"]["type"] == "approval"
    assert approval_event["payload"]["subject"]["approval_id"] == "team-mission:mission-1:approval-plan"
    assert approval_event["payload"]["subject"]["node_id"] == "team-mission:mission-1:approval-plan"
    assert approval_event["payload"]["subject"]["canonical_node_id"] == "team-mission:mission-1:approval-plan"
    assert [event["payload"]["source_event_type"] for event in events if event["type"] == "team_mission.conversation.status"] == [
        "mission.node.created",
        "mission.edge.created",
        "mission.node.updated",
        "mission.approval.requested",
        "mission.strategy.actions",
    ]


def test_team_mission_node_start_reuses_run_submit_and_binds_worker_run(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="自主执行",
        objective="自动执行节点",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="完成交付",
        status="ready",
        runtime_scope_key="profile:worker-a",
        output_contract={"format": "deliverable"},
    )
    submitted = {}

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
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    started = server._methods["team_mission.node.start"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "run_id": "run-worker",
            "turn_id": "turn-worker",
        },
    )

    assert started["result"]["stored_session_id"] == "team:mission-1:node:node-worker"
    assert submitted["stored_session_id"] == "team:mission-1:node:node-worker"
    assert submitted["runtime_scope_key"] == "profile:worker-a"
    assert "You are executing one assigned node in a DoXie team task." in submitted["text"]
    assert "完成交付" in submitted["text"]
    assert "Acceptance criteria:" in submitted["text"]
    assert "clarify tool" in submitted["text"]
    assert "clarify" in submitted["enabled_toolsets"]
    assert submitted["dovie_product_context"]["team_mission"]["node_id"] == "node-worker"

    graph = db.get_team_mission_graph("mission-1")
    node = next(item for item in graph["nodes"] if item["node_id"] == "node-worker")
    assert node["status"] == "running"
    assert node["metadata"]["run_id"] == "run-worker"
    assert node["run_id"] == "run-worker"
    assert node["stored_session_id"] == "team:mission-1:node:node-worker"
    assert node["actual_stable_session_id"] == "team:mission-1:node:node-worker"
    assert node["runtime_session_id"] == "runtime-worker"
    assert node["runtime_scope_key"] == "profile:worker-a"
    assert node["runtime_binding"]["run_id"] == "run-worker"
    assert graph["run_bindings"][0]["run_id"] == "run-worker"
    assert graph["run_bindings"][0]["runtime_session_id"] == "runtime-worker"
    events = db.list_team_mission_run_events("mission-1")
    source_events = [event for event in events if event["type"] == "team_mission.runtime.event"]
    assert source_events[-1]["payload"]["source_event_type"] == "mission.node.started"
    assert source_events[-1]["payload"]["source_event"]["payload"]["binding"]["node_id"] == "node-worker"
    assert events[-1]["type"] == "team_mission.conversation.status"
    assert events[-1]["payload"]["source_event_type"] == "mission.node.started"


def test_team_mission_node_start_registers_worker_runtime_session_shell(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    control_home = tmp_path / "control"
    profile_home = tmp_path / "profile"
    control_home.mkdir()
    profile_home.mkdir()
    control_db = SessionDB(control_home / "state.db")
    runtime_db = SessionDB(profile_home / "state.db")
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))
    monkeypatch.setattr(team_mission, "_get_db", lambda: control_db)
    monkeypatch.setattr(server, "_get_db", lambda: runtime_db)
    control_db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="启动节点",
        objective="执行节点任务",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
    )
    control_db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="完成交付",
        status="ready",
        runtime_scope_key="profile:worker-a",
    )
    expected_session_id = "team:mission-1:node:node-worker"
    assert control_db.get_session(expected_session_id) is None
    assert runtime_db.get_session(expected_session_id) is None

    submitted = {}

    def fake_run_submit(rid, params):
        assert runtime_db.get_session(expected_session_id) is not None
        submitted.update(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-worker",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    started = server._methods["team_mission.node.start"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "run_id": "run-worker",
            "turn_id": "turn-worker",
        },
    )

    assert "error" not in started
    assert started["result"]["stored_session_id"] == expected_session_id
    assert submitted["stored_session_id"] == expected_session_id
    assert control_db.get_session(expected_session_id)["source"] == "team_mission"
    assert runtime_db.get_session(expected_session_id)["source"] == "team_mission"


def test_team_mission_node_start_forwards_assignee_profile_context(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    worker_member = {
        "member_id": "worker-a",
        "profile_id": "profile-worker",
        "profile_version_id": "version-worker",
        "role": "builder",
        "runtime_scope_key": "profile:profile-worker:version:version-worker",
        "dovie_profile": {
            "id": "profile-worker",
            "agentProfileVersionId": "version-worker",
            "runtimeScopeKey": "profile:profile-worker:version:version-worker",
            "hermesHomePath": str(tmp_path / "worker-home"),
        },
    }
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="自主执行",
        objective="自动执行节点",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        members=[worker_member],
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="完成交付",
        status="ready",
        assignee_profile_id="profile-worker",
        assignee_profile_version_id="version-worker",
        output_contract={"format": "deliverable"},
    )
    submitted = {}

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
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    server._methods["team_mission.node.start"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "run_id": "run-worker",
            "turn_id": "turn-worker",
        },
    )

    assert submitted["agent_profile_id"] == "profile-worker"
    assert submitted["agent_profile_version_id"] == "version-worker"
    assert submitted["runtime_scope_key"] == "profile:profile-worker:version:version-worker"
    assert submitted["dovie_profile"]["hermesHomePath"] == str(tmp_path / "worker-home")
    assert submitted["dovie_profile"]["agentProfileVersionId"] == "version-worker"


def test_team_mission_bound_worker_run_event_updates_node_status(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        title="自主执行",
        objective="自动执行节点",
        mode="autonomous_mission",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="完成交付",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-worker",
            "stored_session_id": "session-worker",
            "run_id": "run-worker",
            "runtime_scope_key": "team:mission-1:node:node-worker",
            "seq": 1,
            "payload": {"status": "complete"},
        },
        db=db,
    )

    node = db.get_team_mission_node("mission-1", "node-worker")
    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_event"] == "message.complete"
    events = db.list_team_mission_run_events("mission-1")
    assert any(
        event["type"] == "team_mission.runtime.event"
        and event["payload"]["source_event_type"] == "message.complete"
        for event in events
    )
    memory_event = next(
        event for event in events
        if event["type"] == "team_mission.runtime.event"
        and event["payload"]["source_event_type"] == "mission.memory.compiled"
    )
    assert memory_event["payload"]["protocol"] == "team_mission.event.v1"
    assert memory_event["payload"]["kind"] == "memory.compiled"
    assert any(event["type"] == "team_mission.conversation.status" for event in events)


def test_team_mission_schedule_ready_starts_only_dependency_ready_nodes(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="completed",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-b",
        kind="worker",
        title="B",
        status="todo",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-manual",
        kind="worker",
        title="Manual",
        status="ready",
        metadata={"manual_start": True},
    )
    db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id="node-a",
        to_node_id="node-b",
        kind="depends_on",
    )
    started_nodes = []

    def fake_node_start(rid, params):
        started_nodes.append(params["node_id"])
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"node": {"node_id": params["node_id"]}},
        }

    monkeypatch.setitem(server._methods, "team_mission.node.start", fake_node_start)

    dry = server._methods["team_mission.schedule.ready"](
        1,
        {"mission_id": "mission-1", "dry_run": True},
    )
    assert dry["result"]["started"] == [{"node_id": "node-b", "dry_run": True}]
    assert dry["result"]["max_parallel_nodes"] == 3
    assert db.get_team_mission_node("mission-1", "node-b")["status"] == "ready"

    response = server._methods["team_mission.schedule.ready"](
        2,
        {"mission_id": "mission-1", "limit": 10},
    )

    assert response["result"]["ready_node_ids"] == ["node-b"]
    assert started_nodes == ["node-b"]
    assert db.get_team_mission_node("mission-1", "node-b")["status"] == "starting"


def test_team_mission_schedule_ready_marks_claimed_node_blocked_when_start_fails(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="ready",
    )

    def fake_node_start(rid, params):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": 5008, "message": "runtime unavailable"}}

    monkeypatch.setitem(server._methods, "team_mission.node.start", fake_node_start)

    response = server._methods["team_mission.schedule.ready"](
        1,
        {"mission_id": "mission-1"},
    )

    node = db.get_team_mission_node("mission-1", "node-a")
    assert response["result"]["errors"][0]["node_id"] == "node-a"
    assert node["status"] == "blocked"
    assert "runtime unavailable" in node["metadata"]["start_error"]


def test_team_mission_schedule_ready_skips_autonomous_high_risk_nodes(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-low",
        kind="worker",
        title="Low",
        status="ready",
        metadata={"risk_level": "low"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-high",
        kind="worker",
        title="High",
        status="ready",
        metadata={"risk_level": "high"},
    )
    started_nodes = []

    def fake_node_start(rid, params):
        started_nodes.append(params["node_id"])
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"node": {"node_id": params["node_id"]}},
        }

    monkeypatch.setitem(server._methods, "team_mission.node.start", fake_node_start)

    response = server._methods["team_mission.schedule.ready"](
        1,
        {"mission_id": "mission-1", "limit": 10},
    )

    assert response["result"]["ready_node_ids"] == ["node-low"]
    assert started_nodes == ["node-low"]
    assert db.get_team_mission_node("mission-1", "node-high")["status"] == "ready"


def test_team_mission_schedule_ready_respects_policy_parallel_capacity(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        status="running",
        metadata={"policy": {"maxParallelNodes": 2}},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-running",
        kind="worker",
        title="Running",
        status="running",
    )
    for node_id in ("node-ready-a", "node-ready-b"):
        db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id=node_id,
            kind="worker",
            title=node_id,
            status="ready",
        )
    started_nodes = []

    def fake_node_start(rid, params):
        started_nodes.append(params["node_id"])
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"node": {"node_id": params["node_id"]}},
        }

    monkeypatch.setitem(server._methods, "team_mission.node.start", fake_node_start)

    response = server._methods["team_mission.schedule.ready"](
        1,
        {"mission_id": "mission-1", "limit": 10},
    )

    assert response["result"]["max_parallel_nodes"] == 2
    assert response["result"]["active_node_count"] == 1
    assert response["result"]["available_slots"] == 1
    assert response["result"]["ready_node_ids"] == ["node-ready-a", "node-ready-b"]
    assert started_nodes == ["node-ready-a"]
    assert response["result"]["skipped"] == [
        {"reason": "concurrency_limit", "node_ids": ["node-ready-b"]},
    ]


def test_team_mission_schedule_ready_scopes_to_active_task(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="当前任务",
        mode="autonomous_mission",
        status="running",
        metadata={"active_task_id": "task-2", "task_id": "task-2"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="old-ready",
        kind="worker",
        title="旧任务",
        status="ready",
        metadata={"task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="current-ready",
        kind="worker",
        title="当前任务",
        status="ready",
        metadata={"task_id": "task-2"},
    )
    started_nodes = []

    def fake_node_start(rid, params):
        started_nodes.append(params["node_id"])
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"node": {"node_id": params["node_id"]}},
        }

    monkeypatch.setitem(server._methods, "team_mission.node.start", fake_node_start)

    response = server._methods["team_mission.schedule.ready"](
        1,
        {"mission_id": "mission-1", "limit": 10},
    )

    assert response["result"]["ready_node_ids"] == ["current-ready"]
    assert started_nodes == ["current-ready"]
    assert db.get_team_mission_node("mission-1", "old-ready")["status"] == "ready"


def test_team_mission_schedule_ready_caps_policy_parallel_limit_at_five(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        status="running",
        metadata={"policy": {"maxParallelNodes": 9}},
    )
    for index in range(5):
        db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id=f"node-running-{index}",
            kind="worker",
            title=f"Running {index}",
            status="running",
        )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-ready",
        kind="worker",
        title="Ready",
        status="ready",
    )

    response = server._methods["team_mission.schedule.ready"](
        1,
        {"mission_id": "mission-1", "limit": 10},
    )

    assert response["result"]["max_parallel_nodes"] == 5
    assert response["result"]["active_node_count"] == 5
    assert response["result"]["available_slots"] == 0
    assert response["result"]["started"] == []
    assert response["result"]["skipped"] == [
        {"reason": "concurrency_limit", "node_ids": ["node-ready"]},
    ]


def test_team_mission_schedule_ready_starts_auto_created_verifier(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="completed",
    )
    started_nodes = []

    def fake_node_start(rid, params):
        started_nodes.append(params["node_id"])
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"node": {"node_id": params["node_id"]}},
        }

    monkeypatch.setitem(server._methods, "team_mission.node.start", fake_node_start)

    response = server._methods["team_mission.schedule.ready"](
        1,
        {"mission_id": "mission-1"},
    )

    assert response["result"]["ready_node_ids"] == ["team-mission:mission-1:verifier"]
    assert started_nodes == ["team-mission:mission-1:verifier"]
    assert any(node["kind"] == "verifier" for node in response["result"]["graph"]["nodes"])


def test_team_mission_terminal_event_auto_starts_unblocked_child_node(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-b",
        kind="worker",
        title="B",
        objective="Run B after A",
        status="todo",
        runtime_scope_key="team:mission-1:node:node-b",
    )
    db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id="node-a",
        to_node_id="node-b",
        kind="depends_on",
    )
    db.upsert_run(
        run_id="run-a",
        session_id="session-a",
        runtime_scope_key="team:mission-1:node:node-a",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-a",
        run_id="run-a",
        session_id="session-a",
        runtime_session_id="runtime-a",
        runtime_scope_key="team:mission-1:node:node-a",
        role="worker",
    )
    submitted = []

    def fake_run_submit(rid, params):
        submitted.append(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-b",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-a",
            "stored_session_id": "session-a",
            "run_id": "run-a",
            "runtime_scope_key": "team:mission-1:node:node-a",
            "seq": 1,
            "payload": {"status": "complete"},
        },
        db=db,
    )

    assert db.get_team_mission_node("mission-1", "node-a")["status"] == "completed"
    assert db.get_team_mission_node("mission-1", "node-b")["status"] == "running"
    assert submitted[0]["dovie_product_context"]["team_mission"]["node_id"] == "node-b"
    assert "Run B after A" in submitted[0]["text"]
    assert "Acceptance criteria:" in submitted[0]["text"]
    assert "clarify tool" in submitted[0]["text"]
    assert "Team Conversation Memory Slice" in submitted[0]["text"]
    assert submitted[0]["dovie_product_context"]["team_mission"]["memory"]["kind"] == "worker_memory_slice"


def test_team_mission_node_start_injects_leader_memory_pack(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-old",
        team_id="team-1",
        title="Old mission",
        objective="Research market",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-old"},
    )
    memory_item = db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-old",
        conversation_session_id="team-session-1",
        task_id="task-old",
        scope="conversation",
        kind="summary",
        content="Previous decision: launch in Japan with partner channel.",
        source_node_ids=["node-old"],
        source_run_ids=["run-old"],
        visibility="team",
    )
    db.initialize_team_mission_from_strategy(
        mission_id="mission-new",
        team_id="team-1",
        title="New mission",
        objective="Continue Japan launch planning",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        members=[{"member_id": "leader", "role": "leader"}],
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-new"},
    )
    root_node_id = db.get_team_mission_graph("mission-new")["nodes"][0]["node_id"]
    submitted = {}

    def fake_run_submit(rid, params):
        submitted.update(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-leader",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    started = server._methods["team_mission.node.start"](
        1,
        {
            "mission_id": "mission-new",
            "node_id": root_node_id,
            "use_strategy_prompt": True,
            "run_id": "run-leader",
            "turn_id": "turn-leader",
        },
    )

    assert started["result"]["binding"]["role"] == "leader"
    assert "Team Conversation Memory Pack" in submitted["text"]
    assert "launch in Japan" in submitted["text"]
    memory_context = submitted["dovie_product_context"]["team_mission"]["memory"]
    assert memory_context["kind"] == "leader_memory_pack"
    assert memory_context["item_ids"] == [memory_item["id"]]


def test_team_mission_memory_gateway_methods(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        objective="Create launch plan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    item = db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        scope="conversation",
        kind="summary",
        content="Reusable launch summary.",
        source_node_ids=["node-a"],
        source_run_ids=["run-a"],
        visibility="team",
    )

    listed = server._methods["team_mission.memory.list"](1, {"mission_id": "mission-1"})
    assert listed["result"]["items"][0]["id"] == item["id"]

    packed = server._methods["team_mission.memory.pack"](2, {"mission_id": "mission-1", "objective": "launch"})
    assert packed["result"]["memory_pack"]["item_ids"] == [item["id"]]

    updated = server._methods["team_mission.memory.update"](
        3,
        {"memory_id": item["id"], "status": "invalidated"},
    )
    assert updated["result"]["item"]["status"] == "invalidated"

    deleted = server._methods["team_mission.memory.delete"](4, {"memory_id": item["id"]})
    assert deleted["result"]["item"]["status"] == "deleted"


def test_team_mission_terminal_event_auto_starts_verifier_finalizer(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        **_workspace_kwargs(tmp_path),
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="running",
    )
    db.upsert_run(
        run_id="run-a",
        session_id="session-a",
        runtime_scope_key="team:mission-1:node:node-a",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-a",
        run_id="run-a",
        session_id="session-a",
        runtime_session_id="runtime-a",
        runtime_scope_key="team:mission-1:node:node-a",
        role="worker",
    )
    submitted = []

    def fake_run_submit(rid, params):
        submitted.append(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params["run_id"],
                "turn_id": params["turn_id"],
                "session_id": "runtime-verifier",
                "stored_session_id": params["stored_session_id"],
                "runtime_scope_key": params["runtime_scope_key"],
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-a",
            "stored_session_id": "session-a",
            "run_id": "run-a",
            "runtime_scope_key": "team:mission-1:node:node-a",
            "seq": 1,
            "payload": {"status": "complete"},
        },
        db=db,
    )

    verifier_id = "team-mission:mission-1:verifier"
    assert db.get_team_mission_node("mission-1", verifier_id)["status"] == "running"
    assert submitted[0]["dovie_product_context"]["team_mission"]["node_id"] == verifier_id


# ── team conversation recall_turn ────────────────────────────────────
def _recall_setup_team_conversation(monkeypatch, tmp_path: Path):
    """Common scaffold: a team conv session with a worker member registered,
    and stubbed run.cancel / team_mission.cancel / session.recall_turn so we
    can observe what the recall method routes to (and skip the heavy real
    cancellation paths)."""
    import importlib
    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    db.create_session("team-session-1", source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        workspace_id="workspace-1",
        stable_session_id="team-session-1",
        title="团队会话",
    )
    # Worker member registered (for the C path test)
    db.register_member_chat_run(
        run_id="worker-run-1",
        conversation_session_id="team-session-1",
        member_id="member-bob",
        agent_profile_id="profile-bob",
        display_name="Bob",
        optimistic_run_id="team-member-run-A",
    )

    calls = {"team_mission_cancel": [], "run_cancel": [], "session_recall": []}

    def _stub_recall(rid, params):
        calls["session_recall"].append(dict(params))
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "status": "recalled",
            "removed_messages": 2,
            "draft": {"text": "你好", "attachments": []},
            "interrupted": True,
        }}

    def _stub_run_cancel(rid, params):
        calls["run_cancel"].append(dict(params))
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "status": "cancelled",
            "run_id": params.get("run_id"),
        }}

    def _stub_team_mission_cancel(rid, params):
        calls["team_mission_cancel"].append(dict(params))
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "status": "cancelled",
            "mission_id": params.get("mission_id"),
            "canceled_runs": [{"run_id": "worker-mission-run", "status": "cancelled"}],
            "cancel_errors": [],
        }}

    monkeypatch.setitem(server._methods, "session.recall_turn", _stub_recall)
    monkeypatch.setitem(server._methods, "run.cancel", _stub_run_cancel)
    monkeypatch.setitem(server._methods, "team_mission.cancel", _stub_team_mission_cancel)
    return db, calls, server


def test_recall_turn_path_C_group_chat_cancels_worker_and_syncs_view(monkeypatch, tmp_path: Path):
    """C path: @-member turn. Recall must (a) cancel the worker run on its
    memberchat: session, (b) defer to session.recall_turn for the conv, and
    (c) sync the deactivation into the member-chat view session."""
    db, calls, server = _recall_setup_team_conversation(monkeypatch, tmp_path)

    # Seed conv messages: a user @-request + a mirrored member reply.
    user_msg_id = db.append_message(
        "team-session-1", role="user", content="@Bob 帮个忙",
        metadata={"turn_id": "team-member-turn-A", "team_mission": {
            "kind": "member_chat_user", "target_member_id": "member-bob",
        }},
    )
    reply_msg_id = db.append_message(
        "team-session-1", role="assistant", content="Bob 的回复",
        metadata={"team_mission": {
            "kind": "member_chat", "member_id": "member-bob", "display_name": "Bob",
        }},
    )
    # Member-chat view session with the materialized view rows.
    db.create_session("memberchat:conv-1:member-bob", source="team_mission_member_chat", transient=False)
    db.append_message(
        "memberchat:conv-1:member-bob", role="user", content="@Bob 帮个忙",
        metadata={"member_chat_view": {"source_message_id": str(user_msg_id)}},
    )
    db.append_message(
        "memberchat:conv-1:member-bob", role="assistant", content="Bob 的回复",
        metadata={"member_chat_view": {"source_message_id": str(reply_msg_id)}},
    )

    resp = server._methods["team_mission.conversation.recall_turn"](1, {
        "conversation_id": "conv-1",
        "conversation_session_id": "team-session-1",
        "turn_id": "team-member-turn-A",
        "run_id": "team-member-run-A",
    })

    assert "error" not in resp, resp
    result = resp["result"]
    assert result["cascade_type"] == "C"
    # Worker run cancelled on its memberchat: session, NOT on the conv session.
    assert calls["run_cancel"] == [{
        "run_id": "worker-run-1",
        "stored_session_id": "memberchat:conv-1:member-bob",
        "reason": "Recalled by user.",
    }]
    assert calls["team_mission_cancel"] == []
    # Conv recall was deferred to session.recall_turn.
    assert len(calls["session_recall"]) == 1
    assert calls["session_recall"][0]["session_id"] == "team-session-1"
    assert calls["session_recall"][0]["turn_id"] == "team-member-turn-A"
    # The view rows pointing at the recalled conv messages got deactivated.
    assert result["recalled"]["view_retracted_total"] == 2
    assert result["recalled"]["view_retracted_by_session"] == {
        "memberchat:conv-1:member-bob": 2,
    }
    # The view session messages are now inactive (worker won't re-hydrate them).
    rows = db._conn.execute(  # noqa: SLF001
        "SELECT active FROM messages WHERE session_id = ?",
        ("memberchat:conv-1:member-bob",),
    ).fetchall()
    assert all(int(r["active"]) == 0 for r in rows)


def test_recall_turn_path_B_leader_mission_cancels_mission(monkeypatch, tmp_path: Path):
    """B path: a leader turn that spawned a mission. Recall must call
    team_mission.cancel (which internally cancels every node + binding), NOT
    issue separate run.cancel calls — we delegate cascade to the established
    code path."""
    db, calls, server = _recall_setup_team_conversation(monkeypatch, tmp_path)
    # Mission row so resolve picks it up.
    db.upsert_team_mission(
        mission_id="mission-X",
        conversation_id="conv-1",
        team_id="team-1",
        title="x",
        objective="x",
        mode="supervised_mission",
        status="running",
        leader_session_id="team-session-1",
    )
    db.append_message(
        "team-session-1", role="user", content="启动任务",
        metadata={"turn_id": "team-leader-turn-B"},
    )

    resp = server._methods["team_mission.conversation.recall_turn"](1, {
        "conversation_id": "conv-1",
        "conversation_session_id": "team-session-1",
        "turn_id": "team-leader-turn-B",
        "run_id": "team-leader-run-B",  # NOT in member_chat_runs
        "mission_id": "mission-X",
    })

    assert "error" not in resp, resp
    result = resp["result"]
    assert result["cascade_type"] == "B"
    assert result["cancelled"]["mission_ids"] == ["mission-X"]
    assert calls["team_mission_cancel"] == [{
        "mission_id": "mission-X",
        "canceled_by": "user",
        "reason": "Recalled by user.",
    }]
    # B path doesn't double-cancel via run.cancel — team_mission.cancel covers it.
    assert calls["run_cancel"] == []


def test_recall_turn_path_A_leader_direct_cancels_leader_run(monkeypatch, tmp_path: Path):
    """A path: a plain leader reply with no mission and no member chat.
    Recall should run.cancel the leader's run on the conv session and
    nothing else."""
    db, calls, server = _recall_setup_team_conversation(monkeypatch, tmp_path)
    db.append_message(
        "team-session-1", role="user", content="你好",
        metadata={"turn_id": "team-leader-turn-A"},
    )

    resp = server._methods["team_mission.conversation.recall_turn"](1, {
        "conversation_id": "conv-1",
        "conversation_session_id": "team-session-1",
        "turn_id": "team-leader-turn-A",
        "run_id": "team-leader-run-plain",  # not in member_chat_runs, no mission
    })

    assert "error" not in resp, resp
    result = resp["result"]
    assert result["cascade_type"] == "A"
    assert result["cancelled"]["mission_ids"] == []
    assert calls["team_mission_cancel"] == []
    assert calls["run_cancel"] == [{
        "run_id": "team-leader-run-plain",
        "stored_session_id": "team-session-1",
        "reason": "Recalled by user.",
    }]
