from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


def _install_db(monkeypatch: Any, tmp_path: Path) -> CliSessionStore:
    conversation_render_snapshot = importlib.import_module(
        "tui_gateway.methods.conversation_render_snapshot"
    )
    session_history = importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
    monkeypatch.setattr(session_history, "_get_db", lambda: db)
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    return db


def _seed_zero_mission_team_conversation(db: CliSessionStore) -> None:
    db.sessions.create(session_id="team-session-zero", source="team_mission")
    db.messages.append("team-session-zero", role="user", content="team conversation stays open")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-zero",
        conversation_session_id="team-session-zero",
        team_id="team-1",
        title="Zero mission team",
        active_mission_id="",
        created_at=1,
        updated_at=2,
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id="team-session-zero",
        participant_id="user",
        role="user",
        display_name="User",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id="team-session-zero",
        participant_id="leader:conversation-zero",
        role="leader",
        display_name="Leader",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id="team-session-zero",
        participant_id="member:builder",
        role="member",
        member_id="builder",
        display_name="Builder",
    )


def _seed_active_mission_team_conversation(db: CliSessionStore) -> None:
    db.sessions.create(session_id="team-session-active", source="team_mission")
    db.messages.append("team-session-active", role="assistant", content="active team render")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-active",
        conversation_session_id="team-session-active",
        team_id="team-1",
        title="Active mission team",
        active_mission_id="mission-active",
        created_at=3,
        updated_at=4,
    )
    db.upsert_team_mission(
        mission_id="mission-active",
        conversation_id="conversation-active",
        team_id="team-1",
        title="Active mission",
        objective="Keep active mission path intact.",
        status="running",
        leader_session_id="team-session-active",
    )


def _seed_direct_conversation_with_mission_id(db: CliSessionStore) -> None:
    db.sessions.create(session_id="direct-session", source="tui")
    db.messages.append("direct-session", role="user", content="direct conversation")
    db.session_index.upsert(
        session_id="direct-session",
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="direct",
        conversation_id="conversation-active",
        mission_id="mission-active",
        title="Direct stays direct",
        started_at=5,
        updated_at=6,
    )


def test_zero_mission_team_conversation_routes_to_team_render(monkeypatch, tmp_path: Path) -> None:
    db = _install_db(monkeypatch, tmp_path)
    _seed_zero_mission_team_conversation(db)

    response = server._methods["conversation.render_snapshot"](
        1,
        {"session_id": "team-session-zero"},
    )

    assert "error" not in response
    assert response["result"]["kind"] == "team_mission"
    assert response["result"]["conversation"]["conversation_id"] == "conversation-zero"
    assert response["result"]["mission"] == {}
    assert response["result"]["missionPresent"] is False


def test_direct_conversation_routes_to_ordinary_render_regardless_of_active_mission(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    _seed_active_mission_team_conversation(db)
    _seed_direct_conversation_with_mission_id(db)

    response = server._methods["conversation.render_snapshot"](
        1,
        {"session_id": "direct-session"},
    )

    assert "error" not in response
    assert response["result"]["kind"] == "ordinary"
    assert response["result"]["session_id"] == "direct-session"
    assert response["result"]["messages"][0]["text"] == "direct conversation"


def test_team_conversation_with_active_mission_routes_to_team_render(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    _seed_active_mission_team_conversation(db)

    response = server._methods["conversation.render_snapshot"](
        1,
        {"session_id": "team-session-active"},
    )

    assert "error" not in response
    assert response["result"]["kind"] == "team_mission"
    assert response["result"]["conversation"]["conversation_id"] == "conversation-active"
    assert response["result"]["mission"]["mission_id"] == "mission-active"
    assert response["result"]["missionPresent"] is True


def test_team_render_returns_valid_snapshot_when_mission_empty(monkeypatch, tmp_path: Path) -> None:
    db = _install_db(monkeypatch, tmp_path)
    _seed_zero_mission_team_conversation(db)

    response = server._methods["team_mission.conversation.render"](
        1,
        {"conversation_id": "conversation-zero"},
    )

    assert "error" not in response
    result = response["result"]
    assert result["renderReady"] is True
    assert result["kind"] == "team_mission"
    assert result["mission"] == {}
    assert result["mission_present"] is False
    assert result["messages"][0]["text"] == "team conversation stays open"
    assert isinstance(result["graph"], dict)


def test_team_render_includes_participants_when_mission_empty(monkeypatch, tmp_path: Path) -> None:
    db = _install_db(monkeypatch, tmp_path)
    _seed_zero_mission_team_conversation(db)

    response = server._methods["conversation.render_snapshot"](
        1,
        {"session_id": "team-session-zero"},
    )

    assert "error" not in response
    assert [row["participant_id"] for row in response["result"]["participants"]] == [
        "user",
        "leader:conversation-zero",
        "member:builder",
    ]


def test_routing_uses_conversation_kind_not_active_mission_id(monkeypatch, tmp_path: Path) -> None:
    db = _install_db(monkeypatch, tmp_path)
    _seed_active_mission_team_conversation(db)
    _seed_direct_conversation_with_mission_id(db)

    response = server._methods["conversation.render_snapshot"](
        1,
        {
            "session_id": "direct-session",
            "conversation_kind": "direct",
            "active_mission_id": "mission-active",
        },
    )

    assert "error" not in response
    assert response["result"]["kind"] == "ordinary"
    assert response["result"]["projection"]["source"] == "conversation.render_snapshot"
