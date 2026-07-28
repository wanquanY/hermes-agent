"""Persistent timeline sequence allocation domain service.

The allocator owns the authoritative ``run_events.seq`` domain. Runtime source
sequence values are metadata only; canonical timeline order is allocated here
inside the same SQLite write transaction that writes the event.
"""

from __future__ import annotations

import sqlite3
import time

from hermes_agent.domain.exceptions import SeqAllocatorBusy


_BUSY_BACKOFF_SECONDS = (0.1, 0.3, 0.9)


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message or "locked" in message


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_seq_counter_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seq_counter (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def backfill_seq_counter(conn: sqlite3.Connection, *, updated_at: float = 0) -> None:
    ensure_seq_counter_table(conn)
    conn.execute(
        """
        INSERT INTO seq_counter (session_id, next_seq, updated_at)
        SELECT
            sessions.id,
            COALESCE(MAX(run_events.seq), 0) + 1,
            ?
        FROM sessions
        LEFT JOIN run_events
          ON run_events.session_id = sessions.id
        GROUP BY sessions.id
        ON CONFLICT(session_id) DO UPDATE SET
            next_seq = MAX(seq_counter.next_seq, excluded.next_seq),
            updated_at = MAX(seq_counter.updated_at, excluded.updated_at)
        """,
        (float(updated_at or 0),),
    )


def ensure_session_counter(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    updated_at: float,
) -> None:
    stable = str(session_id or "").strip()
    if not stable:
        return
    ensure_seq_counter_table(conn)
    existing = conn.execute(
        "SELECT next_seq FROM seq_counter WHERE session_id = ?",
        (stable,),
    ).fetchone()
    if existing is not None:
        return
    has_events = conn.execute(
        "SELECT 1 FROM run_events WHERE session_id = ? LIMIT 1",
        (stable,),
    ).fetchone()
    if has_events is not None:
        raise RuntimeError(
            f"seq_counter missing for non-empty session {stable}; run migrations before writing events"
        )
    if _table_exists(conn, "sessions"):
        conn.execute(
            """
            INSERT INTO seq_counter (session_id, next_seq, updated_at)
            SELECT ?, 1, ?
            WHERE EXISTS (SELECT 1 FROM sessions WHERE id = ?)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (stable, float(updated_at or 0), stable),
        )
    else:
        conn.execute(
            """
            INSERT INTO seq_counter (session_id, next_seq, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (stable, float(updated_at or 0)),
        )


def allocate_run_event_seq(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    updated_at: float,
) -> int:
    return allocate_only(conn, session_id=session_id, updated_at=updated_at)


def allocate_only(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    updated_at: float,
) -> int:
    stable = str(session_id or "").strip()
    if not stable:
        raise ValueError("session_id is required")
    for attempt, delay in enumerate((*_BUSY_BACKOFF_SECONDS, 0.0)):
        try:
            return _allocate_once(conn, session_id=stable, updated_at=updated_at)
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or attempt >= len(_BUSY_BACKOFF_SECONDS):
                raise SeqAllocatorBusy(f"seq allocation busy for session {stable}") from exc
            time.sleep(delay)
    raise SeqAllocatorBusy(f"seq allocation busy for session {stable}")


def _allocate_once(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    updated_at: float,
) -> int:
    ensure_session_counter(conn, session_id=session_id, updated_at=updated_at)
    row = conn.execute(
        """
        UPDATE seq_counter
           SET next_seq = next_seq + 1,
               updated_at = ?
         WHERE session_id = ?
        RETURNING next_seq - 1
        """,
        (float(updated_at or 0), session_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"seq_counter allocation failed for session {session_id}")
    return int(row[0])
