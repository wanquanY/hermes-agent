"""Runtime repair contract for derived transcript search indexes."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.composition.session_repository_db import (
    connect_session_repository_db,
)
from hermes_agent.repositories.message_repo import MessageRepository
from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
from hermes_agent.storage.fts_schema import is_fts_write_corruption_error


def _make_repo(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    sessions = SessionRepoImpl(conn)
    sessions.create(SessionSpec(session_id="s1", source="test"))
    conn.commit()
    return conn, MessageRepository(conn, sessions)


def _corrupt_fts(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE messages_fts_data "
        "SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
    )
    conn.commit()


def test_fts_corruption_classifier_covers_sqlite_variants():
    assert is_fts_write_corruption_error(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    assert is_fts_write_corruption_error(
        sqlite3.DatabaseError(
            'fts5: corrupt structure record for table "messages_fts"'
        )
    )
    assert not is_fts_write_corruption_error(
        sqlite3.DatabaseError("no such table: unrelated")
    )


def test_append_rebuilds_corrupt_fts_without_losing_transcript(tmp_path):
    conn, repo = _make_repo(tmp_path)
    try:
        repo.append_conversation_message(
            "s1", {"role": "user", "content": "before corruption"}
        )
        _corrupt_fts(conn)

        message_id = repo.append_conversation_message(
            "s1", {"role": "user", "content": "searchable needle text"}
        )

        assert message_id > 0
        rows = conn.execute(
            "SELECT content FROM messages ORDER BY id"
        ).fetchall()
        assert [row[0] for row in rows] == [
            "before corruption",
            "searchable needle text",
        ]
        hits = conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'needle'"
        ).fetchall()
        assert len(hits) == 1
    finally:
        conn.close()


def test_runtime_fts_rebuild_is_one_shot_per_repository(tmp_path):
    conn, repo = _make_repo(tmp_path)
    try:
        repo.append_conversation_message("s1", {"role": "user", "content": "seed"})
        _corrupt_fts(conn)
        repo.append_conversation_message(
            "s1", {"role": "user", "content": "first heal"}
        )
        assert repo._fts_runtime_rebuild_attempted is True

        _corrupt_fts(conn)
        with pytest.raises(sqlite3.DatabaseError):
            repo.append_conversation_message(
                "s1", {"role": "user", "content": "second corruption"}
            )
    finally:
        conn.close()


def test_non_fts_database_errors_do_not_consume_recovery(tmp_path):
    conn, repo = _make_repo(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            repo._execute_write(
                lambda: conn.execute(
                    "INSERT INTO messages(session_id, role, timestamp) "
                    "VALUES (NULL, 'user', 1)"
                )
            )
        assert repo._fts_runtime_rebuild_attempted is False
    finally:
        conn.close()
