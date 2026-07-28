from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


def _isolate_delete_side_effects(monkeypatch, gateway, tmp_path: Path) -> None:
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(
        gateway,
        "delete_session_artifacts",
        lambda _session_ids: {
            "deleted_artifact_links": 0,
            "deleted_artifacts": 0,
            "deleted_artifact_ids": [],
            "physical_files_deleted": 0,
        },
    )
    monkeypatch.setattr(gateway, "delete_session_workspace_bindings", lambda _session_ids: [])


def test_conversation_delete_is_idempotent_for_direct_sessions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gateway = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(gateway, "_get_db", lambda: db)
    _isolate_delete_side_effects(monkeypatch, gateway, tmp_path)
    try:
        db.sessions.create("direct-session-1", source="cli")
        db.session_index.upsert(session_id="direct-session-1", source="cli")

        response = server._methods["conversation.delete"](
            1,
            {"conversation_session_id": "direct-session-1"},
        )

        assert response["result"]["deleted"] is True
        assert response["result"]["existed"] is True
        assert response["result"]["conversation_kind"] == "direct"
        assert response["result"]["conversation_session_id"] == "direct-session-1"
        assert response["result"]["deleted_session_ids"] == ["direct-session-1"]
        assert db.sessions.get("direct-session-1") is None
        assert db.session_index.get("direct-session-1") is None

        repeated = server._methods["conversation.delete"](
            2,
            {"conversation_session_id": "direct-session-1"},
        )
        assert repeated["result"]["deleted"] is True
        assert repeated["result"]["existed"] is False
        assert repeated["result"]["via"] == "already_absent"
    finally:
        db.close()


def test_conversation_delete_refuses_to_remove_an_active_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gateway = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(gateway, "_get_db", lambda: db)
    _isolate_delete_side_effects(monkeypatch, gateway, tmp_path)
    try:
        db.sessions.create("direct-session-1", source="cli")
        db.runs.upsert(
            run_id="run-1",
            session_id="direct-session-1",
            status="running",
        )

        response = server._methods["conversation.delete"](
            1,
            {"conversation_session_id": "direct-session-1"},
        )

        assert response["error"] == {
            "code": 4023,
            "message": "cannot delete a conversation with an active run",
        }
        assert db.sessions.get("direct-session-1") is not None
    finally:
        db.close()


def test_conversation_delete_uses_authoritative_team_storage_and_cannot_resurrect(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    gateway = team_mission_gateway()
    db = open_cli_session_store(state_path)
    monkeypatch.setattr(gateway, "_get_db", lambda: db)
    _isolate_delete_side_effects(monkeypatch, gateway, tmp_path)
    try:
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Team conversation",
            objective="Delete from the authoritative owner",
            mode="supervised_mission",
            leader_session_id="team-session-1",
            metadata={"conversationTeamSessionId": "team-session-1"},
        )
        index_row = db.session_index.get("team-session-1")
        assert index_row is not None
        assert index_row["conversation_kind"] == "team"

        response = server._methods["conversation.delete"](
            1,
            {"conversation_session_id": "team-session-1"},
        )

        assert response["result"]["deleted"] is True
        assert response["result"]["existed"] is True
        assert response["result"]["conversation_kind"] == "team"
        assert response["result"]["conversation_session_id"] == "team-session-1"
        assert db.resolve_team_mission_conversation("conversation-1") == {}
        assert db.sessions.get("team-session-1") is None
        assert db.session_index.get("team-session-1") is None

        db.session_index.reconcile()
        assert db.session_index.get("team-session-1") is None
    finally:
        db.close()

    reopened = open_cli_session_store(state_path)
    try:
        reopened.session_index.reconcile()
        assert reopened.session_index.get("team-session-1") is None
        assert reopened.resolve_team_mission_conversation("conversation-1") == {}
    finally:
        reopened.close()
