import json
from pathlib import Path
import time
from types import SimpleNamespace


class _MemoryTransport:
    def __init__(self):
        self.frames = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


def _capability_source_packet() -> dict:
    return {
        "teamId": "team-1",
        "team": {
            "name": "研发验收组",
            "description": "面向工程实现和质量验收的团队。",
            "defaultMode": "supervised_mission",
        },
        "members": [
            {
                "memberId": "leader",
                "agentProfileId": "profile-leader",
                "displayName": "多多",
                "role": "leader",
                "profile": {
                    "description": "负责需求沟通、规划和验收协调。",
                    "tags": ["planning", "quality"],
                },
            },
            {
                "memberId": "builder",
                "agentProfileId": "profile-builder",
                "displayName": "推进工程师",
                "role": "engineer",
                "capabilityTags": ["code", "automation"],
                "profile": {
                    "description": "负责代码实现、文件编辑和测试自动化。",
                    "tags": ["engineering"],
                    "defaultToolsets": ["terminal", "file"],
                },
            },
        ],
    }


def test_team_capability_gateway_get_refresh_and_bind(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    get_response = server._methods["team_capability.snapshot.get"](
        1,
        {"source_packet": _capability_source_packet()},
    )
    refresh_response = server._methods["team_capability.snapshot.refresh"](
        2,
        {"source_packet": _capability_source_packet()},
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


def test_team_mission_leader_node_toolsets_are_phase_independent():
    import importlib

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")

    assert team_mission._start_toolsets(
        {},
        {"mode": "supervised_mission"},
        {"kind": "root", "metadata": {"role": "leader", "phase": "planning"}},
    ) == ["team_mission_leader", "team_mission_planning"]
    assert team_mission._start_toolsets(
        {},
        {"mode": "supervised_mission"},
        {"kind": "root", "metadata": {"role": "leader", "phase": "verifying"}},
    ) == ["team_mission_leader"]


def test_team_profile_get_resolves_conversation_source_packet_without_active_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
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
            "team_capability": {"source_packet": _capability_source_packet()},
        },
    )

    result = response["result"]
    assert result["mission_id"] == ""
    assert result["team_id"] == "team-1"
    assert result["source"] == "source_packet"
    assert result["snapshot"]["team_id"] == "team-1"
    assert [member["display_name"] for member in result["snapshot"]["member_profiles"]] == ["多多", "推进工程师"]


def test_leader_team_profile_tool_forwards_conversation_capability_packet(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    leader_tools = importlib.import_module("tools.team_mission_leader_tools")
    profile_tools = importlib.import_module("tools.team_mission_profile_tools")
    db = SessionDB(tmp_path / "state.db")
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
                "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
                "team_capability": {"source_packet": _capability_source_packet()},
            }
        },
    )

    payload = json.loads(leader_tools._handle_team_profile({}, SimpleNamespace(_session_db=db)))

    assert payload["success"] is True
    assert payload["mission_id"] == ""
    assert payload["snapshot"]["team_id"] == "team-1"
    assert [member["display_name"] for member in payload["snapshot"]["member_profiles"]] == ["多多", "推进工程师"]
    assert "evidence_refs" not in payload["snapshot"]
    assert all("evidence_refs" not in member for member in payload["snapshot"]["member_profiles"])


def test_team_mission_gateway_methods_create_graph_and_replay_events(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
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
            "members": [
                {
                    "member_id": "leader",
                    "profile_id": "profile-leader",
                    "role": "leader",
                },
            ],
            "team_capability": {"source_packet": _capability_source_packet()},
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
    assert submitted["enabled_toolsets"] == ["team_mission_leader", "team_mission_planning"]
    assert "delegation" in submitted["disabled_toolsets"]
    assert submitted["toolset_scope"] == "exact"
    assert submitted["doxie_product_context"]["team_mission"]["node_role"] == "leader"
    assert submitted["doxie_product_context"]["team_mission"]["node_phase"] == "planning"
    assert submitted["doxie_product_context"]["team_mission"]["tool_policy"]["blocked_tools"] == ["delegate_task"]
    assert submitted["doxie_product_context"]["team_mission"]["tool_policy"]["toolset_scope"] == "exact"

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
    message_events = [event for event in events if event["type"] == "message.delta"]
    assert events_response["result"]["last_event_seq"] >= message_events[-1]["seq"]
    assert message_events[-1]["payload"]["mission_id"] == "mission-1"
    assert message_events[-1]["payload"]["delta"] == "规划中"


def test_team_mission_create_conversation_only_does_not_create_or_start_graph(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
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
            "workspace": {
                "workspace_id": "workspace-1",
                "workspace_path": "/tmp/workspace",
            },
            "metadata": {"stableTeamSessionId": "team-session-1"},
            "members": [{"member_id": "leader", "role": "leader"}],
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

    submit_response = server._methods["team_mission.message.submit"](
        2,
        {
            "conversation_id": "mission-1",
            "conversation_session_id": "team-session-1",
            "text": "你好啊",
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
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


def test_team_mission_node_create_requires_existing_mission(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
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


def test_team_mission_create_records_user_task_in_stable_team_session(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
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
            "workspace": {
                "workspace_id": "workspace-1",
                "workspace_path": "/tmp/workspace",
            },
            "metadata": {"stableTeamSessionId": "team-session-1"},
            "members": [{"member_id": "leader", "role": "leader"}],
        },
    )

    assert response["result"]["graph"]["mission"]["workspace_id"] == "workspace-1"
    assert response["result"]["graph"]["mission"]["workspace_path"] == "/tmp/workspace"
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
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

    response = server._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-1",
            "text": "你好，上一轮进度怎么样？",
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
        },
    )

    assert response["result"]["conversation_session_id"] == "team-session-1"
    assert submitted["stored_session_id"] == "team-session-1"
    assert submitted["persist_user_message"] == "你好，上一轮进度怎么样？"
    assert submitted["enabled_toolsets"] == [
        "team_mission_leader",
        "clarify",
        "file",
        "terminal",
        "todo",
    ]
    assert "delegation" in submitted["disabled_toolsets"]
    assert submitted["toolset_scope"] == "exact"
    assert "team_mission_start_task" in submitted["text"]
    assert submitted["doxie_product_context"]["team_mission"]["kind"] == "leader_conversation"
    assert submitted["doxie_product_context"]["team_mission"]["tool_policy"]["disabled_toolsets"] == ["delegation"]
    assert submitted["doxie_product_context"]["team_mission"]["tool_policy"]["toolset_scope"] == "exact"
    assert len(db.get_team_mission_graph("mission-1")["nodes"]) == 1


def test_team_mission_message_submit_merges_requested_leader_conversation_toolsets(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
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
        "team_mission_leader",
        "clarify",
        "file",
        "terminal",
        "todo",
        "skills",
    ]
    assert submitted["toolset_scope"] == "exact"
    assert "delegation" in submitted["disabled_toolsets"]


def test_team_mission_member_node_start_keeps_delegation_available(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
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
    assert "toolset_scope" not in submitted
    assert "tool_policy" not in submitted["doxie_product_context"]["team_mission"]


def test_team_mission_message_submit_does_not_inject_other_conversation_memory(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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
    memory_context = submitted["doxie_product_context"]["team_mission"]["memory"]
    assert memory_context["kind"] == "leader_conversation_memory_pack"
    assert memory_context["item_ids"] == []


def test_team_mission_conversation_ensure_creates_missing_stable_session(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.initialize_team_mission_from_strategy(
        mission_id="mission-1",
        team_id="team-1",
        title="监督执行",
        objective="初始任务",
        mode="supervised_mission",
        leader_session_id="team-session-legacy",
        metadata={"conversation_session_id": "team-session-legacy"},
        members=[{"member_id": "leader", "role": "leader"}],
    )

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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.conversation.ensure"](
        1,
        {
            "mission_id": "mission-missing-in-hermes",
            "conversation_session_id": "team-session-from-doxie",
        },
    )

    assert response["result"]["mission_id"] == "mission-missing-in-hermes"
    assert response["result"]["conversation_session_id"] == "team-session-from-doxie"
    assert response["result"]["created"] is True
    assert db.get_session("team-session-from-doxie")["source"] == "team_mission"


def test_team_mission_conversation_rename_gateway_updates_canonical_state(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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
    assert db.get_session("team-session-1")["title"] == "新团队任务"
    assert db.get_team_mission_graph("mission-1")["mission"]["title"] == "旧团队任务"


def test_team_mission_conversation_delete_gateway_blocks_active_leader_run(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
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
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker-1",
        runtime_scope_key="team:mission-1:node-worker",
        role="worker",
    )

    response = server._methods["team_mission.conversation.delete"](
        1,
        {"conversation_id": "conversation-1"},
    )

    assert response["result"]["deleted"] is True
    assert response["result"]["conversation_id"] == "conversation-1"
    assert response["result"]["stable_session_id"] == "team-session-1"
    assert response["result"]["run_session_ids"] == ["worker-session-1", "runtime-worker-1"]
    assert db.resolve_team_mission_conversation("conversation-1") == {}
    assert db.get_session("team-session-1") is None


def test_team_mission_leader_start_task_tool_starts_planning_node(monkeypatch, tmp_path: Path):
    import importlib
    import json

    import tools.team_mission_leader_tools  # noqa: F401
    import tools.team_mission_planning_tools  # noqa: F401
    from hermes_state import SessionDB
    from tools.registry import registry
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    graph = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "title": "监督执行",
            "objective": "初始任务",
            "mode": "supervised_mission",
            "conversation_only": True,
            "metadata": {"stableTeamSessionId": "team-session-1"},
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
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
    monkeypatch.setenv(
        "HERMES_DOXIE_PRODUCT_CONTEXT",
        json.dumps({
                "team_mission": {
                    "kind": "leader_conversation",
                    "conversation_id": "mission-1",
                    "conversation_session_id": "team-session-1",
                    "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
                }
        }),
    )
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

    assert result["success"] is True
    assert result["intent"] == "start_team_task"
    assert result["mission_id"] != "mission-1"
    assert result["conversation_id"] == "mission-1"
    assert result["node"]["node_id"] == f"team-mission:{result['mission_id']}:root"
    assert submitted["record_user_task_message"] is False
    assert submitted["agent_profile_id"] == "profile-leader"
    assert submitted["enabled_toolsets"] == ["team_mission_leader", "team_mission_planning"]
    assert "delegation" in submitted["disabled_toolsets"]
    assert submitted["toolset_scope"] == "exact"
    assert submitted["doxie_product_context"]["team_mission"]["node_phase"] == "planning"
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
            },
            parent_agent=planning_agent,
        )
    )
    assert created_worker["success"] is True
    assert created_worker["node"]["metadata"]["task_id"] == "task-2"
    assert created_worker["node"]["metadata"]["task_title"] == "第二个任务"
    assert created_worker["node"]["metadata"]["task_objective"] == "规划并执行第二个任务"
    completed = json.loads(
        registry.dispatch(
            "team_mission_plan_complete",
            {},
            parent_agent=planning_agent,
        )
    )
    assert completed["success"] is True
    assert completed["approval_requests"][0]["task_id"] == "task-2"
    completed_nodes = completed["graph"]["nodes"]
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


def test_team_mission_direct_root_task_activation_replaces_draft_objective(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
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
            "metadata": {"stableTeamSessionId": "team-session-1"},
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
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
            "task_id": "task-filescan",
            "record_user_task_message": False,
            "members": [{"member_id": "leader", "profile_id": "profile-leader", "role": "leader"}],
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
    message_complete_events = [event for event in team_events if event["type"] == "message.complete"]
    assert len(message_complete_events) == 1
    assert message_complete_events[0]["run_id"] == "run-leader"
    assert message_complete_events[0]["payload"]["text"] == "Leader 已完成任务图规划"


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
            "type": "message.complete",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 1,
            "payload": {"text": "最终汇总交付内容", "status": "complete"},
        },
        db=db,
    )

    mirrored_events = db.list_run_events("team-session-1")
    assert [event["type"] for event in mirrored_events] == ["message.complete"]
    mirrored = mirrored_events[0]
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
    assert messages[0]["metadata"]["team_mission"]["source_run_id"] == "run-synthesis"


def test_team_mission_cancel_marks_graph_and_cancels_active_runs(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "mode": "manual_graph",
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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
    assert subscribed["result"]["events"][0]["payload"]["delta"] == "先前事件"

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={"type": "message.delta", "seq": 2, "payload": {"delta": "实时事件"}},
    )

    deadline = time.time() + 2
    while time.time() < deadline:
        if any(
            ((frame.get("params") or {}).get("payload") or {}).get("delta") == "实时事件"
            for frame in transport.frames
        ):
            break
        time.sleep(0.05)

    assert any(
        ((frame.get("params") or {}).get("payload") or {}).get("delta") == "实时事件"
        for frame in transport.frames
    )

    removed = run_control.unsubscribe_session(subscription_id=subscription_id)
    assert removed == 1


def test_team_mission_node_history_reads_runtime_from_hermes_store(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    team_mission_history = importlib.import_module("tui_gateway.methods.team_mission_history")
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
        status="completed",
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


def test_team_mission_planner_methods_mutate_graph_and_emit_events(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    events = [event["type"] for event in db.list_team_mission_run_events("mission-1")]
    assert events == [
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
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
    assert submitted["text"] == "完成交付"
    assert submitted["doxie_product_context"]["team_mission"]["node_id"] == "node-worker"

    graph = db.get_team_mission_graph("mission-1")
    node = next(item for item in graph["nodes"] if item["node_id"] == "node-worker")
    assert node["status"] == "running"
    assert node["metadata"]["run_id"] == "run-worker"
    assert graph["run_bindings"][0]["run_id"] == "run-worker"
    assert graph["run_bindings"][0]["runtime_session_id"] == "runtime-worker"
    events = db.list_team_mission_run_events("mission-1")
    assert events[-1]["type"] == "mission.node.started"
    assert events[-1]["payload"]["binding"]["node_id"] == "node-worker"


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
    assert any(event["type"] == "message.complete" for event in events)
    assert events[-1]["type"] == "mission.memory.compiled"


def test_team_mission_schedule_ready_starts_only_dependency_ready_nodes(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission", status="running")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission", status="running")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission", status="running")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission", status="running")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission", status="running")
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
    assert submitted[0]["doxie_product_context"]["team_mission"]["node_id"] == "node-b"
    assert submitted[0]["text"].startswith("Run B after A")
    assert "Team Conversation Memory Slice" in submitted[0]["text"]
    assert submitted[0]["doxie_product_context"]["team_mission"]["memory"]["kind"] == "worker_memory_slice"


def test_team_mission_node_start_injects_leader_memory_pack(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(
        mission_id="mission-old",
        team_id="team-1",
        title="Old mission",
        objective="Research market",
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
    memory_context = submitted["doxie_product_context"]["team_mission"]["memory"]
    assert memory_context["kind"] == "leader_memory_pack"
    assert memory_context["item_ids"] == [memory_item["id"]]


def test_team_mission_memory_gateway_methods(monkeypatch, tmp_path: Path):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
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

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission", status="running")
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
    assert submitted[0]["doxie_product_context"]["team_mission"]["node_id"] == verifier_id
