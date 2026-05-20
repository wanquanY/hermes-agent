"""SessionDB schema migration and reconciliation tests."""

import pytest

from hermes_state import SessionDB
from tests.hermes_state_fixtures import db


class TestSchemaInit:
    def test_wal_mode(self, db):
        cursor = db._conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_enabled(self, db):
        cursor = db._conn.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1

    def test_tables_exist(self, db):
        cursor = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert "sessions" in tables
        assert "messages" in tables
        assert "schema_version" in tables
        assert "runs" in tables
        assert "run_events" in tables
        assert "run_event_archives" in tables

    def test_schema_version(self, db):
        cursor = db._conn.execute("SELECT version FROM schema_version")
        version = cursor.fetchone()[0]
        assert version == 11

    def test_existing_runs_table_is_reconciled_before_scope_index(self, tmp_path):
        """Old run-state DBs must add runtime_scope_key before creating its index."""
        import sqlite3

        old_db = tmp_path / "old-runs.db"
        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                runtime_session_id TEXT,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                last_seq INTEGER DEFAULT 0,
                error TEXT,
                metadata_json TEXT
            );
            INSERT INTO runs (
                run_id, session_id, turn_id, runtime_session_id, status,
                started_at, updated_at, last_seq
            ) VALUES (
                'run-old', 'stored-old', 'turn-old', 'runtime-old',
                'running', 1000.0, 1000.0, 1
            );
            """
        )
        conn.close()

        db = SessionDB(db_path=old_db)
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(runs)").fetchall()}
        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'runs'"
            ).fetchall()
        }

        assert "runtime_scope_key" in columns
        assert "idx_runs_scope_status" in indexes
        assert [run["run_id"] for run in db.list_runs(runtime_scope_key="stored-old")] == ["run-old"]
        db.close()

    def test_existing_run_events_table_backfills_runtime_scope_key(self, tmp_path):
        """Old event logs get a first-class scope column before scoped replay."""
        import sqlite3

        old_db = tmp_path / "old-run-events.db"
        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT,
                turn_id TEXT,
                runtime_session_id TEXT,
                event_type TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                payload_json TEXT,
                event_json TEXT NOT NULL,
                status TEXT,
                UNIQUE(session_id, seq)
            );
            INSERT INTO run_events (
                session_id, run_id, turn_id, runtime_session_id, event_type,
                seq, timestamp, payload_json, event_json, status
            ) VALUES (
                'stored-old', 'run-old', 'turn-old', 'runtime-old', 'message.start',
                1, 1000.0,
                '{"run_id":"run-old","turn_id":"turn-old"}',
                '{"type":"message.start","stored_session_id":"stored-old","run_id":"run-old","runtime_scope_key":"profile:alpha"}',
                ''
            );
            """
        )
        conn.close()

        db = SessionDB(db_path=old_db)
        columns = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(run_events)").fetchall()
        }
        row = db._conn.execute(
            "SELECT runtime_scope_key FROM run_events WHERE run_id = 'run-old'"
        ).fetchone()

        assert "runtime_scope_key" in columns
        assert row[0] == "profile:alpha"
        events = db.list_run_events(
            "stored-old",
            runtime_scope_key="profile:alpha",
        )
        assert [event["run_id"] for event in events] == [
            "run-old"
        ]
        db.close()

    def test_title_column_exists(self, db):
        """Verify the title column was created in the sessions table."""
        cursor = db._conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "title" in columns

    def test_topic_mode_schema_is_not_auto_migrated_on_open(self, tmp_path):
        """Opening an old DB should not add topic-mode columns until /topic opts in.

        The gateway must remain rollback-safe: simply upgrading Hermes and starting
        the old bot should not eagerly mutate the state DB for this feature.
        """
        old_db = tmp_path / "old.db"
        import sqlite3

        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
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
                api_call_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
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
                codex_message_items TEXT
            );
            """
        )
        conn.close()

        db = SessionDB(db_path=old_db)
        cursor = db._conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        assert {"chat_id", "chat_type", "thread_id", "session_key"}.isdisjoint(columns)
        db.close()

    def test_apply_telegram_topic_migration_creates_topic_tables_explicitly(self, tmp_path):
        """The /topic opt-in path owns the DB migration for Telegram topic mode."""
        old_db = tmp_path / "old.db"
        import sqlite3

        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
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
                api_call_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
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
                codex_message_items TEXT
            );
            """
        )
        conn.close()

        db = SessionDB(db_path=old_db)
        db.apply_telegram_topic_migration()

        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "telegram_dm_topic_mode" in tables
        assert "telegram_dm_topic_bindings" in tables
        assert db.get_meta("telegram_dm_topic_schema_version") == "2"
        db.close()

    def test_telegram_topic_binding_roundtrip_requires_explicit_schema(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )

        assert db.get_telegram_topic_binding(chat_id="208214988", thread_id="17585") is None

        db.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="telegram:dm:208214988:thread:17585",
            session_id="topic-session",
        )

        binding = db.get_telegram_topic_binding(chat_id="208214988", thread_id="17585")
        assert binding is not None
        assert binding["chat_id"] == "208214988"
        assert binding["thread_id"] == "17585"
        assert binding["user_id"] == "208214988"
        assert binding["session_key"] == "telegram:dm:208214988:thread:17585"
        assert binding["session_id"] == "topic-session"
        assert db.get_meta("telegram_dm_topic_schema_version") == "2"
        db.close()

    def test_telegram_topic_binding_refuses_to_relink_session_to_another_topic(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )
        db.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="topic-session",
        )

        with pytest.raises(ValueError, match="already linked"):
            db.bind_telegram_topic(
                chat_id="208214988",
                thread_id="99999",
                user_id="208214988",
                session_key="key-99999",
                session_id="topic-session",
            )
        db.close()

    def test_list_unlinked_telegram_sessions_for_user_excludes_bound_and_other_users(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            session_id="old-unlinked",
            source="telegram",
            user_id="208214988",
        )
        db.set_session_title("old-unlinked", "Old research")
        db.append_message("old-unlinked", "user", "first prompt")
        db.create_session(
            session_id="already-linked",
            source="telegram",
            user_id="208214988",
        )
        db.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="already-linked",
        )
        db.create_session(
            session_id="other-user",
            source="telegram",
            user_id="someone-else",
        )

        sessions = db.list_unlinked_telegram_sessions_for_user(
            chat_id="208214988",
            user_id="208214988",
        )

        assert [s["id"] for s in sessions] == ["old-unlinked"]
        assert sessions[0]["title"] == "Old research"
        assert sessions[0]["preview"] == "first prompt"
        db.close()

    def test_migration_from_v2(self, tmp_path):
        """Simulate a v2 database and verify migration adds title column."""
        import sqlite3

        db_path = tmp_path / "migrate_test.db"
        conn = sqlite3.connect(str(db_path))
        # Create v2 schema (without title column)
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (2);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT
            );
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("existing", "cli", 1000.0),
        )
        conn.commit()
        conn.close()

        # Open with SessionDB — should migrate to v9
        migrated_db = SessionDB(db_path=db_path)

        # Verify migration
        cursor = migrated_db._conn.execute("SELECT version FROM schema_version")
        assert cursor.fetchone()[0] == 11

        # Verify title column exists and is NULL for existing sessions
        session = migrated_db.get_session("existing")
        assert session is not None
        assert session["title"] is None

        # Verify api_call_count column was added with default 0
        cursor = migrated_db._conn.execute(
            "SELECT api_call_count FROM sessions WHERE id = 'existing'"
        )
        assert cursor.fetchone()[0] == 0

        # Verify we can set title on migrated session
        assert migrated_db.set_session_title("existing", "Migrated Title") is True
        session = migrated_db.get_session("existing")
        assert session["title"] == "Migrated Title"

        migrated_db.close()

    def test_reconciliation_adds_missing_columns(self, tmp_path):
        """Columns present in SCHEMA_SQL but missing from the live table
        are added by _reconcile_columns regardless of schema_version.

        Regression test: commit a7d78d3b inserted a new v7 migration
        (reasoning_content) and renumbered the old v7 (api_call_count)
        to v8.  Users already at the old v7 had schema_version >= 7,
        so the new v7 block was skipped and reasoning_content was never
        created — causing 'no such column' on /continue.
        """
        import sqlite3

        db_path = tmp_path / "gap_test.db"
        conn = sqlite3.connect(str(db_path))
        # Simulate the old v7 state: api_call_count exists, reasoning_content does NOT
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (7);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
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
                api_call_count INTEGER DEFAULT 0
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT
            );
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "cli", 1000.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "assistant", "hello", 1001.0),
        )
        conn.commit()
        # Verify reasoning_content is absent
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "reasoning_content" not in cols
        conn.close()

        # Open with SessionDB — reconciliation should add the missing column
        migrated_db = SessionDB(db_path=db_path)

        msg_cols = {
            r[1]
            for r in migrated_db._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert "reasoning_content" in msg_cols

        # The query that used to crash must now work
        cursor = migrated_db._conn.execute(
            "SELECT role, content, reasoning, reasoning_content, "
            "reasoning_details, codex_reasoning_items "
            "FROM messages WHERE session_id = ?",
            ("s1",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "assistant"
        assert row[3] is None  # reasoning_content NULL for old rows

        migrated_db.close()

    def test_reconciliation_is_idempotent(self, tmp_path):
        """Opening the same database twice doesn't error or duplicate columns."""
        db_path = tmp_path / "idempotent.db"
        db1 = SessionDB(db_path=db_path)
        cols1 = {r[1] for r in db1._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db1.close()

        db2 = SessionDB(db_path=db_path)
        cols2 = {r[1] for r in db2._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db2.close()

        assert cols1 == cols2

    def test_schema_sql_is_source_of_truth(self, db):
        """Every column in SCHEMA_SQL exists in the live database.

        This is the architectural invariant: SCHEMA_SQL declares the
        desired schema, _reconcile_columns ensures it matches reality.
        """
        from hermes_state import SCHEMA_SQL

        expected = SessionDB._parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            live_cols = {
                r[1]
                for r in db._conn.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }
            for col_name in declared_cols:
                assert col_name in live_cols, (
                    f"Column {col_name} declared in SCHEMA_SQL for {table_name} "
                    f"but missing from live DB. Live columns: {live_cols}"
                )
