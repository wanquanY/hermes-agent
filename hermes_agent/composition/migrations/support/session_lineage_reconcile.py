"""Bootstrap reconciliation for legacy ``session_lineage`` schemas."""

from __future__ import annotations

import sqlite3


def ensure_session_lineage_repository_schema(conn: sqlite3.Connection) -> None:
    """Upgrade legacy lineage rows before repository access begins.

    This remains a migration concern even though it runs during connection
    bootstrap: old databases may predate the versioned migration ledger and
    must be made structurally readable before that ledger can advance.
    """
    columns = _table_columns(conn, "session_lineage")
    additions = {
        "parent_session_id": "TEXT",
        "root_session_id": "TEXT NOT NULL DEFAULT ''",
        "branch_from_message_row_id": "INTEGER",
        "branch_from_turn_id": "TEXT",
        "branch_from_run_id": "TEXT",
        "branch_from_client_message_id": "TEXT",
        "branch_origin": "TEXT NOT NULL DEFAULT 'legacy'",
        "branch_mode": "TEXT NOT NULL DEFAULT 'legacy'",
        "branch_depth": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "REAL NOT NULL DEFAULT 0",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE session_lineage ADD COLUMN {name} {ddl}")
    conn.execute(
        "UPDATE session_lineage SET root_session_id = session_id "
        "WHERE COALESCE(root_session_id, '') = ''"
    )
    conn.execute(
        "DELETE FROM session_lineage WHERE rowid NOT IN ("
        "SELECT MAX(rowid) FROM session_lineage GROUP BY session_id"
        ")"
    )
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_session_lineage_session
            ON session_lineage(session_id);
        CREATE INDEX IF NOT EXISTS idx_session_lineage_parent
            ON session_lineage(parent_session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_session_lineage_root
            ON session_lineage(root_session_id, branch_depth, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_session_lineage_branch_point
            ON session_lineage(branch_from_message_row_id);
        """
    )


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in rows
    }


__all__ = ["ensure_session_lineage_repository_schema"]
