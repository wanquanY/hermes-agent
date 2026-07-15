from __future__ import annotations

import importlib
from pathlib import Path

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.gateway import runtime_methods
from tui_gateway import server


def _participant_ids(db: CliSessionStore, session_id: str) -> set[str]:
    return {p["participant_id"] for p in db.participants.list_conversation_participants(session_id)}


def _seed_team(db: CliSessionStore) -> None:
    db.teams.upsert_agent_team(team_id="team-1", name="Team", description="")
    db.teams.upsert_agent_team_member(
        member_id="m-lead",
        team_id="team-1",
        agent_profile_id="p-lead",
        role="lead",
        profile_name="Lead",
    )
    db.teams.upsert_agent_team_member(
        member_id="m-alice",
        team_id="team-1",
        agent_profile_id="p-alice",
        role="member",
        profile_name="Alice",
    )


def test_team_conversation_create_upserts_leader_and_members(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    _seed_team(db)

    db.ensure_team_mission_conversation(
        conversation_id="conv-1",
        conversation_session_id="team-session-1",
        team_id="team-1",
        title="Team Conversation",
    )

    participants = db.participants.list_conversation_participants("team-session-1")
    by_id = {p["participant_id"]: p for p in participants}
    assert set(by_id) == {"leader:conv-1", "member:m-alice"}
    assert by_id["leader:conv-1"]["role"] == "leader"
    assert by_id["leader:conv-1"]["member_id"] == "m-lead"
    assert by_id["leader:conv-1"]["runtime_scope_key"] == "team:conv-1:leader-conversation"
    assert by_id["member:m-alice"]["role"] == "member"
    assert by_id["member:m-alice"]["member_id"] == "m-alice"
    assert by_id["member:m-alice"]["agent_profile_id"] == "p-alice"


def test_member_submit_upserts_mentioned_member_idempotently(tmp_path: Path, monkeypatch):
    db = open_cli_session_store(tmp_path / "state.db")
    _seed_team(db)
    db.ensure_team_mission_conversation(
        conversation_id="conv-1",
        conversation_session_id="team-session-1",
        team_id="team-1",
        title="Team Conversation",
    )
    before = db.participants.list_conversation_participants("team-session-1")

    monkeypatch.setattr(runtime_methods, "_proxy_run_submit_via_worker", lambda _params: {"ok": True})
    mission = {
        "mission_id": "mission-1",
        "team_id": "team-1",
        "workspace_path": str(tmp_path),
        "metadata": {
            "members": [
                {
                    "member_id": "m-lead",
                    "agent_profile_id": "p-lead",
                    "role": "lead",
                    "profile_name": "Lead",
                    "dovie_profile": {"id": "p-lead", "hermesHomePath": str(tmp_path / "lead-home")},
                },
                {
                    "member_id": "m-alice",
                    "agent_profile_id": "p-alice",
                    "role": "member",
                    "profile_name": "Alice",
                    "dovie_profile": {"id": "p-alice", "hermesHomePath": str(tmp_path / "alice-home")},
                },
            ]
        },
    }
    params = {
        "team_id": "team-1",
        "conversation_id": "conv-1",
        "conversation_session_id": "team-session-1",
        "cwd": str(tmp_path),
        "workspace": {"id": "ws-1", "path": str(tmp_path), "kind": "local"},
    }

    response = runtime_methods._submit_message_to_member(
        "r1",
        params,
        db=db,
        target_member_id="m-alice",
        conversation_id="conv-1",
        conversation_session_id="team-session-1",
        mission=mission,
        text="@Alice hello",
    )

    assert "error" not in response
    participants = db.participants.list_conversation_participants("team-session-1")
    assert _participant_ids(db, "team-session-1") == {"leader:conv-1", "member:m-alice"}
    assert len(participants) == len(before)
    alice = db.participants.get_conversation_participant("team-session-1", "member:m-alice")
    assert alice["runtime_scope_key"] == "member-chat:conv-1:m-alice"
    assert alice["display_name"] == "Alice"


def test_session_create_upserts_user_and_agent_participants(tmp_path: Path, monkeypatch):
    session_methods = importlib.reload(importlib.import_module("tui_gateway.methods.session"))
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-test")
    monkeypatch.setattr(
        session_methods,
        "_bind_session_workspace",
        lambda **kwargs: kwargs.get("workspace") or {},
    )

    response = server.handle_request(
        {
            "id": "r1",
            "method": "session.create",
            "params": {
                "control_plane_only": True,
                "created_by_user_id": "local-user",
                "agent_profile_id": "profile-1",
                "agent_profile_version_id": "version-1",
                "runtime_scope_key": "profile:profile-1",
                "cwd": str(tmp_path),
                "workspace": {"id": "ws-1", "path": str(tmp_path), "kind": "local"},
            },
        }
    )

    assert "error" not in response
    session_id = response["result"]["conversation_session_id"]
    participants = db.participants.list_conversation_participants(session_id)
    by_id = {p["participant_id"]: p for p in participants}
    assert set(by_id) == {"user", "agent:profile-1"}
    assert by_id["user"]["role"] == "user"
    assert by_id["agent:profile-1"]["role"] == "agent"
    assert by_id["agent:profile-1"]["agent_profile_id"] == "profile-1"
