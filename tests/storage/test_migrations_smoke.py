from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_agent.storage.migrations import CURRENT_SCHEMA_VERSION


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type IN ('table', 'virtual table')
            """
        ).fetchall()
        return {str(row[0]) for row in rows}
    finally:
        conn.close()


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT version FROM schema_version").fetchone()[0])
    finally:
        conn.close()


def test_empty_database_init_applies_all_migrations(tmp_path: Path):
    db_path = tmp_path / "state.db"

    db = open_cli_session_store(db_path)
    db.close()

    tables = _table_names(db_path)
    assert {
        "sessions",
        "messages",
        "messages_fts",
        "messages_fts_trigram",
        "run_events",
        "session_runtime_state",
        "tool_events",
        "team_mission_events",
        "v3_activities",
        "activity_commands",
    }.issubset(tables)
    assert _schema_version(db_path) == CURRENT_SCHEMA_VERSION


def test_half_upgraded_database_runs_pending_owner_migrations(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (15)")
        conn.commit()
    finally:
        conn.close()

    db = open_cli_session_store(db_path)
    db.close()

    assert _schema_version(db_path) == CURRENT_SCHEMA_VERSION
    conn = sqlite3.connect(db_path)
    try:
        run_event_columns = {
            row[1] for row in conn.execute('PRAGMA table_info("run_events")').fetchall()
        }
        message_columns = {
            row[1] for row in conn.execute('PRAGMA table_info("messages")').fetchall()
        }
        assert {"participant_id", "activity_id", "frame_blob"}.issubset(run_event_columns)
        assert "participant_id" in message_columns
        assert {
            "session_system_prompts",
            "session_runtime_state",
            "tool_events",
            "activity_commands",
        }.issubset(_table_names(db_path))
    finally:
        conn.close()
