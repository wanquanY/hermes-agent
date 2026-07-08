from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from hermes_state import SessionDB
from tui_gateway import server


def _setup_gateway_db(monkeypatch, tmp_path: Path) -> SessionDB:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    monkeypatch.setattr(session_methods, "_SESSION_INDEX_RECONCILED", False)
    return db


def _column_info(db: SessionDB, column: str) -> dict:
    row = next(
        item
        for item in db._conn.execute("PRAGMA table_info(session_index)").fetchall()
        if item["name"] == column
    )
    return dict(row)


def test_session_index_has_conversation_kind_column(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")

    info = _column_info(db, "conversation_kind")

    assert info["type"] == "TEXT"
    assert info["notnull"] == 1
    assert str(info["dflt_value"]).strip("'\"") == "direct"


def test_legacy_session_index_rows_migrate_conversation_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE session_index (
                session_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'unknown',
                session_kind TEXT NOT NULL DEFAULT 'hermes_session'
            )
            """
        )
        conn.execute(
            "INSERT INTO session_index (session_id, source, session_kind) VALUES (?, ?, ?)",
            ("team-session", "team_mission", "team_mission"),
        )
        conn.execute(
            "INSERT INTO session_index (session_id, source, session_kind) VALUES (?, ?, ?)",
            ("direct-session", "cli", "hermes_session"),
        )
        conn.commit()
    finally:
        conn.close()

    db = SessionDB(db_path)
    rows = {
        row["session_id"]: row["conversation_kind"]
        for row in db._conn.execute(
            "SELECT session_id, conversation_kind FROM session_index"
        ).fetchall()
    }

    assert rows == {
        "team-session": "team",
        "direct-session": "direct",
    }


def test_session_index_upsert_populates_conversation_kind(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")

    db.upsert_session_index(session_id="direct-session", source="cli", title="Direct")
    db.upsert_session_index(
        session_id="team-session",
        source="team_mission",
        session_kind="team_mission",
        title="Team",
    )

    rows = {item["session_id"]: item for item in db.list_session_index()["sessions"]}
    assert rows["direct-session"]["conversation_kind"] == "direct"
    assert rows["team-session"]["conversation_kind"] == "team"


def test_session_create_projection_sets_direct_conversation_kind(tmp_path: Path) -> None:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = SessionDB(tmp_path / "state.db")

    session_methods._project_session_index_on_create(
        db,
        "direct-session",
        {"agentProfileId": "agent-1"},
        "profile:agent-1",
        False,
    )

    [item] = db.list_session_index()["sessions"]
    assert item["session_id"] == "direct-session"
    assert item["conversation_kind"] == "direct"


def test_team_conversation_projection_sets_team_conversation_kind(
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")

    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        conversation_session_id="team-session",
        team_id="team-1",
        title="Team",
        active_mission_id="mission-1",
        created_at=10,
        updated_at=20,
    )

    [item] = db.list_session_index()["sessions"]
    assert item["session_id"] == "team-session"
    assert item["conversation_kind"] == "team"


def test_gateway_session_lists_emit_conversation_kind(monkeypatch, tmp_path: Path) -> None:
    db = _setup_gateway_db(monkeypatch, tmp_path)
    db.create_session("direct-session", source="cli", transient=False)
    db.append_message("direct-session", role="user", content="hello")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        conversation_session_id="team-session",
        team_id="team-1",
        title="Team",
        active_mission_id="mission-1",
        created_at=10,
        updated_at=20,
    )
    db.create_session("team-session", source="team_mission", transient=False)
    db.append_message("team-session", role="user", content="hello team")

    session_list = server._methods["session.list"](1, {})
    assert "error" not in session_list
    rich_rows = {item["id"]: item for item in session_list["result"]["sessions"]}
    assert rich_rows["direct-session"]["conversation_kind"] == "direct"
    assert rich_rows["team-session"]["conversation_kind"] == "team"

    index_list = server._methods["session.index.list"](2, {})
    assert "error" not in index_list
    index_rows = {item["id"]: item for item in index_list["result"]["sessions"]}
    assert index_rows["direct-session"]["conversation_kind"] == "direct"
    assert index_rows["team-session"]["conversation_kind"] == "team"


def test_gateway_session_index_list_uses_read_model_not_sessiondb_method(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(
        session_id="direct-session",
        source="cli",
        title="Direct",
        conversation_kind="direct",
        started_at=10,
        updated_at=20,
    )

    class _ReadOnlyGatewayDB:
        def __init__(self, source: SessionDB) -> None:
            self._conn = source._conn

        def reconcile_session_index(self) -> None:
            return None

    monkeypatch.setattr(session_methods, "_get_db", lambda: _ReadOnlyGatewayDB(db))
    monkeypatch.setattr(session_methods, "_SESSION_INDEX_RECONCILED", False)

    response = server._methods["session.index.list"](1, {})

    assert "error" not in response
    assert [item["id"] for item in response["result"]["sessions"]] == ["direct-session"]
