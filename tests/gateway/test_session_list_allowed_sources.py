"""Regression tests for the TUI gateway's ``session.list`` handler.

History:
- The original implementation hardcoded an allow-list of known gateway
  sources (``tui, cli, telegram, discord, slack, ...``). New or unlisted
  sources (``acp``, ``webhook``, user-defined ``HERMES_SESSION_SOURCE``
  values, newly-added platforms) were silently dropped from the resume
  picker — users reported "lots of sessions are missing from browse
  but exist in .hermes/sessions."
- The handler now deny-lists only internal/noisy sources such as ``tool``
  (sub-agent runs) and ``cron`` (scheduler execution contexts), and surfaces
  every human-facing source to the picker. Team mission conversations are
  surfaced through ``session.list`` with Hermes-owned route metadata.
- The handler now exposes backend pagination metadata so clients can keep
  loading older sessions without a fixed fetch cap.
"""

from __future__ import annotations

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.services import runtime_proxy


class _StubDB:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    def list_sessions_rich(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


def _call(limit: int | None = None):
    params: dict = {}
    if limit is not None:
        params["limit"] = limit
    return server.handle_request({
        "id": "1",
        "method": "session.list",
        "params": params,
    })


def test_session_list_surfaces_all_user_facing_sources(monkeypatch):
    """acp / webhook / custom sources appear; internal runtime sessions stay hidden."""
    rows = [
        {"id": "tui-1", "source": "tui", "started_at": 9},
        {"id": "tool-1", "source": "tool", "started_at": 8},
        {"id": "cron-1", "source": "cron", "started_at": 8},
        {"id": "team-1", "source": "team_mission", "started_at": 8},
        {"id": "tg-1", "source": "telegram", "started_at": 7},
        {"id": "acp-1", "source": "acp", "started_at": 6},
        {"id": "cli-1", "source": "cli", "started_at": 5},
        {"id": "webhook-1", "source": "webhook", "started_at": 4},
        {"id": "custom-1", "source": "my-custom-source", "started_at": 3},
    ]
    db = _StubDB(rows)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    resp = _call(limit=10)
    ids = [s["id"] for s in resp["result"]["sessions"]]

    # Every human-facing source — including previously-hidden acp, webhook,
    # and custom sources — must surface in the picker now.
    assert "tg-1" in ids
    assert "tui-1" in ids
    assert "cli-1" in ids
    assert "acp-1" in ids, "acp sessions were being hidden by the old allow-list"
    assert "webhook-1" in ids, "webhook sessions were being hidden by the old allow-list"
    assert "custom-1" in ids, "custom HERMES_SESSION_SOURCE values were being hidden"

    # Internal execution contexts stay hidden.
    assert "tool-1" not in ids
    assert "cron-1" not in ids
    assert "team-1" not in ids


def test_session_list_default_limit_stays_legacy_compatible(monkeypatch):
    """Clients that omit limit still get the historical broad first page."""
    db = _StubDB([{"id": "x", "source": "cli", "started_at": 1}])
    monkeypatch.setattr(server, "_get_db", lambda: db)

    _call()  # no explicit limit
    assert db.calls[0].get("limit") == 201, db.calls[0]
    assert db.calls[0].get("order_by_last_active") is True, db.calls[0]
    assert db.calls[0].get("exclude_sources") == ["tool", "cron"], db.calls[0]


def test_session_list_surfaces_team_conversation_route_metadata(tmp_path, monkeypatch):
    """Team conversations belong in the unified Hermes session list."""
    profile_home = tmp_path / "profile-home"
    seed_db = SessionDB(profile_home / "state.db")
    try:
        seed_db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            team_id="team-1",
            stable_session_id="team-session-1",
            title="团队会话标题",
            objective="团队任务预览",
            workspace_id="workspace-1",
            workspace_path="/tmp/workspace",
            active_mission_id="mission-1",
            created_at=100,
            updated_at=200,
        )
        seed_db.create_session("team-session-1", source="team_mission", transient=False)
        seed_db.append_message("team-session-1", role="user", content="hello team")
    finally:
        seed_db.close()

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "session.list",
            "params": {
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert "error" not in resp
        [item] = resp["result"]["sessions"]
        assert item["id"] == "team-session-1"
        assert item["source"] == "team_mission"
        assert item["session_kind"] == "team_mission"
        assert item["conversation_id"] == "conversation-1"
        assert item["team_id"] == "team-1"
        assert item["active_mission_id"] == "mission-1"
        assert item["title"] == "团队会话标题"
        assert item["preview"] == "团队任务预览"
        assert item["workspace"]["path"] == "/tmp/workspace"
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_session_list_hides_team_mission_node_run_sessions(tmp_path, monkeypatch):
    """Team node/member runtime sessions are mission internals, not sidebar conversations."""
    profile_home = tmp_path / "profile-home"
    seed_db = SessionDB(profile_home / "state.db")
    try:
        seed_db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            team_id="team-1",
            stable_session_id="team-session-1",
            title="团队会话",
            active_mission_id="mission-1",
            created_at=100,
            updated_at=200,
        )
        seed_db.create_session("team-session-1", source="team_mission", transient=False)
        seed_db.append_message("team-session-1", role="user", content="hello team")
        seed_db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="团队会话",
            mode="supervised_mission",
            status="running",
            leader_session_id="team-session-1",
        )
        seed_db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id="node-worker",
            kind="worker",
            title="成员节点",
            status="running",
            runtime_scope_key="team:mission-1:node:node-worker",
        )
        seed_db.create_session("team:mission-1:node:worker", source="team_mission", transient=False)
        seed_db.append_message("team:mission-1:node:worker", role="assistant", content="worker output")
        seed_db.create_session("member-session-leaked-as-tui", source="tui", transient=False)
        seed_db.append_message("member-session-leaked-as-tui", role="assistant", content="member output")
        seed_db.bind_team_mission_run(
            mission_id="mission-1",
            node_id="node-worker",
            run_id="run-worker",
            session_id="member-session-leaked-as-tui",
            runtime_session_id="runtime-worker",
            runtime_scope_key="team:mission-1:node:node-worker",
        )
    finally:
        seed_db.close()

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "session.list",
            "params": {
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert "error" not in resp
        ids = [item["id"] for item in resp["result"]["sessions"]]
        assert ids == ["team-session-1"]
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_session_list_returns_last_message_activity_as_updated_at(monkeypatch):
    db = _StubDB([
        {
            "id": "active-by-message",
            "source": "tui",
            "started_at": 1,
            "last_active": 10,
        }
    ])
    monkeypatch.setattr(server, "_get_db", lambda: db)

    resp = _call(limit=10)
    [session] = resp["result"]["sessions"]

    assert session["updated_at"] == 10


def test_session_list_overlays_live_running_state(monkeypatch):
    db = _StubDB([{"id": "stored-live", "source": "tui", "started_at": 1}])
    monkeypatch.setattr(server, "_get_db", lambda: db)
    server._sessions["runtime-live"] = {
        "session_key": "stored-live",
        "running": True,
        "active_run_id": "run-live",
        "run_started_at": 10,
        "run_updated_at": 20,
    }

    resp = _call(limit=10)
    [session] = resp["result"]["sessions"]

    assert session["id"] == "stored-live"
    assert session["running"] is True
    assert session["active_runtime_session_id"] == "runtime-live"
    assert session["active_run_id"] == "run-live"
    assert session["run_started_at"] == 10
    assert session["run_updated_at"] == 20
    server._sessions.clear()


def test_session_list_hides_only_explicit_empty_stored_placeholders(monkeypatch):
    rows = [
        {
            "id": "empty-placeholder",
            "source": "tui",
            "started_at": 3,
            "title": "",
            "preview": "",
            "message_count": 0,
        },
        {
            "id": "visible-title",
            "source": "tui",
            "started_at": 2,
            "title": "Draft conversation",
            "preview": "",
            "message_count": 0,
        },
        {
            "id": "visible-message",
            "source": "tui",
            "started_at": 1,
            "title": "",
            "preview": "hello",
            "message_count": 1,
        },
    ]
    monkeypatch.setattr(server, "_get_db", lambda: _StubDB(rows))

    resp = _call(limit=10)
    ids = [s["id"] for s in resp["result"]["sessions"]]

    assert ids == ["visible-title", "visible-message"]


def test_session_list_respects_explicit_limit(monkeypatch):
    db = _StubDB([{"id": "x", "source": "cli", "started_at": 1}])
    monkeypatch.setattr(server, "_get_db", lambda: db)

    _call(limit=10)
    assert db.calls[0].get("limit") == 11, db.calls[0]
    assert db.calls[0].get("order_by_last_active") is True, db.calls[0]


def test_session_list_returns_page_info(monkeypatch):
    rows = [
        {"id": "s1", "source": "cli", "started_at": 3, "_page_cursor": {"effective_last_active": 3, "started_at": 3, "id": "s1"}},
        {"id": "s2", "source": "cli", "started_at": 2, "_page_cursor": {"effective_last_active": 2, "started_at": 2, "id": "s2"}},
        {"id": "s3", "source": "cli", "started_at": 1, "_page_cursor": {"effective_last_active": 1, "started_at": 1, "id": "s3"}},
    ]
    db = _StubDB(rows)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    resp = _call(limit=2)

    assert [s["id"] for s in resp["result"]["sessions"]] == ["s1", "s2"]
    assert resp["result"]["pageInfo"]["hasMore"] is True
    assert resp["result"]["pageInfo"]["nextCursor"]


def test_session_list_preserves_ordering_after_filter(monkeypatch):
    rows = [
        {"id": "newest", "source": "telegram", "started_at": 5},
        {"id": "internal", "source": "tool", "started_at": 4},
        {"id": "cron-runtime", "source": "cron", "started_at": 4},
        {"id": "middle", "source": "tui", "started_at": 3},
        {"id": "also-visible", "source": "webhook", "started_at": 2},
        {"id": "oldest", "source": "discord", "started_at": 1},
    ]
    monkeypatch.setattr(server, "_get_db", lambda: _StubDB(rows))

    resp = _call()
    ids = [s["id"] for s in resp["result"]["sessions"]]

    assert ids == ["newest", "middle", "also-visible", "oldest"]


def test_session_list_reads_requested_doxie_profile_home(tmp_path, monkeypatch):
    """Control-plane session.list must read the requested profile/version DB."""
    profile_home = tmp_path / "profile-home"
    seed_db = SessionDB(profile_home / "state.db")
    try:
        seed_db.create_session("stored-1", "tui")
        seed_db.append_message("stored-1", "user", "hello from profile db")
    finally:
        seed_db.close()

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "session.list",
            "params": {
                "agentProfileId": "agent-a",
                "agentProfileVersionId": "version-1",
                "runtimeScopeKey": "profile:agent-a:version:version-1",
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert resp["result"]["sessions"][0]["id"] == "stored-1"
        assert resp["result"]["sessions"][0]["preview"] == "hello from profile db"
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_team_conversation_list_reads_requested_doxie_profile_home(tmp_path, monkeypatch):
    """Team conversation history must use the same profile-home routing as session.list."""
    profile_home = tmp_path / "profile-home"
    seed_db = SessionDB(profile_home / "state.db")
    try:
        seed_db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            team_id="team-1",
            stable_session_id="team-session-1",
            title="Profile scoped team conversation",
            workspace_id="workspace-1",
            workspace_path="/tmp/workspace",
            updated_at=200,
        )
        seed_db.create_session("team-session-1", source="team_mission", transient=False)
        seed_db.append_message("team-session-1", role="user", content="hello from profile team conversation")
    finally:
        seed_db.close()

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "team_mission.conversation.list",
            "params": {
                "agentProfileId": "agent-a",
                "agentProfileVersionId": "version-1",
                "runtimeScopeKey": "profile:agent-a:version:version-1",
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert resp["result"]["conversations"][0]["conversation_id"] == "conversation-1"
        assert resp["result"]["conversations"][0]["title"] == "Profile scoped team conversation"
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_team_conversation_list_returns_empty_for_profile_home_without_state_db(tmp_path, monkeypatch):
    """Enumerating every profile/version scope should not turn unused homes into UI errors."""
    profile_home = tmp_path / "empty-profile-home"

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "team_mission.conversation.list",
            "params": {
                "agentProfileId": "agent-a",
                "agentProfileVersionId": "version-empty",
                "runtimeScopeKey": "profile:agent-a:version:version-empty",
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-empty",
                    "runtimeScopeKey": "profile:agent-a:version:version-empty",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert "error" not in resp
        assert resp["result"]["conversations"] == []
        assert not (profile_home / "state.db").exists()
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_team_conversation_list_is_control_plane_read_for_profile_scope():
    assert runtime_proxy.should_proxy_to_runtime({
        "id": "1",
        "method": "team_mission.conversation.list",
        "params": {
            "agentProfileId": "agent-a",
            "runtimeScopeKey": "profile:agent-a",
            "doxie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
            },
        },
    }) is False


def test_team_conversation_list_projects_active_mission_runtime_state(tmp_path, monkeypatch):
    profile_home = tmp_path / "profile-home"
    seed_db = SessionDB(profile_home / "state.db")
    try:
        seed_db.upsert_team_mission_conversation(
            conversation_id="conversation-running",
            team_id="team-1",
            stable_session_id="team-session-running",
            title="运行中团队会话",
            active_mission_id="mission-running",
            created_at=100,
            updated_at=200,
        )
        seed_db.create_session("team-session-running", source="team_mission", transient=False)
        seed_db.upsert_team_mission(
            mission_id="mission-running",
            conversation_id="conversation-running",
            team_id="team-1",
            title="运行中任务",
            mode="supervised_mission",
            status="running",
            leader_session_id="team-session-running",
        )
        seed_db.upsert_team_mission_node(
            mission_id="mission-running",
            node_id="node-running",
            kind="worker",
            title="运行中节点",
            status="running",
            runtime_scope_key="team:mission-running:node:node-running",
        )
        seed_db.bind_team_mission_run(
            mission_id="mission-running",
            node_id="node-running",
            run_id="run-worker",
            session_id="worker-session-1",
            runtime_session_id="runtime-worker-1",
            runtime_scope_key="team:mission-running:node:node-running",
            role="worker",
        )
    finally:
        seed_db.close()

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "team_mission.conversation.list",
            "params": {
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert "error" not in resp
        [conversation] = resp["result"]["conversations"]
        assert conversation["conversation_id"] == "conversation-running"
        assert conversation["running"] is True
        assert conversation["run_state"] == "running"
        assert conversation["mission_status"] == "running"
        assert conversation["active_node_count"] == 1
        assert conversation["task_frame_count"] == 1
        assert conversation["task_frames"][0]["missionId"] == "mission-running"
        assert conversation["run_session_ids"] == ["worker-session-1", "runtime-worker-1"]
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_team_conversation_list_projects_final_deliverable_and_artifacts(tmp_path, monkeypatch):
    profile_home = tmp_path / "profile-home"
    seed_db = SessionDB(profile_home / "state.db")
    try:
        seed_db.upsert_team_mission_conversation(
            conversation_id="conversation-completed",
            team_id="team-1",
            stable_session_id="team-session-completed",
            title="已完成团队会话",
            active_mission_id="mission-completed",
            created_at=100,
            updated_at=200,
        )
        seed_db.create_session("team-session-completed", source="team_mission", transient=False)
        seed_db.upsert_team_mission(
            mission_id="mission-completed",
            conversation_id="conversation-completed",
            team_id="team-1",
            title="交付任务",
            mode="supervised_mission",
            status="completed",
            leader_session_id="team-session-completed",
            metadata={"task_id": "task-completed"},
        )
        seed_db.upsert_team_mission_node(
            mission_id="mission-completed",
            node_id="synthesis",
            kind="synthesizer",
            title="汇总",
            status="completed",
            metadata={"task_id": "task-completed"},
        )
        message_id = seed_db.append_message(
            "team-session-completed",
            "assistant",
            "最终汇总",
            metadata={
                "team_mission": {
                    "kind": "final_deliverable",
                    "mission_id": "mission-completed",
                    "node_id": "synthesis",
                    "task_id": "task-completed",
                    "source_run_id": "run-synthesis",
                    "source_session_id": "worker-session-completed",
                }
            },
        )
        seed_db.upsert_team_mission_memory_item(
            memory_id="memory-artifact-completed",
            team_id="team-1",
            mission_id="mission-completed",
            conversation_session_id="team-session-completed",
            task_id="task-completed",
            content="交付文件",
            artifact_refs=[{"path": "/tmp/final.txt", "title": "final.txt"}],
        )
    finally:
        seed_db.close()

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "team_mission.conversation.list",
            "params": {
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert "error" not in resp
        [conversation] = resp["result"]["conversations"]
        assert conversation["conversation_id"] == "conversation-completed"
        assert conversation["run_state"] == "completed"
        assert conversation["final_deliverables"][0]["messageId"] == str(message_id)
        assert conversation["artifact_refs"] == [{"path": "/tmp/final.txt", "title": "final.txt"}]
        assert conversation["task_frames"][0]["finalDeliverable"]["content"] == "最终汇总"
        assert conversation["task_frames"][0]["artifactRefs"] == conversation["artifact_refs"]
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_team_conversation_list_prioritizes_approval_gate_state(tmp_path, monkeypatch):
    profile_home = tmp_path / "profile-home"
    seed_db = SessionDB(profile_home / "state.db")
    try:
        seed_db.upsert_team_mission_conversation(
            conversation_id="conversation-approval",
            team_id="team-1",
            stable_session_id="team-session-approval",
            title="待审批团队会话",
            active_mission_id="mission-approval",
            created_at=100,
            updated_at=200,
        )
        seed_db.create_session("team-session-approval", source="team_mission", transient=False)
        seed_db.upsert_team_mission(
            mission_id="mission-approval",
            conversation_id="conversation-approval",
            team_id="team-1",
            title="待审批任务",
            mode="supervised_mission",
            status="running",
            leader_session_id="team-session-approval",
        )
        seed_db.upsert_team_mission_node(
            mission_id="mission-approval",
            node_id="approval-plan",
            kind="approval_gate",
            title="审批任务图",
            status="waiting_approval",
        )
    finally:
        seed_db.close()

    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "team_mission.conversation.list",
            "params": {
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })
        assert "error" not in resp
        [conversation] = resp["result"]["conversations"]
        assert conversation["conversation_id"] == "conversation-approval"
        assert conversation["running"] is True
        assert conversation["run_state"] == "waiting_approval"
        assert conversation["waiting_approval"] is True
        assert conversation["pending_approval_count"] == 1
        assert conversation["pending_approvals"][0]["nodeId"] == "mission-approval:approval-plan"
    finally:
        for db in list(server._db_by_home.values()):
            db.close()


def test_conversation_activity_list_projects_run_and_approval_state(monkeypatch):
    class _ActivityDB(_StubDB):
        def get_team_mission_conversation_by_session(self, session_id):
            if session_id != "team-session-approval":
                return {}
            return {
                "conversation_id": "conversation-approval",
                "stable_session_id": "team-session-approval",
                "team_id": "team-1",
                "active_mission_id": "mission-approval",
                "status": "waiting_approval",
                "title": "Team",
            }

        def team_mission_run_session_ids(self, _session_ids):
            return set()

        def get_team_mission_graph(self, mission_id):
            if mission_id != "mission-approval":
                return {}
            return {
                "mission": {
                    "mission_id": "mission-approval",
                    "status": "waiting_approval",
                }
            }

    db = _ActivityDB([
        {
            "id": "ordinary-running",
            "source": "tui",
            "title": "Ordinary",
            "started_at": 1,
            "message_count": 1,
        },
        {
            "id": "team-session-approval",
            "source": "team_mission",
            "session_kind": "team_mission",
            "conversation_id": "conversation-approval",
            "team_id": "team-1",
            "active_mission_id": "mission-approval",
            "status": "active",
            "title": "Team",
            "started_at": 2,
            "message_count": 1,
        },
    ])
    monkeypatch.setattr(server, "_get_db", lambda: db)
    server._sessions["runtime-ordinary"] = {
        "session_key": "ordinary-running",
        "running": True,
        "active_run_id": "run-ordinary",
        "active_turn_id": "turn-ordinary",
        "run_started_at": 10,
        "run_updated_at": 20,
    }
    original_approval = server._methods.get("approval.pending.list")

    def fake_pending_approvals(rid, params):
        session_id = params.get("stored_session_id")
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "approvals": [{"id": "approval-1"}] if session_id == "ordinary-running" else [],
            },
        }

    server._methods["approval.pending.list"] = fake_pending_approvals
    try:
        resp = server.handle_request({
            "id": "1",
            "method": "conversation.activity.list",
            "params": {"limit": 10},
        })
    finally:
        server._sessions.clear()
        if original_approval is not None:
            server._methods["approval.pending.list"] = original_approval

    assert "error" not in resp
    activities = {item["stable_session_id"]: item for item in resp["result"]["activities"]}
    assert activities["ordinary-running"]["run_state"] == "waiting_approval"
    assert activities["ordinary-running"]["pending_approval_count"] == 1
    assert activities["ordinary-running"]["active_run_id"] == "run-ordinary"
    assert activities["team-session-approval"]["kind"] == "team_mission"
    assert activities["team-session-approval"]["run_state"] == "waiting_approval"
    assert activities["team-session-approval"]["mission_id"] == "mission-approval"


def test_conversation_activity_list_is_control_plane_read_for_profile_scope():
    assert runtime_proxy.should_proxy_to_runtime({
        "id": "1",
        "method": "conversation.activity.list",
        "params": {
            "agentProfileId": "agent-a",
            "runtimeScopeKey": "profile:agent-a",
            "doxie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
            },
        },
    }) is False


def test_session_messages_returns_paged_transcript(monkeypatch):
    class _MessagesDB:
        def get_session(self, session_id):
            return {"id": session_id} if session_id == "s1" else None

        def get_messages_page_as_conversation(self, *args, **kwargs):
            assert args[0] == "s1"
            assert kwargs["direction"] == "before"
            assert kwargs["cursor_id"] == 20
            return {
                "messages": [
                    {"id": 10, "role": "user", "content": "older", "timestamp": 10.0},
                ],
                "pageInfo": {
                    "prev_cursor_id": 10,
                    "next_cursor_id": 10,
                    "hasMoreBefore": False,
                    "hasMoreAfter": True,
                    "totalCount": 3,
                },
            }

        def list_run_events(self, *args, **kwargs):
            assert args[0] == "s1"
            assert kwargs["runtime_scope_key"] == "profile:agent-default:version:v1"
            return [
                {
                    "type": "tool.complete",
                    "stored_session_id": "s1",
                    "run_id": "run-1",
                    "turn_id": "turn-1",
                    "seq": 4,
                    "payload": {
                        "name": "create_agent_profile_draft",
                        "result": {"doxie_event": "agent_profile_draft_saved", "draft": {"id": "draft-1"}},
                    },
                },
            ]

    cursor = server._methods["session.messages"].__globals__["_encode_page_cursor"]({"id": 20})
    monkeypatch.setattr(server, "_get_db", lambda: _MessagesDB())

    resp = server.handle_request({
        "id": "1",
        "method": "session.messages",
        "params": {
            "session_id": "s1",
            "direction": "before",
            "cursor": cursor,
            "limit": 1,
            "includeRunEvents": True,
            "runtimeScopeKey": "profile:agent-default:version:v1",
        },
    })

    assert resp["result"]["messages"] == [
        {"role": "user", "text": "older", "message_id": "10", "timestamp": 10.0},
    ]
    assert resp["result"]["runEvents"][0]["type"] == "tool.complete"
    assert resp["result"]["runEvents"][0]["payload"]["result"]["draft"]["id"] == "draft-1"
    assert resp["result"]["pageInfo"]["hasMoreBefore"] is False
    assert resp["result"]["pageInfo"]["hasMoreAfter"] is True
    assert resp["result"]["pageInfo"]["totalCount"] == 3


def test_session_status_reads_stored_profile_session_without_runtime(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile-home"
    db = SessionDB(db_path=profile_home / "state.db")
    try:
        db.create_session("stored-1", source="tui")
        monkeypatch.setattr(server, "_db_by_home", {})
        monkeypatch.setattr(server, "_db_error_by_home", {})
        resp = server.handle_request({
            "id": "1",
            "method": "session.status",
            "params": {
                "session_id": "stored-1",
                "doxie_profile": {
                    "id": "agent-a",
                    "agentProfileVersionId": "version-1",
                    "runtimeScopeKey": "profile:agent-a:version:version-1",
                    "hermesHomePath": str(profile_home),
                },
            },
        })

        assert "error" not in resp
        assert resp["result"]["stored_session_id"] == "stored-1"
        assert resp["result"]["running"] is False
    finally:
        db.close()
