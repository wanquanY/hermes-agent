"""Session deletion lifecycle service.

The service owns destructive cleanup across the session aggregate boundary:
session rows, sidebar index rows, transcript messages, branch lineage records,
and optional on-disk transcript files. It is intentionally outside
SessionRepoImpl because deletion crosses message and lineage tables.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hermes_agent.repositories.base import RepositoryConnection


@dataclass(frozen=True)
class SessionDeletionResult:
    session_deleted: bool
    index_deleted: bool


class SessionDeletionService:
    """SQLite-backed session deletion service."""

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def delete(self, session_id: str, *, sessions_dir: Path | None = None) -> SessionDeletionResult:
        stable = str(session_id or "").strip()
        if not stable:
            return SessionDeletionResult(session_deleted=False, index_deleted=False)
        with self._conn:
            existed = _session_exists(self._conn, stable)
            if existed:
                _orphan_children(self._conn, stable)
                _delete_branch_requests(self._conn, stable)
                _delete_lineage(self._conn, stable)
                _delete_messages(self._conn, stable)
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (stable,))
            index_deleted = _delete_session_index(self._conn, stable)
        if existed:
            _remove_session_files(sessions_dir, stable)
        return SessionDeletionResult(
            session_deleted=existed,
            index_deleted=index_deleted,
        )


def _session_exists(conn: RepositoryConnection, session_id: str) -> bool:
    if not _table_exists(conn, "sessions"):
        return False
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
        (session_id,),
    ).fetchone()
    return row is not None


def _orphan_children(conn: RepositoryConnection, session_id: str) -> None:
    if _table_exists(conn, "sessions") and _column_exists(conn, "sessions", "parent_session_id"):
        conn.execute(
            "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
            (session_id,),
        )
    if _table_exists(conn, "session_lineage") and _column_exists(
        conn,
        "session_lineage",
        "parent_session_id",
    ):
        conn.execute(
            "UPDATE session_lineage SET parent_session_id = NULL WHERE parent_session_id = ?",
            (session_id,),
        )


def _delete_branch_requests(conn: RepositoryConnection, session_id: str) -> None:
    if not _table_exists(conn, "session_branch_requests"):
        return
    conn.execute(
        "DELETE FROM session_branch_requests "
        "WHERE source_session_id = ? OR result_session_id = ?",
        (session_id, session_id),
    )


def _delete_lineage(conn: RepositoryConnection, session_id: str) -> None:
    if not _table_exists(conn, "session_lineage"):
        return
    conn.execute("DELETE FROM session_lineage WHERE session_id = ?", (session_id,))


def _delete_messages(conn: RepositoryConnection, session_id: str) -> None:
    if not _table_exists(conn, "messages"):
        return
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))


def _delete_session_index(conn: RepositoryConnection, session_id: str) -> bool:
    if not _table_exists(conn, "session_index"):
        return False
    cursor = conn.execute(
        "DELETE FROM session_index WHERE session_id = ?",
        (session_id,),
    )
    return int(cursor.rowcount or 0) > 0


def _remove_session_files(sessions_dir: Path | None, session_id: str) -> None:
    if sessions_dir is None:
        return
    for suffix in (".json", ".jsonl"):
        path = sessions_dir / f"{session_id}{suffix}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        for path in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _table_exists(conn: RepositoryConnection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: RepositoryConnection, table_name: str, column_name: str) -> bool:
    try:
        return any(
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) == column_name
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        )
    except Exception:
        return False


__all__ = ["SessionDeletionResult", "SessionDeletionService"]
