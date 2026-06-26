from __future__ import annotations

import importlib
from pathlib import Path

from hermes_state import SessionDB
from tui_gateway import server


def _setup(monkeypatch, tmp_path: Path) -> SessionDB:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    monkeypatch.setattr(session_methods, "_SESSION_INDEX_RECONCILED", False)
    return db


def _session_list() -> list[dict]:
    response = server._methods["session.list"](1, {})
    assert "error" not in response
    return response["result"]["sessions"]


def _session_index_list() -> list[dict]:
    response = server._methods["session.index.list"](1, {})
    assert "error" not in response
    return response["result"]["sessions"]


def _create_team_sidebar_session(
    db: SessionDB,
    *,
    conversation_id: str = "conversation-1",
    stable_session_id: str = "team-session-1",
    team_id: str = "team-1",
    title: str = "Team Conversation",
    active_mission_id: str = "mission-1",
) -> None:
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=stable_session_id,
        team_id=team_id,
        title=title,
        objective="Team objective",
        active_mission_id=active_mission_id,
        created_at=100,
        updated_at=200,
    )
    db.create_session(stable_session_id, source="team_mission", transient=False)
    db.append_message(stable_session_id, role="user", content="hello team")


def test_session_list_returns_team_metadata_for_team_session(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(
        db,
        title="Backend Team Title",
        active_mission_id="mission-active",
    )

    [item] = _session_list()

    assert item["id"] == "team-session-1"
    assert item["session_kind"] == "team_mission"
    assert item["conversation_id"] == "conversation-1"
    assert item["team_id"] == "team-1"
    assert item["team_conversation_title"] == "Backend Team Title"
    assert item["mission_status"] == "active"
    assert item["running"] is True


def test_session_list_returns_null_team_fields_for_direct_session(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    db.create_session("direct-session-1", source="cli", transient=False)
    db.append_message("direct-session-1", role="user", content="hello")

    [item] = _session_list()

    assert item["id"] == "direct-session-1"
    assert item["team_id"] == ""
    assert item["team_conversation_title"] == ""
    assert item["mission_status"] == ""
    assert item["active_mission_id"] == ""
    assert item["running"] is False


def test_session_list_running_reflects_any_active_mission(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(db, active_mission_id="")
    db.add_mission_to_conversation(
        conversation_id="conversation-1",
        mission_id="mission-latest-active",
    )

    [item] = _session_list()

    assert db.has_active_mission("conversation-1") is True
    assert item["running"] is True
    assert item["active_mission_id"] == "mission-latest-active"
    assert item["mission_status"] == "active"


def test_session_list_includes_active_mission_id_when_set(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(db, active_mission_id="mission-explicit")

    [item] = _session_list()

    assert item["active_mission_id"] == "mission-explicit"
    assert item["mission_id"] == "mission-explicit"


def test_session_index_list_also_enriched_for_consistency(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(
        db,
        title="Indexed Team Title",
        active_mission_id="mission-index",
    )
    db.upsert_session_index(
        session_id="direct-session-1",
        title="Direct",
        preview="hello",
        source="cli",
        started_at=300,
        updated_at=300,
    )

    by_id = {item["id"]: item for item in _session_index_list()}
    team_item = by_id["team-session-1"]
    direct_item = by_id["direct-session-1"]

    assert team_item["team_id"] == "team-1"
    assert team_item["team_conversation_title"] == "Indexed Team Title"
    assert team_item["active_mission_id"] == "mission-index"
    assert team_item["mission_status"] == "active"
    assert team_item["running"] is True
    assert direct_item["team_id"] == ""
    assert direct_item["team_conversation_title"] == ""
    assert direct_item["mission_status"] == ""
