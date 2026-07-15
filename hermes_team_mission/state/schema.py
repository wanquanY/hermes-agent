"""Team Mission SQLite schema and state migrations."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from hermes_team_mission.state.conversation_status_event import (
    compact_team_mission_conversation_status_event,
)


TEAM_MISSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS team_mission_conversations (
    conversation_id TEXT PRIMARY KEY,
    team_id TEXT,
    conversation_session_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    objective TEXT,
    workspace_id TEXT,
    workspace_path TEXT,
    status TEXT NOT NULL,
    -- CR-P4.1: write path removed; CR-P3.1 activities are authoritative.
    active_mission_id TEXT,
    created_by_user_id TEXT,
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_missions (
    conversation_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    added_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (conversation_id, mission_id)
);

CREATE TABLE IF NOT EXISTS team_missions (
    mission_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    team_id TEXT,
    title TEXT NOT NULL,
    objective TEXT,
    workspace_id TEXT,
    workspace_path TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    leader_session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS team_mission_nodes (
    node_id TEXT NOT NULL,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    objective TEXT,
    status TEXT NOT NULL,
    assignee_profile_id TEXT,
    assignee_profile_version_id TEXT,
    canonical_node_id TEXT,
    task_frame_id TEXT,
    runtime_conversation_session_id TEXT,
    execution_session_id TEXT,
    runtime_scope_key TEXT,
    output_contract_json TEXT,
    metadata_json TEXT,
    position_x REAL NOT NULL DEFAULT 0,
    position_y REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (mission_id, node_id)
);

CREATE TABLE IF NOT EXISTS team_mission_edges (
    edge_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    metadata_json TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_mission_run_bindings (
    run_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    node_id TEXT,
    session_id TEXT NOT NULL,
    execution_session_id TEXT,
    runtime_scope_key TEXT,
    role TEXT NOT NULL,
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_mission_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source_event_type TEXT,
    source_run_id TEXT,
    source_session_id TEXT,
    source_seq INTEGER DEFAULT 0,
    dedupe_key TEXT NOT NULL,
    timestamp REAL NOT NULL,
    payload_json TEXT,
    source_event_json TEXT,
    event_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(mission_id, seq),
    UNIQUE(mission_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS team_mission_event_seq_counter (
    mission_id TEXT PRIMARY KEY REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS team_mission_artifacts (
    artifact_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    node_id TEXT,
    run_id TEXT,
    tool_call_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    uri TEXT NOT NULL,
    mime_type TEXT,
    metadata_json TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_mission_deliverables (
    deliverable_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    result TEXT,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    artifact_refs_json TEXT,
    next_context_json TEXT,
    output_contract_json TEXT,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    visibility TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_mission_results (
    result_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL UNIQUE REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    activity_id TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    node_results_json TEXT NOT NULL,
    artifact_refs_json TEXT,
    leader_report_run_id TEXT,
    leader_report_message_id TEXT,
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

"""


TEAM_MISSION_DEFERRED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_team_mission_conversations_team
    ON team_mission_conversations(team_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_conversations_workspace
    ON team_mission_conversations(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_missions_conv
    ON conversation_missions (conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_conversation_missions_mission
    ON conversation_missions (mission_id, status);
CREATE INDEX IF NOT EXISTS idx_team_missions_conversation
    ON team_missions(conversation_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_missions_status_updated
    ON team_missions(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_nodes_mission
    ON team_mission_nodes(mission_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_edges_mission
    ON team_mission_edges(mission_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_run_bindings_mission
    ON team_mission_run_bindings(mission_id, node_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_run_bindings_session
    ON team_mission_run_bindings(session_id, run_id);
CREATE INDEX IF NOT EXISTS idx_team_mission_events_mission_seq
    ON team_mission_events(mission_id, seq ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_events_source_run
    ON team_mission_events(source_run_id, source_seq);
CREATE INDEX IF NOT EXISTS idx_team_mission_event_seq_counter_updated
    ON team_mission_event_seq_counter(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_artifacts_mission
    ON team_mission_artifacts(mission_id, node_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_deliverables_mission
    ON team_mission_deliverables(mission_id, node_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_deliverables_run
    ON team_mission_deliverables(run_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_results_mission
    ON team_mission_results(mission_id, updated_at DESC);
"""


def team_mission_schema_sql() -> str:
    return TEAM_MISSION_SCHEMA_SQL


def team_mission_deferred_index_sql() -> str:
    return TEAM_MISSION_DEFERRED_INDEX_SQL


def _row_value(row: sqlite3.Row | tuple[Any, ...] | None, key: str, index: int, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        try:
            return row[index]  # type: ignore[index]
        except (IndexError, TypeError):
            return default


def _table_columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    try:
        rows = cursor.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.OperationalError:
        return set()
    return {
        str(_row_value(row, "name", 1, "") or "")
        for row in rows
        if str(_row_value(row, "name", 1, "") or "")
    }


def _quoted_legacy_column(column: str) -> str:
    return f'legacy."{column}"'


def _coalesced_text_expr(columns: set[str], *candidates: str, default: str = "''") -> str:
    parts = [
        f"NULLIF({_quoted_legacy_column(column)}, '')"
        for column in candidates
        if column in columns
    ]
    parts.append(default)
    return f"COALESCE({', '.join(parts)})"


def _coalesced_number_expr(columns: set[str], candidate: str, default: float) -> str:
    if candidate in columns:
        return f"COALESCE({_quoted_legacy_column(candidate)}, {default!r})"
    return repr(default)


def _create_canonical_team_mission_conversations(cursor: sqlite3.Cursor, table_name: str) -> None:
    cursor.execute(
        f"""
        CREATE TABLE "{table_name}" (
            conversation_id TEXT PRIMARY KEY,
            team_id TEXT,
            conversation_session_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            objective TEXT,
            workspace_id TEXT,
            workspace_path TEXT,
            status TEXT NOT NULL,
            active_mission_id TEXT,
            created_by_user_id TEXT,
            metadata_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


def _rebuild_team_mission_conversations_for_conversation_session_id(
    cursor: sqlite3.Cursor,
    columns: set[str],
) -> None:
    """Rebuild conversation rows with the canonical conversation_session_id shape."""

    temp_table = "team_mission_conversations__canonical_session_id"
    now = time.time()
    conversation_id_expr = _coalesced_text_expr(
        columns,
        "conversation_id",
        "conversation_session_id",
        default="NULL",
    )
    conversation_session_id_expr = _coalesced_text_expr(
        columns,
        "conversation_session_id",
        "conversation_id",
        default="NULL",
    )
    created_at_expr = _coalesced_number_expr(columns, "created_at", now)
    updated_at_expr = _coalesced_number_expr(columns, "updated_at", now)

    cursor.execute(f'DROP TABLE IF EXISTS "{temp_table}"')
    _create_canonical_team_mission_conversations(cursor, temp_table)
    cursor.execute(
        f"""
        INSERT OR REPLACE INTO "{temp_table}" (
            conversation_id, team_id, conversation_session_id, title, objective,
            workspace_id, workspace_path, status, active_mission_id,
            created_by_user_id, metadata_json, created_at, updated_at
        )
        SELECT
            {conversation_id_expr},
            {_coalesced_text_expr(columns, "team_id")},
            {conversation_session_id_expr},
            {_coalesced_text_expr(columns, "title", default="'Team Mission'")},
            {_coalesced_text_expr(columns, "objective")},
            {_coalesced_text_expr(columns, "workspace_id")},
            {_coalesced_text_expr(columns, "workspace_path")},
            {_coalesced_text_expr(columns, "status", default="'active'")},
            {_coalesced_text_expr(columns, "active_mission_id")},
            {_coalesced_text_expr(columns, "created_by_user_id")},
            {_coalesced_text_expr(columns, "metadata_json", default="'{}'")},
            {created_at_expr},
            {updated_at_expr}
        FROM team_mission_conversations AS legacy
        WHERE {conversation_id_expr} IS NOT NULL
          AND {conversation_session_id_expr} IS NOT NULL
        ORDER BY {updated_at_expr} ASC, {created_at_expr} ASC
        """
    )
    cursor.execute('DROP TABLE "team_mission_conversations"')
    cursor.execute(f'ALTER TABLE "{temp_table}" RENAME TO "team_mission_conversations"')


def migrate_team_mission_runtime_session_columns(cursor: sqlite3.Cursor) -> None:
    """Ensure Team Mission graph tables expose canonical execution identity columns."""

    node_columns = _table_columns(cursor, "team_mission_nodes")
    if node_columns:
        if "runtime_conversation_session_id" not in node_columns:
            cursor.execute(
                "ALTER TABLE team_mission_nodes "
                "ADD COLUMN runtime_conversation_session_id TEXT"
            )
            node_columns.add("runtime_conversation_session_id")
        if "execution_session_id" not in node_columns:
            cursor.execute(
                "ALTER TABLE team_mission_nodes "
                "ADD COLUMN execution_session_id TEXT"
            )
            node_columns.add("execution_session_id")

    binding_columns = _table_columns(cursor, "team_mission_run_bindings")
    if binding_columns:
        if "execution_session_id" not in binding_columns:
            cursor.execute(
                "ALTER TABLE team_mission_run_bindings "
                "ADD COLUMN execution_session_id TEXT"
            )
            binding_columns.add("execution_session_id")


def reconcile_team_mission_node_primary_key(cursor: sqlite3.Cursor) -> None:
    """Ensure Team Mission nodes are keyed by mission and node."""

    rows = cursor.execute('PRAGMA table_info("team_mission_nodes")').fetchall()
    if not rows:
        return
    pk_columns = [
        str(_row_value(row, "name", 1, ""))
        for row in sorted(
            rows,
            key=lambda item: _row_value(item, "pk", 5, 0),
        )
        if _row_value(row, "pk", 5, 0)
    ]
    if pk_columns == ["mission_id", "node_id"]:
        return

    cursor.execute("PRAGMA foreign_keys=OFF")
    cursor.execute("DROP INDEX IF EXISTS idx_team_mission_nodes_mission")
    cursor.execute("ALTER TABLE team_mission_nodes RENAME TO team_mission_nodes_legacy_pk")
    cursor.execute(
        """
        CREATE TABLE team_mission_nodes (
            node_id TEXT NOT NULL,
            mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            objective TEXT,
            status TEXT NOT NULL,
            assignee_profile_id TEXT,
            assignee_profile_version_id TEXT,
            canonical_node_id TEXT,
            task_frame_id TEXT,
            runtime_conversation_session_id TEXT,
            execution_session_id TEXT,
            runtime_scope_key TEXT,
            output_contract_json TEXT,
            metadata_json TEXT,
            position_x REAL NOT NULL DEFAULT 0,
            position_y REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (mission_id, node_id)
        )
        """
    )
    cursor.execute(
        """
        INSERT OR REPLACE INTO team_mission_nodes (
            node_id, mission_id, kind, title, objective, status,
            assignee_profile_id, assignee_profile_version_id,
            canonical_node_id, task_frame_id, runtime_conversation_session_id,
            execution_session_id, runtime_scope_key,
            output_contract_json, metadata_json, position_x, position_y,
            created_at, updated_at
        )
        SELECT
            node_id, mission_id, kind, title, objective, status,
            assignee_profile_id, assignee_profile_version_id,
            canonical_node_id, task_frame_id, runtime_conversation_session_id,
            execution_session_id, runtime_scope_key,
            output_contract_json, metadata_json, position_x, position_y,
            created_at, updated_at
        FROM team_mission_nodes_legacy_pk
        ORDER BY updated_at ASC, created_at ASC
        """
    )
    cursor.execute("DROP TABLE team_mission_nodes_legacy_pk")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_team_mission_nodes_mission "
        "ON team_mission_nodes(mission_id, created_at ASC)"
    )
    cursor.execute("PRAGMA foreign_keys=ON")


def migrate_team_mission_conversation_session_id(cursor: sqlite3.Cursor) -> None:
    """Backfill the canonical conversation_session_id column on legacy tables."""

    columns = _table_columns(cursor, "team_mission_conversations")
    if not columns:
        return
    added_conversation_session_id = False
    if "conversation_session_id" not in columns:
        cursor.execute("ALTER TABLE team_mission_conversations ADD COLUMN conversation_session_id TEXT")
        columns.add("conversation_session_id")
        added_conversation_session_id = True
    cursor.execute(
        """
        UPDATE team_mission_conversations
           SET conversation_session_id = conversation_id
         WHERE COALESCE(conversation_session_id, '') = ''
        """
    )
    if added_conversation_session_id:
        _rebuild_team_mission_conversations_for_conversation_session_id(cursor, columns)
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_team_mission_conversations_session_id
            ON team_mission_conversations(conversation_session_id)
            WHERE COALESCE(conversation_session_id, '') != ''
        """
    )


def migrate_active_mission_id_to_conversation_missions(cursor: sqlite3.Cursor) -> None:
    """Backfill the P3 conversation-mission join table from legacy 1:1 rows."""

    try:
        rows = cursor.execute(
            """
            SELECT conversation_id, active_mission_id, created_at
            FROM team_mission_conversations
            WHERE COALESCE(active_mission_id, '') != ''
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return
    now = time.time()
    for row in rows:
        conversation_id = str(_row_value(row, "conversation_id", 0, "") or "").strip()
        mission_id = str(_row_value(row, "active_mission_id", 1, "") or "").strip()
        if not conversation_id or not mission_id:
            continue
        added_at = float(_row_value(row, "created_at", 2, 0) or now)
        cursor.execute(
            """
            INSERT OR IGNORE INTO conversation_missions (
                conversation_id, mission_id, status, added_at, updated_at, metadata_json
            )
            VALUES (?, ?, 'active', ?, ?, '')
            """,
            (conversation_id, mission_id, added_at, now),
        )


def compact_team_mission_event_json_storage(cursor: sqlite3.Cursor, logger: Any) -> None:
    """Compact legacy duplicate JSON storage in team_mission_events."""

    try:
        cursor.execute(
            """
            UPDATE team_mission_events
            SET payload_json = '',
                source_event_json = ''
            WHERE COALESCE(payload_json, '') != ''
               OR COALESCE(source_event_json, '') != ''
            """
        )
    except sqlite3.OperationalError as exc:
        logger.debug("team_mission_events JSON storage compaction skipped: %s", exc)
        return

    try:
        rows = cursor.execute(
            """
            SELECT id, mission_id, event_json
            FROM team_mission_events
            WHERE event_type = 'team_mission.conversation.status'
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.debug("team_mission_events status event compaction skipped: %s", exc)
        return

    compacted = 0
    for row in rows:
        try:
            event = json.loads(row["event_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        next_event, changed = compact_team_mission_conversation_status_event(
            event,
            mission_id=str(row["mission_id"] or ""),
        )
        if not changed:
            continue
        cursor.execute(
            """
            UPDATE team_mission_events
               SET event_json = ?
             WHERE id = ?
            """,
            (
                json.dumps(next_event, ensure_ascii=False, separators=(",", ":")),
                row["id"],
            ),
        )
        compacted += 1
    if compacted:
        logger.info("compacted %d legacy Team Mission conversation status event(s)", compacted)
