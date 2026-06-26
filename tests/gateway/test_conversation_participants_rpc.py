from __future__ import annotations

from pathlib import Path

from hermes_state import SessionDB
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


def _seed_participants(db: SessionDB) -> None:
    db.create_session("team-session-1", source="team_mission", transient=False)
    db.upsert_conversation_participant(
        conversation_session_id="team-session-1",
        participant_id="leader:conv-1",
        role="leader",
        member_id="m-lead",
        agent_profile_id="p-lead",
        runtime_scope_key="team:conv-1:leader-conversation",
        display_name="Lead",
    )
    db.upsert_conversation_participant(
        conversation_session_id="team-session-1",
        participant_id="member:m-alice",
        role="member",
        member_id="m-alice",
        agent_profile_id="p-alice",
        runtime_scope_key="member-chat:conv-1:m-alice",
        display_name="Alice",
    )


def test_conversation_participants_rpc_lists_roster(tmp_path: Path, monkeypatch):
    gateway = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_participants(db)
    monkeypatch.setattr(gateway, "_get_db", lambda: db)

    response = server._methods["team_mission.conversation.participants"](
        1,
        {"conversation_session_id": "team-session-1"},
    )

    assert "error" not in response
    assert response["result"]["conversation_session_id"] == "team-session-1"
    assert response["result"]["conversationSessionId"] == "team-session-1"
    participants = response["result"]["participants"]
    by_id = {participant["participant_id"]: participant for participant in participants}
    assert list(by_id) == ["leader:conv-1", "member:m-alice"]
    assert by_id["leader:conv-1"]["display_name"] == "Lead"
    assert by_id["member:m-alice"]["runtime_scope_key"] == "member-chat:conv-1:m-alice"


def test_conversation_participants_rpc_accepts_session_alias(tmp_path: Path, monkeypatch):
    gateway = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_participants(db)
    monkeypatch.setattr(gateway, "_get_db", lambda: db)

    response = server._methods["team_mission.conversation.participants"](
        2,
        {"conversationSessionId": "team-session-1"},
    )

    assert "error" not in response
    assert response["result"]["participants"][0]["participant_id"] == "leader:conv-1"


def test_conversation_participants_rpc_requires_conversation_session_id(monkeypatch):
    gateway = team_mission_gateway()
    monkeypatch.setattr(gateway, "_get_db", lambda: None)

    response = server._methods["team_mission.conversation.participants"](3, {})

    assert response["error"]["code"] == 4006
    assert response["error"]["message"] == "conversation_session_id required"
