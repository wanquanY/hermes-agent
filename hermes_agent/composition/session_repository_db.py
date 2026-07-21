"""SQLite bootstrap for the repo-owned state.db table family.

This module opens ``state.db`` for the repository-backed runtime path. It
guarantees the write tables owned by repository slices and the compatibility
tables required by gateway read models, without importing the legacy state
facade.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_agent.domain.seq_allocator import ensure_seq_counter_table
from hermes_agent.repositories.agent_profile_repo import ensure_agent_profile_repository_schema
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl
from hermes_agent.repositories.team_registry_repo import ensure_team_registry_repository_schema
from hermes_agent.storage.state_schema import (
    CRON_RUN_HISTORY_INDEX_SQL,
    RUNTIME_DEFERRED_INDEX_SQL,
    SCHEMA_SQL,
)
from hermes_agent.composition.execution_session_migration import (
    reconcile_legacy_delegate_execution_sessions,
    reconcile_team_mission_session_classification,
)
from hermes_agent.storage.fts_schema import ensure_message_fts
from hermes_agent.composition.migration_operations import reconcile_declared_columns
from hermes_agent.composition.migrations import CURRENT_SCHEMA_VERSION, MigrationRunner
from hermes_agent.composition.migrations.support.session_lineage_reconcile import (
    ensure_session_lineage_repository_schema,
)
from hermes_agent.storage.session_store_health import set_last_init_error
from hermes_agent.storage.sqlite_wal import configure_sqlite_connection
from hermes_team_mission.state.schema import migrate_active_mission_id_to_conversation_missions
from hermes_team_mission.state.schema import migrate_team_mission_runtime_session_columns
from hermes_team_mission.state.schema import migrate_team_mission_conversation_session_id
from hermes_team_mission.state.schema import reconcile_team_mission_node_primary_key
from hermes_team_mission.state.schema import team_mission_deferred_index_sql
from hermes_team_mission.state.schema import team_mission_schema_sql

logger = logging.getLogger(__name__)


class SessionRepositoryConnection(sqlite3.Connection):
    """Connection type for repo-owned bootstrap compatibility.

    Some older gateway tests and integration helpers create the `messages`
    table by hand after opening the repository connection. The repository now
    owns that table family and bootstraps it eagerly, so make the legacy DDL
    idempotent without changing the schema or requiring callers to know the
    bootstrap order.
    """

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        statement = sql.lstrip()
        if statement.lower().startswith("create table messages"):
            sql = sql.replace("CREATE TABLE messages", "CREATE TABLE IF NOT EXISTS messages", 1)
            sql = sql.replace("create table messages", "CREATE TABLE IF NOT EXISTS messages", 1)
        return super().execute(sql, parameters)


def connect_session_repository_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the repo-owned session connection and ensure its table family."""

    path = Path(db_path) if db_path is not None else get_hermes_home() / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        timeout=1.0,
        isolation_level=None,
        factory=SessionRepositoryConnection,
    )
    conn.row_factory = sqlite3.Row
    try:
        _configure_connection(conn, db_label=str(path))
        _validate_existing_runtime_identity_schema(conn)
        ensure_session_repository_schema(conn)
        MigrationRunner(conn.cursor()).run_all()
        reconcile_session_repository_data(conn)
        ensure_message_fts(conn)
        return conn
    except Exception as exc:
        set_last_init_error(f"{type(exc).__name__}: {exc}")
        conn.close()
        raise


def _configure_connection(conn: sqlite3.Connection, *, db_label: str) -> None:
    configure_sqlite_connection(conn, db_label=db_label)


def ensure_session_repository_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the tables owned by ``SessionRepoImpl``."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            preview TEXT DEFAULT '',
            last_active REAL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            display_title TEXT DEFAULT '',
            display_title_source TEXT DEFAULT '',
            cwd TEXT,
            git_branch TEXT NOT NULL DEFAULT '',
            git_repo_root TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT,
            handoff_platform TEXT,
            handoff_error TEXT,
            rewind_count INTEGER NOT NULL DEFAULT 0,
            transient INTEGER DEFAULT 0,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS session_compression_leases (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            holder TEXT NOT NULL,
            expires_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_session_compression_leases_expiry
            ON session_compression_leases(expires_at);

        CREATE TABLE IF NOT EXISTS session_index (
            session_id TEXT PRIMARY KEY,
            owner_agent_profile_id TEXT NOT NULL DEFAULT '',
            owner_profile_version_id TEXT NOT NULL DEFAULT '',
            runtime_scope_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'unknown',
            transient INTEGER NOT NULL DEFAULT 0,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            status TEXT NOT NULL DEFAULT 'idle',
            running INTEGER NOT NULL DEFAULT 0,
            waiting_approval INTEGER NOT NULL DEFAULT 0,
            active_run_id TEXT NOT NULL DEFAULT '',
            active_execution_session_id TEXT NOT NULL DEFAULT '',
            pending_approval_count INTEGER NOT NULL DEFAULT 0,
            team_id TEXT NOT NULL DEFAULT '',
            mission_id TEXT NOT NULL DEFAULT '',
            conversation_id TEXT NOT NULL DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            last_activity REAL
        );

        CREATE TABLE IF NOT EXISTS session_branches (
            child_session_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            branch_from_seq INTEGER NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (child_session_id) REFERENCES sessions(id),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS session_lineage (
            session_id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            root_session_id TEXT NOT NULL,
            branch_from_message_row_id INTEGER,
            branch_from_turn_id TEXT,
            branch_from_run_id TEXT,
            branch_from_client_message_id TEXT,
            branch_origin TEXT NOT NULL,
            branch_mode TEXT NOT NULL,
            branch_depth INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
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
            api_content TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS state_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        """
    )
    # Existing tables are not changed by CREATE TABLE IF NOT EXISTS. Reconcile
    # every declarative column before creating indexes or running read models.
    reconcile_declared_columns(conn.cursor())
    conn.executescript(CRON_RUN_HISTORY_INDEX_SQL)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_session_index_updated
            ON session_index(updated_at DESC, started_at DESC, session_id DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_session_id
            ON messages(session_id, id);
        """
    )
    ensure_session_lineage_repository_schema(conn)
    ensure_runtime_repository_schema(conn)
    ensure_agent_profile_repository_schema(conn)
    ensure_seq_counter_table(conn)
    ensure_team_registry_repository_schema(conn)
    ensure_session_index_read_side_schema(conn)


def reconcile_session_repository_data(conn: sqlite3.Connection) -> None:
    """Run data reconciliations only after all versioned migrations complete."""

    normalized_index_rows = SessionRepoImpl(conn).normalize_index_conversation_kind()
    if normalized_index_rows:
        logger.info(
            "Session repository normalized %d legacy session index classifications",
            normalized_index_rows,
        )
    classification = reconcile_team_mission_session_classification(conn)
    if any(classification.values()):
        logger.info(
            "Session repository reconciled Team Mission classifications: %s",
            classification,
        )
    migrated_executions = reconcile_legacy_delegate_execution_sessions(conn)
    if migrated_executions:
        logger.info(
            "Session repository classified %d legacy delegated executions",
            migrated_executions,
        )


def ensure_runtime_repository_schema(conn: sqlite3.Connection) -> None:
    """Create and verify repository-owned runtime tables."""

    conn.executescript(SCHEMA_SQL)
    conn.executescript(RUNTIME_DEFERRED_INDEX_SQL)
    for table_name in ("runs", "run_events", "session_runtime_state"):
        columns = _table_columns(conn, table_name)
        if "execution_session_id" not in columns:
            raise RuntimeError(
                f"runtime repository schema is not canonical for {table_name}: "
                f"columns={sorted(columns)}"
            )


def _validate_existing_runtime_identity_schema(conn: sqlite3.Connection) -> None:
    """Reject current/unknown noncanonical schemas before reconcile masks them.

    A database with an explicit older schema version is a supported migration
    input. Declarative reconciliation may add its canonical identity columns
    before the remaining versioned migrations run.
    """
    noncanonical_tables: list[tuple[str, set[str]]] = []
    for table_name in ("runs", "run_events", "session_runtime_state"):
        columns = _table_columns(conn, table_name)
        if not columns or "execution_session_id" in columns:
            continue
        noncanonical_tables.append((table_name, columns))
    if not noncanonical_tables:
        return

    schema_version = _existing_schema_version(conn)
    if schema_version is not None and schema_version < CURRENT_SCHEMA_VERSION:
        return

    details = "; ".join(
        f"{table_name}: columns={sorted(columns)}"
        for table_name, columns in noncanonical_tables
    )
    raise RuntimeError(f"runtime repository schema is not canonical for {details}")


def _existing_schema_version(conn: sqlite3.Connection) -> int | None:
    if not _table_columns(conn, "schema_version"):
        return None
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def ensure_session_index_read_side_schema(conn: sqlite3.Connection) -> None:
    """Ensure cross-domain tables required by the session_index read model."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_participants (
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
            PRIMARY KEY (conversation_session_id, participant_id)
        );

        CREATE TABLE IF NOT EXISTS activities (
            activity_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            parent_activity_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('chat', 'agent_dispatch', 'team_dispatch', 'member_chat', 'mission')),
            target_profile_id TEXT,
            target_team_id TEXT,
            target_mission_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
            prompt_summary TEXT,
            result_summary TEXT,
            result_json TEXT,
            started_at REAL,
            completed_at REAL,
            notify_parent INTEGER NOT NULL DEFAULT 1,
            read_at REAL,
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_activities_conv
            ON activities(conversation_id, status);
        CREATE INDEX IF NOT EXISTS idx_activities_parent
            ON activities(parent_activity_id, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_mission
            ON activities(target_mission_id)
            WHERE kind = 'mission' AND COALESCE(target_mission_id, '') != '';
        """
    )
    TeamMissionRepoImpl(conn).migrate_legacy_activities_kind_mission_check()
    cursor = conn.cursor()
    cursor.executescript(team_mission_schema_sql())
    migrate_team_mission_runtime_session_columns(cursor)
    migrate_team_mission_conversation_session_id(cursor)
    reconcile_team_mission_node_primary_key(cursor)
    migrate_active_mission_id_to_conversation_missions(cursor)
    try:
        cursor.executescript(team_mission_deferred_index_sql())
    except sqlite3.OperationalError:
        logger.debug("session index read-side deferred indexes skipped", exc_info=True)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


__all__ = [
    "connect_session_repository_db",
    "reconcile_session_repository_data",
    "ensure_runtime_repository_schema",
    "ensure_session_repository_schema",
]
