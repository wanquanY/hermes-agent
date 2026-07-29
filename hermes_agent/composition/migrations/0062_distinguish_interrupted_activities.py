"""Preserve abnormal Activity interruption as a distinct terminal status."""

from __future__ import annotations

import sqlite3


version = 62
description = "distinguish interrupted activities from user cancellation"

_LEGACY_TABLE = "activities_pre_interrupted_status"
_ACTIVITY_COLUMNS = (
    "activity_id",
    "conversation_id",
    "parent_activity_id",
    "kind",
    "target_profile_id",
    "target_team_id",
    "target_mission_id",
    "status",
    "prompt_summary",
    "result_summary",
    "result_json",
    "started_at",
    "completed_at",
    "notify_parent",
    "read_at",
    "created_at",
    "updated_at",
)


def _table_sql(cursor: sqlite3.Cursor, table: str) -> str:
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return str(row[0] or "") if row else ""


def apply(cursor: sqlite3.Cursor) -> None:
    sql = _table_sql(cursor, "activities")
    if not sql or "'interrupted'" in sql:
        return
    if _table_sql(cursor, _LEGACY_TABLE):
        raise RuntimeError(f"stale migration table exists: {_LEGACY_TABLE}")

    live_columns = {
        str(row[1] or "")
        for row in cursor.execute('PRAGMA table_info("activities")').fetchall()
        if str(row[1] or "")
    }
    copy_columns = [
        column for column in _ACTIVITY_COLUMNS if column in live_columns
    ]
    if not copy_columns:
        raise RuntimeError("activities table has no recognized columns")

    cursor.execute("DROP INDEX IF EXISTS idx_activities_conv")
    cursor.execute("DROP INDEX IF EXISTS idx_activities_parent")
    cursor.execute("DROP INDEX IF EXISTS idx_activities_mission")
    cursor.execute(f"ALTER TABLE activities RENAME TO {_LEGACY_TABLE}")
    cursor.execute(
        """
        CREATE TABLE activities (
            activity_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            parent_activity_id TEXT,
            kind TEXT NOT NULL CHECK (
                kind IN (
                    'chat',
                    'agent_dispatch',
                    'team_dispatch',
                    'member_chat',
                    'mission'
                )
            ),
            target_profile_id TEXT,
            target_team_id TEXT,
            target_mission_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN (
                    'pending',
                    'running',
                    'completed',
                    'failed',
                    'cancelled',
                    'interrupted'
                )
            ),
            prompt_summary TEXT,
            result_summary TEXT,
            result_json TEXT,
            started_at REAL,
            completed_at REAL,
            notify_parent INTEGER NOT NULL DEFAULT 1,
            read_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    column_sql = ", ".join(f'"{column}"' for column in copy_columns)
    cursor.execute(
        f"INSERT INTO activities ({column_sql}) "
        f"SELECT {column_sql} FROM {_LEGACY_TABLE}"
    )
    cursor.execute(f"DROP TABLE {_LEGACY_TABLE}")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_activities_conv "
        "ON activities(conversation_id, status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_activities_parent "
        "ON activities(parent_activity_id, status)"
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_mission "
        "ON activities(target_mission_id) "
        "WHERE kind = 'mission' AND COALESCE(target_mission_id, '') != ''"
    )


__all__ = ["apply", "description", "version"]
