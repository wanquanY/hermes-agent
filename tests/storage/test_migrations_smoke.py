from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_state import SCHEMA_VERSION
from hermes_state import SessionDB


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

    db = SessionDB(db_path)
    db.close()

    tables = _table_names(db_path)
    assert {
        "sessions",
        "messages",
        "run_events",
        "session_runtime_state",
        "tool_events",
        "team_mission_events",
        "activity_commands",
    }.issubset(tables)
    assert _schema_version(db_path) == SCHEMA_VERSION


def test_half_upgraded_database_runs_pending_owner_migrations(
    tmp_path: Path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (15)")
        conn.commit()
    finally:
        conn.close()

    calls: list[str] = []

    def _wrap(name: str) -> None:
        original = getattr(SessionDB, name)

        def wrapped(self, cursor):
            calls.append(name)
            return original(self, cursor)

        monkeypatch.setattr(SessionDB, name, wrapped)

    for method_name in (
        "_backfill_session_list_summaries",
        "_migrate_agent_profile_versions_to_latest_profiles",
        "_migrate_run_events_participant_id",
        "_migrate_activities_kind_mission_check",
        "_migrate_messages_participant_id",
        "_migrate_session_system_prompts",
        "_backfill_session_runtime_state",
        "_backfill_tool_events",
        "_backfill_run_event_frame_indexes",
        "_migrate_run_events_activity_id",
        "_migrate_activity_commands",
    ):
        _wrap(method_name)

    db = SessionDB(db_path)
    db.close()

    assert _schema_version(db_path) == SCHEMA_VERSION
    assert calls == [
        "_migrate_activities_kind_mission_check",
        "_backfill_session_list_summaries",
        "_migrate_agent_profile_versions_to_latest_profiles",
        "_migrate_run_events_participant_id",
        "_migrate_activities_kind_mission_check",
        "_migrate_messages_participant_id",
        "_migrate_session_system_prompts",
        "_backfill_session_runtime_state",
        "_backfill_tool_events",
        "_backfill_run_event_frame_indexes",
        "_migrate_run_events_activity_id",
        "_migrate_activity_commands",
    ]
