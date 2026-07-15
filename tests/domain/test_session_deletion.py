from __future__ import annotations

import sqlite3

from hermes_agent.application.session_deletion import SessionDeletionService


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY
        );
        CREATE TABLE session_lineage (
            session_id TEXT PRIMARY KEY,
            parent_session_id TEXT
        );
        CREATE TABLE session_branch_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_session_id TEXT,
            result_session_id TEXT
        );
        """
    )
    return conn


def test_delete_session_cleans_graph_and_files(tmp_path):
    conn = _conn()
    conn.execute("INSERT INTO sessions (id) VALUES ('source')")
    conn.execute("INSERT INTO sessions (id, parent_session_id) VALUES ('child', 'source')")
    conn.execute("INSERT INTO messages (session_id) VALUES ('source')")
    conn.execute("INSERT INTO session_index (session_id) VALUES ('source')")
    conn.execute("INSERT INTO session_lineage (session_id, parent_session_id) VALUES ('source', '')")
    conn.execute("INSERT INTO session_lineage (session_id, parent_session_id) VALUES ('child', 'source')")
    conn.execute(
        "INSERT INTO session_branch_requests (source_session_id, result_session_id) VALUES ('source', 'child')"
    )
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "source.json").write_text("{}", encoding="utf-8")
    (sessions_dir / "source.jsonl").write_text("{}", encoding="utf-8")
    (sessions_dir / "request_dump_source_1.json").write_text("{}", encoding="utf-8")

    result = SessionDeletionService(conn).delete("source", sessions_dir=sessions_dir)

    assert result.session_deleted is True
    assert result.index_deleted is True
    assert conn.execute("SELECT 1 FROM sessions WHERE id = 'source'").fetchone() is None
    assert conn.execute("SELECT parent_session_id FROM sessions WHERE id = 'child'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM session_lineage WHERE session_id = 'source'").fetchone()[0] == 0
    assert conn.execute("SELECT parent_session_id FROM session_lineage WHERE session_id = 'child'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM session_branch_requests").fetchone()[0] == 0
    assert not (sessions_dir / "source.json").exists()
    assert not (sessions_dir / "source.jsonl").exists()
    assert not (sessions_dir / "request_dump_source_1.json").exists()


def test_delete_orphan_index_without_session():
    conn = _conn()
    conn.execute("INSERT INTO session_index (session_id) VALUES ('orphan')")

    result = SessionDeletionService(conn).delete("orphan")

    assert result.session_deleted is False
    assert result.index_deleted is True
    assert conn.execute("SELECT 1 FROM session_index WHERE session_id = 'orphan'").fetchone() is None


def test_delete_handles_minimal_schema():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY
        );
        INSERT INTO sessions (id) VALUES ('minimal');
        """
    )

    result = SessionDeletionService(conn).delete("minimal")

    assert result.session_deleted is True
    assert result.index_deleted is False
    assert conn.execute("SELECT 1 FROM sessions WHERE id = 'minimal'").fetchone() is None
