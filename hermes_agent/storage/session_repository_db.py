"""SQLite bootstrap for the SessionRepo-owned table family.

This module is intentionally narrow: it opens ``state.db`` and guarantees the
tables/columns required by ``SessionRepoImpl`` without importing the legacy
state facade. Broader schema ownership remains with the migration system until
each P2 data-plane slice moves to its target repository.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def connect_session_repository_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the repo-owned session connection and ensure its table family."""

    path = Path(db_path) if db_path is not None else get_hermes_home() / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=1.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _configure_connection(conn, db_label=str(path))
    ensure_session_repository_schema(conn)
    return conn


def _configure_connection(conn: sqlite3.Connection, *, db_label: str) -> None:
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error as exc:
        logger.warning(
            "Session repository could not enable WAL for %s; falling back to DELETE: %s",
            db_label,
            exc,
        )
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            logger.debug("Session repository DELETE journal fallback failed", exc_info=True)


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
            active_runtime_session_id TEXT NOT NULL DEFAULT '',
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
            session_id TEXT NOT NULL,
            branch_origin TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_session_index_updated
            ON session_index(updated_at DESC, started_at DESC, session_id DESC);
        """
    )
    _ensure_columns(
        conn,
        "sessions",
        {
            "updated_at": "REAL NOT NULL DEFAULT 0",
            "session_kind": "TEXT NOT NULL DEFAULT 'hermes_session'",
            "conversation_kind": "TEXT NOT NULL DEFAULT 'direct'",
        },
    )
    _ensure_columns(
        conn,
        "session_index",
        {
            "transient": "INTEGER NOT NULL DEFAULT 0",
            "team_id": "TEXT NOT NULL DEFAULT ''",
            "mission_id": "TEXT NOT NULL DEFAULT ''",
            "conversation_id": "TEXT NOT NULL DEFAULT ''",
        },
    )


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


__all__ = ["connect_session_repository_db", "ensure_session_repository_schema"]
