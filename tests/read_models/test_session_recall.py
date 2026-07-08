from __future__ import annotations

import sqlite3

from hermes_agent.read_models.session_recall import SessionRecallReadModel


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'test',
            title TEXT,
            display_title TEXT,
            display_title_source TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            updated_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            preview TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    return conn


def test_resolve_resume_session_id_follows_compression_tip_with_messages():
    conn = _make_conn()
    conn.executescript(
        """
        INSERT INTO sessions (id, started_at, updated_at, ended_at, end_reason)
        VALUES ('parent', 1, 1, 2, 'compression');
        INSERT INTO sessions (id, parent_session_id, started_at, updated_at)
        VALUES ('tip', 'parent', 3, 3);
        INSERT INTO messages (session_id, active) VALUES ('tip', 1);
        """
    )

    assert SessionRecallReadModel(conn).resolve_resume_session_id("parent") == "tip"


def test_resolve_resume_session_id_follows_latest_child_until_messages_exist():
    conn = _make_conn()
    conn.executescript(
        """
        INSERT INTO sessions (id, started_at, updated_at)
        VALUES ('parent', 1, 1);
        INSERT INTO sessions (id, parent_session_id, started_at, updated_at)
        VALUES ('empty-child', 'parent', 2, 2);
        INSERT INTO sessions (id, parent_session_id, started_at, updated_at)
        VALUES ('message-child', 'empty-child', 3, 3);
        INSERT INTO messages (session_id, active) VALUES ('message-child', 1);
        """
    )

    assert (
        SessionRecallReadModel(conn).resolve_resume_session_id("parent")
        == "message-child"
    )
