from __future__ import annotations

from types import SimpleNamespace

from hermes_team_mission.runtime import profile_scope
from hermes_team_mission.state.store import TeamMissionStateStore
from tui_gateway.services.worker_supervisor import DB_RPC_ALLOWED_METHODS
from hermes_agent.orchestration.worker_rpc_proxy import set_default_worker_rpc_proxy


class _Proxy:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        return self.response


class _WorkerDBProxySentinel:
    db_path = "worker-db-proxy:team-session-1"


class _DirectDBSentinel:
    db_path = "/profile/state.db"


def test_team_mission_gateway_call_routes_from_worker_to_control_plane_rpc():
    response = {"jsonrpc": "2.0", "id": "worker-team-mission:1", "result": {"mission_id": "m1"}}
    proxy = _Proxy(response)
    set_default_worker_rpc_proxy(proxy)
    try:
        result = profile_scope.gateway_call("team_mission.create", {"mission_id": "m1"})
    finally:
        set_default_worker_rpc_proxy(None)

    assert result == response
    assert proxy.calls == [
        (
            "worker.team_mission_gateway_call",
            {"method": "team_mission.create", "params": {"mission_id": "m1"}},
        )
    ]


def test_team_mission_gateway_call_rejects_unknown_methods_inside_worker():
    proxy = _Proxy({"jsonrpc": "2.0", "id": "unused", "result": {}})
    set_default_worker_rpc_proxy(proxy)
    try:
        result = profile_scope.gateway_call("session.resume", {"session_id": "s1"})
    finally:
        set_default_worker_rpc_proxy(None)

    assert result["error"]["message"] == (
        "Gateway method session.resume is not allowed from Team Mission worker runtime."
    )
    assert proxy.calls == []


def test_team_mission_control_db_prefers_worker_proxy_over_control_home(monkeypatch, tmp_path):
    control_home = tmp_path / "control"
    control_home.mkdir()
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))
    proxy_db = _WorkerDBProxySentinel()

    result = profile_scope.team_mission_control_db(SimpleNamespace(_session_db=proxy_db))

    assert result is proxy_db


def test_team_mission_control_db_keeps_control_home_for_direct_profile_db(monkeypatch, tmp_path):
    control_home = tmp_path / "control"
    control_home.mkdir()
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))

    result = profile_scope.team_mission_control_db(SimpleNamespace(_session_db=_DirectDBSentinel()))
    try:
        assert isinstance(result, TeamMissionStateStore)
        assert str(result.db_path) == str(control_home / "state.db")
    finally:
        result.close()


def test_team_mission_control_db_writes_through_team_mission_store(monkeypatch, tmp_path):
    control_home = tmp_path / "control"
    control_home.mkdir()
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))

    db = profile_scope.team_mission_control_db()
    try:
        assert isinstance(db, TeamMissionStateStore)
        conversation = db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="session-1",
            team_id="team-1",
            title="Team Mission",
        )
        mission = db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Create artifact",
            metadata={"conversation_session_id": "session-1"},
        )
        graph = db.get_team_mission_graph("mission-1")
    finally:
        db.close()

    assert conversation["conversation_session_id"] == "session-1"
    assert mission["mission_id"] == "mission-1"
    assert graph["mission"]["conversation_id"] == "conversation-1"


def test_team_mission_planning_completion_db_method_is_available_to_worker_ipc():
    assert "complete_team_mission_plan" in DB_RPC_ALLOWED_METHODS
