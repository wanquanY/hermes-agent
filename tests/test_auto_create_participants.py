from __future__ import annotations

import importlib
from pathlib import Path

from hermes_state import SessionDB
from hermes_team_mission.gateway import runtime_methods
from tests.team_mission_gateway_test_support import team_mission_gateway


def _db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def _workspace(tmp_path: Path) -> dict:
    path = tmp_path / "workspace"
    path.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(path)}


def _seed_team(db: SessionDB, tmp_path: Path) -> None:
    db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader",
        avatar="avatar://leader",
        description="Plans work.",
        category="team",
        tags=["planning"],
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "leader-home"),
        current_version_id="version-leader",
        current_version_number=1,
    )
    db.upsert_agent_profile(
        profile_id="profile-builder",
        slug="builder",
        name="Builder",
        avatar="avatar://builder",
        description="Builds work.",
        category="team",
        tags=["build"],
        hermes_profile_name="builder",
        hermes_home_path=str(tmp_path / "builder-home"),
        current_version_id="version-builder",
        current_version_number=1,
    )
    db.upsert_agent_team(
        team_id="team-1",
        name="Team",
        description="Test team.",
        lead_agent_profile_id="profile-leader",
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        profile_name="Leader",
        profile_avatar="avatar://leader",
    )
    db.upsert_agent_team_member(
        member_id="member-builder",
        team_id="team-1",
        agent_profile_id="profile-builder",
        agent_profile_version_id="version-builder",
        role="builder",
        profile_name="Builder",
        profile_avatar="avatar://builder",
    )


def _participants_by_id(db: SessionDB, session_id: str) -> dict[str, dict]:
    return {
        row["participant_id"]: row
        for row in db.list_conversation_participants(session_id)
    }


def test_session_create_inserts_user_and_agent_participants(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server

    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = _db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-test")
    monkeypatch.setattr(
        session_methods,
        "_bind_session_workspace",
        lambda **kwargs: kwargs.get("workspace") or {},
    )

    response = server.handle_request({
        "id": "r1",
        "method": "session.create",
        "params": {
            "control_plane_only": True,
            "agent_profile_id": "profile-1",
            "agent_profile_name": "Agent One",
            "agent_profile_avatar": "avatar://agent",
            "runtime_scope_key": "profile:profile-1",
            "cwd": str(tmp_path),
            "workspace": {"id": "ws-1", "path": str(tmp_path), "kind": "local"},
        },
    })

    assert "error" not in response
    session_id = response["result"]["conversation_session_id"]
    participants = _participants_by_id(db, session_id)
    assert set(participants) == {"user", "agent:profile-1"}
    assert participants["user"]["role"] == "user"
    assert participants["agent:profile-1"]["role"] == "agent"
    assert participants["agent:profile-1"]["display_name"] == "Agent One"
    assert participants["agent:profile-1"]["avatar"] == "avatar://agent"


def test_team_mission_create_inserts_user_leader_and_members(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = _db(tmp_path)
    _seed_team(db, tmp_path)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": "mission-1",
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "title": "Team work",
            "objective": "Ship it",
            "mode": "supervised_mission",
            "workspace": _workspace(tmp_path),
            "metadata": {"start_leader": False},
        },
    )

    assert "error" not in response
    participants = _participants_by_id(db, "team-session-1")
    assert {"user", "leader:team-1", "member:member-builder"} <= set(participants)
    assert participants["leader:team-1"]["role"] == "leader"
    assert participants["leader:team-1"]["agent_profile_id"] == "profile-leader"
    assert participants["leader:team-1"]["display_name"] == "Leader"
    assert participants["member:member-builder"]["role"] == "member"
    assert participants["member:member-builder"]["display_name"] == "Builder"
    assert participants["member:member-builder"]["avatar"] == "avatar://builder"


def test_member_chat_start_ensures_member_participant_idempotent(monkeypatch, tmp_path: Path) -> None:
    db = _db(tmp_path)
    captured: dict = {}

    def fake_proxy_run_submit(params: dict) -> dict:
        captured.update(params)
        return {"ok": True}

    monkeypatch.setattr(runtime_methods, "_proxy_run_submit_via_worker", fake_proxy_run_submit)
    mission = {
        "mission_id": "mission-1",
        "team_id": "team-1",
        "workspace_path": str(tmp_path),
        "metadata": {
            "members": [
                {
                    "member_id": "member-builder",
                    "agent_profile_id": "profile-builder",
                    "agent_profile_version_id": "version-builder",
                    "role": "builder",
                    "profile_name": "Builder",
                    "profile_avatar": "avatar://builder",
                    "dovie_profile": {
                        "hermesHomePath": str(tmp_path / "builder-home"),
                    },
                }
            ]
        },
    }
    params = {
        "team_id": "team-1",
        "conversation_id": "conversation-1",
        "conversation_session_id": "team-session-1",
        "client_run_id": "run-1",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "workspace": {"id": "workspace-1", "path": str(tmp_path), "kind": "local"},
    }

    first = runtime_methods._submit_message_to_member(
        "rid-1",
        params,
        db=db,
        target_member_id="member-builder",
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
        mission=mission,
        text="@Builder check this",
    )
    second = runtime_methods._submit_message_to_member(
        "rid-2",
        {**params, "client_run_id": "run-2", "turn_id": "turn-2"},
        db=db,
        target_member_id="member-builder",
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
        mission=mission,
        text="@Builder check again",
    )

    assert "error" not in first
    assert "error" not in second
    assert captured["conversation_session_id"] == "team-session-1"
    rows = [
        row
        for row in db.list_conversation_participants("team-session-1")
        if row["participant_id"] == "member:member-builder"
    ]
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Builder"
    assert rows[0]["avatar"] == "avatar://builder"


def test_render_snapshot_includes_participants_list(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = _db(tmp_path)
    db.create_session("conv-1", source="tui")
    db.ensure_user_participant("conv-1")
    db.ensure_agent_participant("conv-1", agent_profile_id="profile-1", display_name="Agent")
    db.append_message("conv-1", role="user", content="hello")
    monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)

    response = server._methods["conversation.render_snapshot"](
        1,
        {"session_id": "conv-1"},
    )

    assert "error" not in response
    assert [row["participant_id"] for row in response["result"]["participants"]] == [
        "user",
        "agent:profile-1",
    ]


def test_one_shot_migration_backfills_existing_conversations(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_team(db, tmp_path)
    db.create_session("legacy-direct", source="tui")
    db.create_session("team-session-1", source="team_mission")
    db.upsert_session_index(
        session_id="legacy-direct",
        owner_agent_profile_id="profile-builder",
        owner_profile_version_id="version-builder",
        source="tui",
    )
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
        team_id="team-1",
        title="Team",
    )
    db._conn.execute("DELETE FROM conversation_participants")  # noqa: SLF001

    result = db.reconcile_conversation_participants_one_shot()

    assert result["ran"] is True
    assert result["inserted"] >= 5
    direct = _participants_by_id(db, "legacy-direct")
    team = _participants_by_id(db, "team-session-1")
    assert {"user", "agent:profile-builder"} <= set(direct)
    assert {"user", "leader:team-1", "member:member-builder"} <= set(team)
    assert team["leader:team-1"]["runtime_scope_key"] == "team:team-1:leader-conversation"


def test_one_shot_migration_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.create_session("legacy-direct", source="tui")
    db._conn.execute("DELETE FROM conversation_participants")  # noqa: SLF001

    first = db.reconcile_conversation_participants_one_shot()
    before = db.list_conversation_participants("legacy-direct")
    second = db.reconcile_conversation_participants_one_shot()
    after = db.list_conversation_participants("legacy-direct")

    assert first["inserted"] == 2
    assert second["ran"] is False
    assert second["inserted"] == 0
    assert before == after


def test_participant_create_failure_does_not_block_conversation_create(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server

    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = _db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-test")
    monkeypatch.setattr(
        session_methods,
        "_bind_session_workspace",
        lambda **kwargs: kwargs.get("workspace") or {},
    )

    def fail_user_participant(*_args, **_kwargs):
        raise RuntimeError("participant write failed")

    monkeypatch.setattr(db, "ensure_user_participant", fail_user_participant)

    response = server.handle_request({
        "id": "r1",
        "method": "session.create",
        "params": {
            "control_plane_only": True,
            "agent_profile_id": "profile-1",
            "cwd": str(tmp_path),
            "workspace": {"id": "ws-1", "path": str(tmp_path), "kind": "local"},
        },
    })

    assert "error" not in response
    assert db.get_session(response["result"]["conversation_session_id"]) is not None
