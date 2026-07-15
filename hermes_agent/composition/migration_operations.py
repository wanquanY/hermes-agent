"""Pure operations used by versioned SQLite storage migrations."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_event_codec import decode_run_event_row
from hermes_agent.domain.run_event_codec import update_run_event_frame_columns
from hermes_agent.domain.run_event_index import project_run_event_search_index
from hermes_agent.domain.run_event_index import runtime_source_seq_from_event
from hermes_agent.domain.session_runtime_state import session_info_record
from hermes_agent.read_models.tool_events import backfill_tool_events_from_run_events
from hermes_agent.repositories.agent_profile_repo import AgentProfileRepoImpl
from hermes_agent.repositories.message_repo import MessageRepository
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl
from hermes_agent.storage.state_schema import SCHEMA_SQL
from hermes_team_mission.runtime.run_event_retention import RunEventRetentionPolicy

logger = logging.getLogger(__name__)
_RETENTION_POLICY = RunEventRetentionPolicy()
_REQUIRED_COLUMN_MIGRATION_TYPES = {
    ("sessions", "source"): "TEXT NOT NULL DEFAULT 'unknown'",
    ("sessions", "started_at"): "REAL NOT NULL DEFAULT 0",
}


def reconcile_declared_columns(cursor: sqlite3.Cursor) -> None:
    expected = parse_schema_columns(SCHEMA_SQL)
    for table_name, declared_columns in expected.items():
        rows = cursor.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        live_columns = {
            str(_row_value(row, "name", 1, "") or "")
            for row in rows
        }
        for column_name, column_type in declared_columns.items():
            if column_name in live_columns:
                continue
            safe_name = column_name.replace('"', '""')
            migration_type = _REQUIRED_COLUMN_MIGRATION_TYPES.get(
                (table_name, column_name),
                column_type,
            )
            try:
                cursor.execute(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{safe_name}" {migration_type}'
                )
            except sqlite3.OperationalError as exc:
                logger.debug(
                    "reconcile %s.%s skipped: %s",
                    table_name,
                    column_name,
                    exc,
                )


def backfill_session_list_summaries(cursor: sqlite3.Cursor) -> None:
    rows = cursor.execute("SELECT id FROM sessions ORDER BY id").fetchall()
    repository = MessageRepository(cursor.connection, SessionRepoImpl(cursor.connection))
    for row in rows:
        session_id = str(_row_value(row, "id", 0, "") or "").strip()
        if session_id:
            repository.rebuild_session_projection(session_id)


def fold_agent_profile_versions(cursor: sqlite3.Cursor) -> None:
    columns = {
        str(_row_value(row, "name", 1, "") or "")
        for row in cursor.execute('PRAGMA table_info("agent_profile_versions")').fetchall()
    }
    legacy_columns = {"id", "agent_profile_id", "published_at"}
    if not legacy_columns.issubset(columns):
        return
    AgentProfileRepoImpl(cursor.connection).fold_removed_profile_versions()


def backfill_session_runtime_state(cursor: sqlite3.Cursor) -> None:
    try:
        rows = cursor.execute(
            """
            SELECT * FROM run_events
            WHERE event_type = 'session.info'
            ORDER BY session_id ASC, seq ASC, id ASC
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.debug("session_runtime_state backfill skipped: %s", exc)
        return

    latest_by_session: dict[str, dict[str, Any]] = {}
    for row in rows:
        event = decode_run_event_row(row)
        event = event if isinstance(event, dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        session_id = str(
            event.get("conversation_session_id") or _row_value(row, "session_id", 1, "") or ""
        ).strip()
        if not session_id:
            continue
        latest_by_session[session_id] = session_info_record(
            session_id=session_id,
            payload=payload,
            runtime_scope_key=str(
                event.get("runtime_scope_key")
                or _row_value(row, "runtime_scope_key", 5, "")
                or ""
            ),
            execution_session_id=str(
                event.get("execution_session_id")
                or _row_value(row, "execution_session_id", 4, "")
                or event.get("session_id")
                or ""
            ),
            run_id=str(event.get("run_id") or _row_value(row, "run_id", 2, "") or ""),
            turn_id=str(event.get("turn_id") or _row_value(row, "turn_id", 3, "") or ""),
            updated_at=float(
                event.get("timestamp") or _row_value(row, "timestamp", 9, 0) or 0
            ),
            source_seq=int(event.get("seq") or _row_value(row, "seq", 8, 0) or 0),
        )

    for record in latest_by_session.values():
        cursor.execute(
            """
            INSERT INTO session_runtime_state (
                session_id, runtime_scope_key, execution_session_id, run_id,
                turn_id, status, model, provider, profile_json,
                payload_hash, updated_at, source_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                runtime_scope_key = excluded.runtime_scope_key,
                execution_session_id = excluded.execution_session_id,
                run_id = excluded.run_id,
                turn_id = excluded.turn_id,
                status = excluded.status,
                model = excluded.model,
                provider = excluded.provider,
                profile_json = excluded.profile_json,
                payload_hash = excluded.payload_hash,
                updated_at = excluded.updated_at,
                source_seq = excluded.source_seq
            WHERE COALESCE(excluded.source_seq, 0) >=
                  COALESCE(session_runtime_state.source_seq, 0)
            """,
            (
                record["session_id"],
                record["runtime_scope_key"],
                record["execution_session_id"],
                record["run_id"],
                record["turn_id"],
                record["status"],
                record["model"],
                record["provider"],
                record["profile_json"],
                record["payload_hash"],
                record["updated_at"],
                record["source_seq"],
            ),
        )


def backfill_tool_events(cursor: sqlite3.Cursor) -> None:
    backfill_tool_events_from_run_events(cursor.connection, logger=logger)


def backfill_run_event_frame_indexes(cursor: sqlite3.Cursor) -> None:
    try:
        rows = cursor.execute(
            """
            SELECT * FROM run_events
            WHERE frame_blob IS NULL
               OR COALESCE(frame_format, '') = ''
               OR COALESCE(retention_class, '') = ''
               OR NOT EXISTS (
                   SELECT 1 FROM run_event_search_index idx
                   WHERE idx.run_event_id = run_events.id
               )
            ORDER BY session_id ASC, seq ASC, id ASC
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.debug("run_event frame/search-index backfill skipped: %s", exc)
        return

    for row in rows:
        event = decode_run_event_row(row)
        event = event if isinstance(event, dict) else {}
        event_type = str(_row_value(row, "event_type", 7, "") or event.get("type") or "")
        runtime_source_seq = runtime_source_seq_from_event(event)
        row_id = int(_row_value(row, "id", 0, 0) or 0)
        update_run_event_frame_columns(
            cursor.connection,
            row_id=row_id,
            event=event,
            retention_class=_RETENTION_POLICY.classify_event_type(event_type),
            projection_state="raw",
        )
        EventLedger(cursor.connection).update_runtime_source_seq(
            row_id=row_id,
            runtime_source_seq=runtime_source_seq,
        )
        project_run_event_search_index(
            cursor.connection,
            row_id=row_id,
            session_id=str(
                _row_value(row, "session_id", 1, "")
                or event.get("conversation_session_id")
                or ""
            ),
            seq=int(_row_value(row, "seq", 8, 0) or event.get("seq") or 0),
            event_type=event_type,
            runtime_scope_key=str(
                _row_value(row, "runtime_scope_key", 5, "")
                or event.get("runtime_scope_key")
                or ""
            ),
            runtime_source_seq=runtime_source_seq,
            event=event,
            updated_at=float(
                _row_value(row, "timestamp", 9, 0) or event.get("timestamp") or 0
            ),
        )


def migrate_run_events_participant_id(cursor: sqlite3.Cursor) -> None:
    _add_column(cursor, "run_events", "participant_id", "TEXT NOT NULL DEFAULT ''")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_participant "
        "ON run_events(participant_id)"
    )


def migrate_messages_participant_id(cursor: sqlite3.Cursor) -> None:
    _add_column(cursor, "messages", "participant_id", "TEXT NOT NULL DEFAULT ''")


def migrate_session_system_prompts(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS session_system_prompts (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            scope_key TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (session_id, scope_key)
        )
        """
    )


def migrate_activities_kind_mission_check(cursor: sqlite3.Cursor) -> None:
    TeamMissionRepoImpl(cursor.connection).migrate_legacy_activities_kind_mission_check()


def migrate_run_events_activity_id(cursor: sqlite3.Cursor) -> None:
    _add_column(cursor, "run_events", "activity_id", "TEXT")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_activity_seq "
        "ON run_events(activity_id, seq) WHERE activity_id IS NOT NULL"
    )


def migrate_activity_commands(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_commands (
            command_id TEXT PRIMARY KEY,
            activity_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('create', 'start', 'cancel', 'complete')),
            payload_json TEXT NOT NULL DEFAULT '{}',
            intent_at REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'accepted'
                CHECK (state IN ('accepted', 'dispatched', 'satisfied', 'failed')),
            state_changed_at REAL NOT NULL,
            result_event_id INTEGER,
            error_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (result_event_id) REFERENCES run_events(id) ON DELETE SET NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_commands_state "
        "ON activity_commands(state, intent_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_commands_activity "
        "ON activity_commands(activity_id, intent_at)"
    )


def _add_column(
    cursor: sqlite3.Cursor,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        str(_row_value(row, "name", 1, "") or "")
        for row in cursor.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    if column not in columns:
        cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}')


def parse_schema_columns(schema_sql: str) -> dict[str, dict[str, str]]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(schema_sql)
        tables: dict[str, dict[str, str]] = {}
        rows = reference.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table_name,) in rows:
            columns: dict[str, str] = {}
            for row in reference.execute(f'PRAGMA table_info("{table_name}")').fetchall():
                column_type = str(row[2] or "")
                parts = [column_type] if column_type else []
                if row[3] and not row[5]:
                    parts.append("NOT NULL")
                if row[4] is not None:
                    parts.append(f"DEFAULT {row[4]}")
                columns[str(row[1])] = " ".join(parts)
            tables[str(table_name)] = columns
        return tables
    finally:
        reference.close()


def _row_value(
    row: sqlite3.Row | tuple[Any, ...] | None,
    key: str,
    index: int,
    default: Any = None,
) -> Any:
    if row is None:
        return default
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        try:
            return row[index]
        except (IndexError, TypeError):
            return default


__all__ = [
    "backfill_run_event_frame_indexes",
    "backfill_session_list_summaries",
    "backfill_session_runtime_state",
    "backfill_tool_events",
    "fold_agent_profile_versions",
    "migrate_activities_kind_mission_check",
    "migrate_activity_commands",
    "migrate_messages_participant_id",
    "migrate_run_events_activity_id",
    "migrate_run_events_participant_id",
    "migrate_session_system_prompts",
    "reconcile_declared_columns",
]
