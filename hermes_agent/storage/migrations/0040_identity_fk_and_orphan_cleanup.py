"""Add identity foreign keys and remove orphan identity rows."""

from __future__ import annotations

import logging
import sqlite3

version = 40
description = "identity FK completeness + orphan cleanup"

_logger = logging.getLogger(__name__)

FK_TABLES = [
    ("runs", "session_id", "sessions", "id", "CASCADE"),
    ("run_events", "session_id", "sessions", "id", "CASCADE"),
    ("run_event_search_index", "run_event_id", "run_events", "id", "CASCADE"),
    ("messages", "session_id", "sessions", "id", "CASCADE"),
    ("session_runtime_state", "session_id", "sessions", "id", "CASCADE"),
    ("conversation_participants", "conversation_session_id", "sessions", "id", "CASCADE"),
]


def apply(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA foreign_keys = OFF")
    cursor.execute("BEGIN IMMEDIATE")
    try:
        _apply_identity_fk_cleanup(cursor)
    except Exception:
        cursor.execute("ROLLBACK")
        cursor.execute("PRAGMA foreign_keys = ON")
        raise
    cursor.execute("COMMIT")
    cursor.execute("PRAGMA foreign_keys = ON")


def _apply_identity_fk_cleanup(cursor: sqlite3.Cursor) -> None:

    for table, col, parent, parent_col, _on_delete in FK_TABLES:
        orphan_count = _orphan_count(cursor, table, col, parent, parent_col)
        if orphan_count > 0:
            _delete_orphans(cursor, table, col, parent, parent_col)
            _logger.warning("removed %d orphan rows from %s", orphan_count, table)

    _migrate_runs(cursor)
    _migrate_run_events(cursor)
    _migrate_messages(cursor)
    _migrate_session_runtime_state(cursor)
    _migrate_conversation_participants(cursor)

    violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"FK violations after migration: {violations}")


def _orphan_count(
    cursor: sqlite3.Cursor,
    table: str,
    col: str,
    parent: str,
    parent_col: str,
) -> int:
    row = cursor.execute(
        f"""
        SELECT COUNT(*)
          FROM {table}
         WHERE {col} IS NOT NULL
           AND {col} != ''
           AND {col} NOT IN (SELECT {parent_col} FROM {parent})
        """
    ).fetchone()
    return int(row[0]) if row else 0


def _delete_orphans(
    cursor: sqlite3.Cursor,
    table: str,
    col: str,
    parent: str,
    parent_col: str,
) -> None:
    cursor.execute(
        f"""
        DELETE FROM {table}
         WHERE {col} IS NOT NULL
           AND {col} != ''
           AND {col} NOT IN (SELECT {parent_col} FROM {parent})
        """
    )


def _execute_script_in_current_transaction(cursor: sqlite3.Cursor, sql: str) -> None:
    for statement in sql.split(";"):
        normalized = statement.strip()
        if normalized:
            cursor.execute(normalized)


def _rebuild_table(
    cursor: sqlite3.Cursor,
    *,
    table: str,
    ddl: str,
    copy_columns: tuple[str, ...],
    indexes_sql: str,
    triggers_sql: str = "",
) -> None:
    cursor.execute(f"DROP TABLE IF EXISTS {table}_new")
    _execute_script_in_current_transaction(cursor, ddl)
    column_sql = ", ".join(f'"{column}"' for column in copy_columns)
    cursor.execute(f"INSERT INTO {table}_new ({column_sql}) SELECT {column_sql} FROM {table}")
    cursor.execute(f"DROP TABLE {table}")
    cursor.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    if indexes_sql:
        _execute_script_in_current_transaction(cursor, indexes_sql)
    if triggers_sql:
        _execute_script_in_current_transaction(cursor, triggers_sql)


def _migrate_runs(cursor: sqlite3.Cursor) -> None:
    _rebuild_table(
        cursor,
        table="runs",
        ddl="""
        CREATE TABLE IF NOT EXISTS runs_new (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            runtime_scope_key TEXT,
            turn_id TEXT,
            execution_session_id TEXT,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            last_seq INTEGER DEFAULT 0,
            error TEXT,
            metadata_json TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """,
        copy_columns=(
            "run_id",
            "session_id",
            "runtime_scope_key",
            "turn_id",
            "execution_session_id",
            "status",
            "started_at",
            "updated_at",
            "completed_at",
            "last_seq",
            "error",
            "metadata_json",
        ),
        indexes_sql="""
        CREATE INDEX IF NOT EXISTS idx_runs_session_status
            ON runs(session_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_scope_status
            ON runs(runtime_scope_key, status, updated_at DESC);
        """,
    )


def _migrate_run_events(cursor: sqlite3.Cursor) -> None:
    _rebuild_table(
        cursor,
        table="run_events",
        ddl="""
        CREATE TABLE IF NOT EXISTS run_events_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            turn_id TEXT,
            execution_session_id TEXT,
            runtime_scope_key TEXT,
            participant_id TEXT NOT NULL DEFAULT '',
            activity_id TEXT,
            event_type TEXT NOT NULL,
            seq INTEGER NOT NULL,
            timestamp REAL NOT NULL,
            payload_json TEXT,
            event_json TEXT NOT NULL,
            status TEXT,
            frame_blob BLOB,
            frame_format TEXT,
            retention_class TEXT,
            projected_message_id TEXT,
            projected_tool_event_id TEXT,
            projection_state TEXT,
            runtime_source_seq INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session_id, seq),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """,
        copy_columns=(
            "id",
            "session_id",
            "run_id",
            "turn_id",
            "execution_session_id",
            "runtime_scope_key",
            "participant_id",
            "activity_id",
            "event_type",
            "seq",
            "timestamp",
            "payload_json",
            "event_json",
            "status",
            "frame_blob",
            "frame_format",
            "retention_class",
            "projected_message_id",
            "projected_tool_event_id",
            "projection_state",
            "runtime_source_seq",
        ),
        indexes_sql="""
        CREATE INDEX IF NOT EXISTS idx_run_events_activity_seq
            ON run_events(activity_id, seq)
            WHERE activity_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_run_events_session_seq
            ON run_events(session_id, seq);
        CREATE INDEX IF NOT EXISTS idx_run_events_scope_seq
            ON run_events(runtime_scope_key, session_id, seq);
        CREATE INDEX IF NOT EXISTS idx_run_events_run
            ON run_events(run_id, id);
        CREATE INDEX IF NOT EXISTS idx_run_events_participant
            ON run_events(participant_id);
        CREATE INDEX IF NOT EXISTS idx_run_events_retention_class
            ON run_events(retention_class, timestamp);
        CREATE INDEX IF NOT EXISTS idx_run_events_projection_state
            ON run_events(projection_state, session_id, seq);
        CREATE INDEX IF NOT EXISTS idx_run_events_runtime_source_seq
            ON run_events(session_id, runtime_source_seq, event_type);
        """,
    )


def _migrate_messages(cursor: sqlite3.Cursor) -> None:
    _rebuild_table(
        cursor,
        table="messages",
        ddl="""
        CREATE TABLE IF NOT EXISTS messages_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT,
            participant_id TEXT NOT NULL DEFAULT '',
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            codex_message_items TEXT,
            platform_message_id TEXT,
            conversation_message_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """,
        copy_columns=(
            "id",
            "session_id",
            "role",
            "content",
            "participant_id",
            "tool_call_id",
            "tool_calls",
            "tool_name",
            "timestamp",
            "token_count",
            "finish_reason",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "codex_reasoning_items",
            "codex_message_items",
            "platform_message_id",
            "conversation_message_id",
            "metadata_json",
            "active",
        ),
        indexes_sql="""
        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_session_active
            ON messages(session_id, active, timestamp);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conversation_message_id
            ON messages(session_id, conversation_message_id)
            WHERE conversation_message_id != '';
        CREATE INDEX IF NOT EXISTS idx_messages_platform_msg_id
            ON messages(session_id, platform_message_id)
            WHERE platform_message_id IS NOT NULL;
        """,
    )


def _migrate_session_runtime_state(cursor: sqlite3.Cursor) -> None:
    _rebuild_table(
        cursor,
        table="session_runtime_state",
        ddl="""
        CREATE TABLE IF NOT EXISTS session_runtime_state_new (
            session_id TEXT PRIMARY KEY,
            runtime_scope_key TEXT,
            execution_session_id TEXT,
            run_id TEXT,
            turn_id TEXT,
            status TEXT,
            model TEXT,
            provider TEXT,
            profile_json TEXT,
            payload_hash TEXT,
            updated_at REAL NOT NULL,
            source_seq INTEGER,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """,
        copy_columns=(
            "session_id",
            "runtime_scope_key",
            "execution_session_id",
            "run_id",
            "turn_id",
            "status",
            "model",
            "provider",
            "profile_json",
            "payload_hash",
            "updated_at",
            "source_seq",
        ),
        indexes_sql="""
        CREATE INDEX IF NOT EXISTS idx_session_runtime_state_scope
            ON session_runtime_state(runtime_scope_key, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_session_runtime_state_status
            ON session_runtime_state(status, updated_at DESC);
        """,
    )


def _migrate_conversation_participants(cursor: sqlite3.Cursor) -> None:
    _rebuild_table(
        cursor,
        table="conversation_participants",
        ddl="""
        CREATE TABLE IF NOT EXISTS conversation_participants_new (
            conversation_session_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            member_id TEXT NOT NULL DEFAULT '',
            agent_profile_id TEXT NOT NULL DEFAULT '',
            agent_profile_version_id TEXT NOT NULL DEFAULT '',
            runtime_scope_key TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            avatar TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (conversation_session_id, participant_id),
            FOREIGN KEY (conversation_session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """,
        copy_columns=(
            "conversation_session_id",
            "participant_id",
            "role",
            "member_id",
            "agent_profile_id",
            "agent_profile_version_id",
            "runtime_scope_key",
            "display_name",
            "avatar",
            "metadata_json",
            "created_at",
            "updated_at",
        ),
        indexes_sql="""
        CREATE INDEX IF NOT EXISTS idx_conversation_participants_runtime_scope
            ON conversation_participants(runtime_scope_key);
        """,
    )
