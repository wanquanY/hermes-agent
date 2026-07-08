from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from hermes_state import SCHEMA_VERSION
from hermes_state import SessionDB


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_legacy_v39_db(path: Path) -> None:
    conn = _connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (39);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                created_at REAL,
                updated_at REAL
            );
            INSERT INTO sessions (id, title, created_at, updated_at)
            VALUES ('session-valid', 'valid', 1, 1);

            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                runtime_scope_key TEXT,
                turn_id TEXT,
                execution_session_id TEXT,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                last_seq INTEGER DEFAULT 0,
                error TEXT,
                metadata_json TEXT
            );
            INSERT INTO runs VALUES
                ('run-valid', 'session-valid', 'member-chat:conv:m1', 'turn-1', 'runtime-1', 'running', 1, 1, NULL, 7, '', '{}'),
                ('run-orphan', 'session-missing', 'member-chat:missing:m1', 'turn-x', 'runtime-x', 'running', 1, 1, NULL, 1, '', '{}');

            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT,
                turn_id TEXT,
                execution_session_id TEXT,
                runtime_scope_key TEXT,
                participant_id TEXT NOT NULL DEFAULT '',
                activity_id TEXT,
                event_type TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                payload_json TEXT,
                event_json TEXT NOT NULL,
                status TEXT,
                frame_blob BLOB,
                frame_format TEXT,
                retention_class TEXT,
                projected_message_id TEXT,
                projected_tool_event_id TEXT,
                projection_state TEXT,
                runtime_source_seq INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, seq)
            );
            INSERT INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                participant_id, activity_id, event_type, seq, timestamp, payload_json,
                event_json, status, frame_blob, frame_format, retention_class,
                projected_message_id, projected_tool_event_id, projection_state,
                runtime_source_seq
            )
            VALUES
                ('session-valid', 'run-valid', 'turn-1', 'runtime-1', 'member-chat:conv:m1', 'member:m1', 'act-chat:session-valid', 'message.start', 1, 1, '{}', '{"type":"message.start","seq":1}', '', NULL, NULL, 'live', '', '', 'raw', 1),
                ('session-missing', 'run-orphan', 'turn-x', 'runtime-x', 'member-chat:missing:m1', 'member:m1', 'act-chat:missing', 'message.start', 1, 1, '{}', '{"type":"message.start","seq":1}', '', NULL, NULL, 'live', '', '', 'raw', 1);

            CREATE TABLE run_event_search_index (
                run_event_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                runtime_scope_key TEXT,
                runtime_source_seq INTEGER NOT NULL DEFAULT 0,
                search_text TEXT NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (run_event_id) REFERENCES run_events(id) ON DELETE CASCADE
            );
            INSERT INTO run_event_search_index VALUES
                (1, 'session-valid', 1, 'message.start', 'member-chat:conv:m1', 1, 'valid event', 1),
                (999, 'session-valid', 999, 'message.start', 'member-chat:conv:m1', 999, 'orphan event', 1);

            CREATE TABLE messages (
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
                active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO messages (
                session_id, role, content, participant_id, timestamp, metadata_json
            )
            VALUES
                ('session-valid', 'assistant', 'ok', 'member:m1', 1, '{}'),
                ('session-missing', 'assistant', 'orphan', 'member:m1', 1, '{}');

            CREATE TABLE session_runtime_state (
                session_id TEXT PRIMARY KEY,
                runtime_scope_key TEXT,
                execution_session_id TEXT,
                run_id TEXT,
                turn_id TEXT,
                status TEXT,
                model TEXT,
                provider TEXT,
                profile_json TEXT,
                payload_hash TEXT,
                updated_at REAL NOT NULL,
                source_seq INTEGER
            );
            INSERT INTO session_runtime_state VALUES
                ('session-valid', 'member-chat:conv:m1', 'runtime-1', 'run-valid', 'turn-1', 'running', 'model', 'provider', '{}', 'hash', 1, 1),
                ('session-missing', 'member-chat:missing:m1', 'runtime-x', 'run-orphan', 'turn-x', 'running', 'model', 'provider', '{}', 'hash', 1, 1);

            CREATE TABLE conversation_participants (
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
            INSERT INTO conversation_participants VALUES
                ('session-valid', 'member:m1', 'member', 'm1', 'profile-1', 'version-1', 'member-chat:conv:m1', 'Member', '', '{}', 1, 1),
                ('session-missing', 'member:ghost', 'member', 'ghost', 'profile-x', 'version-x', 'member-chat:missing:ghost', 'Ghost', '', '{}', 1, 1);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_identity_fk_migration_preserves_runtime_identity_and_removes_orphans(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _create_legacy_v39_db(db_path)

    db = SessionDB(db_path)
    db.close()

    conn = _connect(db_path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_event_search_index").fetchone()[0] == 1
        assert conn.execute("SELECT run_event_id FROM run_event_search_index").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM session_runtime_state").fetchone()[0] == 1
        assert (
            conn.execute(
                """
                SELECT COUNT(*)
                  FROM conversation_participants
                 WHERE conversation_session_id = 'session-missing'
                """
            ).fetchone()[0]
            == 0
        )

        run = conn.execute("SELECT * FROM runs").fetchone()
        assert run["runtime_scope_key"] == "member-chat:conv:m1"

        participant = conn.execute(
            """
            SELECT *
              FROM conversation_participants
             WHERE conversation_session_id = 'session-valid'
               AND participant_id = 'member:m1'
            """
        ).fetchone()
        assert participant is not None
        assert participant["conversation_session_id"] == "session-valid"
        assert participant["participant_id"] == "member:m1"
        assert participant["runtime_scope_key"] == "member-chat:conv:m1"

        fk_rows = conn.execute('PRAGMA foreign_key_list("conversation_participants")').fetchall()
        assert any(row["table"] == "sessions" for row in fk_rows)
    finally:
        conn.close()


def test_identity_fk_migration_rolls_back_on_unhandled_fk_violation(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _create_legacy_v39_db(db_path)

    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE activity_commands (
                command_id TEXT PRIMARY KEY,
                activity_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                intent_at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'accepted',
                state_changed_at REAL NOT NULL,
                result_event_id INTEGER,
                error_reason TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (result_event_id) REFERENCES run_events(id) ON DELETE SET NULL
            );
            INSERT INTO activity_commands (
                command_id, activity_id, kind, intent_at, state_changed_at, result_event_id
            )
            VALUES ('cmd-orphan', 'activity-1', 'complete', 1, 1, 9999);
            """
        )
        conn.commit()
        before_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        before_run_events = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]

        migration = importlib.import_module(
            "hermes_agent.storage.migrations.0040_identity_fk_and_orphan_cleanup"
        )
        try:
            migration.apply(conn.cursor())
        except RuntimeError as exc:
            assert "FK violations after migration" in str(exc)
        else:
            raise AssertionError("migration unexpectedly succeeded")

        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before_messages
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == before_run_events
        assert conn.execute("SELECT COUNT(*) FROM run_event_search_index").fetchone()[0] == 2
    finally:
        conn.close()
