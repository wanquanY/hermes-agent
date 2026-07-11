"""Session schema, lifecycle maintenance, and listing contracts."""

import sqlite3
import time

import pytest

from hermes_agent.domain.session_service import SessionService
from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_agent.storage.migration_operations import parse_schema_columns
from hermes_agent.storage.migrations import CURRENT_SCHEMA_VERSION
from hermes_agent.storage.state_schema import SCHEMA_SQL


# =========================================================================
# Delete and export
# =========================================================================

class TestDeleteAndExport:
    def test_delete_session(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Hello")

        assert db.maintenance.delete_session("s1") is True
        assert db.sessions.get("s1") is None
        assert db.messages.count(session_id="s1") == 0

    def test_delete_branch_source_session_cleans_lineage_references(self, db):
        db.sessions.create("source", "tui")
        db.messages.append("source", role="user", content="branch from this")
        assistant_id = db.messages.append("source", role="assistant", content="answer")
        db.branches.branch_session(
            source_session_id="source",
            new_session_id="branch-1",
            branch_point={"message_id": str(assistant_id)},
            idempotency_key="branch-key-1",
        )

        assert db.maintenance.delete_session("source") is True

        branch = db.sessions.get("branch-1")
        branch_info = db.branches.get_session_branch_info("branch-1")
        branch_requests = db._conn.execute(
            "SELECT COUNT(*) FROM session_branch_requests"
        ).fetchone()[0]
        fk_errors = db._conn.execute("PRAGMA foreign_key_check").fetchall()

        assert db.sessions.get("source") is None
        assert branch is not None
        assert branch_info is not None
        assert branch_info["parent_session_id"] == ""
        assert branch_requests == 0
        assert fk_errors == []

    def test_delete_branch_result_session_cleans_lineage_and_idempotency(self, db):
        db.sessions.create("source", "tui")
        db.messages.append("source", role="user", content="branch from this")
        assistant_id = db.messages.append("source", role="assistant", content="answer")
        db.branches.branch_session(
            source_session_id="source",
            new_session_id="branch-1",
            branch_point={"message_id": str(assistant_id)},
            idempotency_key="branch-key-1",
        )

        assert db.maintenance.delete_session("branch-1") is True

        lineage_rows = db._conn.execute(
            "SELECT COUNT(*) FROM session_lineage WHERE session_id = ?",
            ("branch-1",),
        ).fetchone()[0]
        branch_requests = db._conn.execute(
            "SELECT COUNT(*) FROM session_branch_requests"
        ).fetchone()[0]
        fk_errors = db._conn.execute("PRAGMA foreign_key_check").fetchall()

        assert db.sessions.get("source") is not None
        assert db.sessions.get("branch-1") is None
        assert lineage_rows == 0
        assert branch_requests == 0
        assert fk_errors == []

    def test_delete_nonexistent(self, db):
        assert db.maintenance.delete_session("nope") is False

    def test_resolve_session_id_exact(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6ff", source="cli")
        assert db.sessions.resolve_id("20260315_092437_c9a6ff") == "20260315_092437_c9a6ff"

    def test_resolve_session_id_unique_prefix(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6ff", source="cli")
        assert db.sessions.resolve_id("20260315_092437_c9a6") == "20260315_092437_c9a6ff"

    def test_resolve_session_id_ambiguous_prefix_returns_none(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6aa", source="cli")
        db.sessions.create(session_id="20260315_092437_c9a6bb", source="cli")
        assert db.sessions.resolve_id("20260315_092437_c9a6") is None

    def test_resolve_session_id_escapes_like_wildcards(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6ff", source="cli")
        db.sessions.create(session_id="20260315X092437_c9a6ff", source="cli")
        assert db.sessions.resolve_id("20260315_092437") == "20260315_092437_c9a6ff"

    def test_export_session(self, db):
        db.sessions.create(session_id="s1", source="cli", model="test")
        db.messages.append("s1", role="user", content="Hello")
        db.messages.append("s1", role="assistant", content="Hi")

        export = db.sessions.export("s1")
        assert isinstance(export, dict)
        assert export["source"] == "cli"
        assert len(export["messages"]) == 2

    def test_export_nonexistent(self, db):
        assert db.sessions.export("nope") is None

    def test_export_all(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        db.messages.append("s1", role="user", content="A")

        exports = db.sessions.export_all()
        assert len(exports) == 2

    def test_export_all_with_source(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")

        exports = db.sessions.export_all(source="cli")
        assert len(exports) == 1
        assert exports[0]["source"] == "cli"


# =========================================================================
# Prune
# =========================================================================

class TestPruneSessions:
    def test_prune_old_ended_sessions(self, db):
        # Create and end an "old" session
        db.sessions.create(session_id="old", source="cli")
        db.sessions.end("old", reason="done")
        # Manually backdate started_at
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 100 * 86400, "old"),
        )
        db._conn.commit()

        # Create a recent session
        db.sessions.create(session_id="new", source="cli")

        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 1
        assert db.sessions.get("old") is None
        session = db.sessions.get("new")
        assert session is not None
        assert session["id"] == "new"

    def test_prune_skips_active_sessions(self, db):
        db.sessions.create(session_id="active", source="cli")
        # Backdate but don't end
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 200 * 86400, "active"),
        )
        db._conn.commit()

        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 0
        assert db.sessions.get("active") is not None

    def test_prune_with_source_filter(self, db):
        for sid, src in [("old_cli", "cli"), ("old_tg", "telegram")]:
            db.sessions.create(session_id=sid, source=src)
            db.sessions.end(sid, reason="done")
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 200 * 86400, sid),
            )
        db._conn.commit()

        pruned = db.maintenance.prune_sessions(older_than_days=90, source="cli")
        assert pruned == 1
        assert db.sessions.get("old_cli") is None
        assert db.sessions.get("old_tg") is not None

    def test_prune_with_multilevel_chain(self, db):
        """Pruning old sessions orphans newer children instead of crashing on FK."""
        old_ts = time.time() - 200 * 86400
        recent_ts = time.time() - 10 * 86400

        # Chain: A (old) -> B (old) -> C (recent) -> D (recent)
        db.sessions.create(session_id="A", source="cli")
        db.sessions.end("A", reason="compressed")
        db.sessions.create(session_id="B", source="cli", parent_session_id="A")
        db.sessions.end("B", reason="compressed")
        db.sessions.create(session_id="C", source="cli", parent_session_id="B")
        db.sessions.end("C", reason="compressed")
        db.sessions.create(session_id="D", source="cli", parent_session_id="C")
        db.sessions.end("D", reason="done")

        # Backdate A and B to be old; C and D stay recent
        for sid, ts in [("A", old_ts), ("B", old_ts), ("C", recent_ts), ("D", recent_ts)]:
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (ts, sid)
            )
        db._conn.commit()

        # Should not raise IntegrityError
        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 2  # only A and B
        assert db.sessions.get("A") is None
        assert db.sessions.get("B") is None
        # C and D survive, C is orphaned (parent_session_id NULL)
        c = db.sessions.get("C")
        assert c is not None
        assert c["parent_session_id"] is None
        d = db.sessions.get("D")
        assert d is not None
        assert d["parent_session_id"] == "C"

    def test_prune_entire_old_chain(self, db):
        """All sessions in a chain are old — entire chain is pruned."""
        old_ts = time.time() - 200 * 86400

        db.sessions.create(session_id="X", source="cli")
        db.sessions.end("X", reason="compressed")
        db.sessions.create(session_id="Y", source="cli", parent_session_id="X")
        db.sessions.end("Y", reason="compressed")
        db.sessions.create(session_id="Z", source="cli", parent_session_id="Y")
        db.sessions.end("Z", reason="done")

        for sid in ("X", "Y", "Z"):
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (old_ts, sid)
            )
        db._conn.commit()

        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 3
        for sid in ("X", "Y", "Z"):
            assert db.sessions.get(sid) is None


class TestDeleteSessionOrphansChildren:
    def test_delete_orphans_children(self, db):
        """Deleting a parent session orphans its children."""
        db.sessions.create(session_id="parent", source="cli")
        db.sessions.create(session_id="child", source="cli", parent_session_id="parent")
        db.sessions.create(session_id="grandchild", source="cli", parent_session_id="child")

        # Should not raise IntegrityError
        result = db.maintenance.delete_session("parent")
        assert result is True
        assert db.sessions.get("parent") is None
        # Child is orphaned, not deleted
        child = db.sessions.get("child")
        assert child is not None
        assert child["parent_session_id"] is None
        # Grandchild is untouched
        grandchild = db.sessions.get("grandchild")
        assert grandchild is not None
        assert grandchild["parent_session_id"] == "child"


def test_repair_orphaned_foreign_key_rows_removes_non_authoritative_dangling_rows(db):
    db._conn.execute("PRAGMA foreign_keys=OFF")
    db._conn.execute(
        """
        INSERT INTO session_lineage (
            session_id, parent_session_id, root_session_id,
            branch_origin, branch_mode, branch_depth, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "missing-branch",
            "missing-parent",
            "missing-root",
            "user_message_action",
            "materialized_prefix",
            1,
            time.time(),
        ),
    )
    db._conn.execute(
        """
        INSERT INTO session_branch_requests (
            idempotency_key, source_session_id, branch_fingerprint,
            result_session_id, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("branch-key-orphan", "missing-source", "fingerprint", "missing-result", time.time()),
    )
    db._conn.execute(
        """
        INSERT INTO team_capability_snapshot_bindings (
            binding_id, mission_id, conversation_id, snapshot_id,
            snapshot_version, source_digest, pinned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "team-capability-binding:missing",
            "missing-mission",
            "missing-conversation",
            "missing-snapshot",
            1,
            "digest",
            time.time(),
        ),
    )
    db._conn.execute("PRAGMA foreign_keys=ON")

    assert db._conn.execute("PRAGMA foreign_key_check").fetchall()

    repaired = db.maintenance.repair_orphaned_foreign_key_rows()

    assert repaired == 3
    assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []


# =========================================================================
# Schema and WAL mode
# =========================================================================

# =========================================================================
# Session title
# =========================================================================

class TestSessionTitle:
    def test_set_and_get_title(self, db):
        db.sessions.create(session_id="s1", source="cli")
        assert db.sessions.set_title("s1", "My Session") is True

        session = db.sessions.get("s1")
        assert session["title"] == "My Session"

    def test_set_title_nonexistent_session(self, db):
        assert db.sessions.set_title("nonexistent", "Title") is False

    def test_title_initially_none(self, db):
        db.sessions.create(session_id="s1", source="cli")
        session = db.sessions.get("s1")
        assert session["title"] is None

    def test_update_title(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "First Title")
        db.sessions.set_title("s1", "Updated Title")

        session = db.sessions.get("s1")
        assert session["title"] == "Updated Title"

    def test_title_in_search_sessions(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "Debugging Auth")
        db.sessions.create(session_id="s2", source="cli")

        sessions = db.sessions.search()
        titled = [s for s in sessions if s.get("title") == "Debugging Auth"]
        assert len(titled) == 1
        assert titled[0]["id"] == "s1"

    def test_title_in_export(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "Export Test")
        db.messages.append("s1", role="user", content="Hello")

        export = db.sessions.export("s1")
        assert export["title"] == "Export Test"

    def test_title_with_special_characters(self, db):
        db.sessions.create(session_id="s1", source="cli")
        title = "PR #438 — fixing the 'auth' middleware"
        db.sessions.set_title("s1", title)

        session = db.sessions.get("s1")
        assert session["title"] == title

    def test_title_empty_string_normalized_to_none(self, db):
        """Empty strings are normalized to None (clearing the title)."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "My Title")
        # Setting to empty string should clear the title (normalize to None)
        db.sessions.set_title("s1", "")

        session = db.sessions.get("s1")
        assert session["title"] is None

    def test_multiple_empty_titles_no_conflict(self, db):
        """Multiple sessions can have empty-string (normalized to NULL) titles."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="cli")
        db.sessions.set_title("s1", "")
        db.sessions.set_title("s2", "")
        # Both should be None, no uniqueness conflict
        assert db.sessions.get("s1")["title"] is None
        assert db.sessions.get("s2")["title"] is None

    def test_title_survives_end_session(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "Before End")
        db.sessions.end("s1", reason="user_exit")

        session = db.sessions.get("s1")
        assert session["title"] == "Before End"
        assert session["ended_at"] is not None


class TestSanitizeTitle:
    """Tests for SessionService.sanitize_title() validation and cleaning."""

    def test_normal_title_unchanged(self):
        assert SessionService.sanitize_title("My Project") == "My Project"

    def test_strips_whitespace(self):
        assert SessionService.sanitize_title("  hello world  ") == "hello world"

    def test_collapses_internal_whitespace(self):
        assert SessionService.sanitize_title("hello   world") == "hello world"

    def test_tabs_and_newlines_collapsed(self):
        assert SessionService.sanitize_title("hello\t\nworld") == "hello world"

    def test_none_returns_none(self):
        assert SessionService.sanitize_title(None) is None

    def test_empty_string_returns_none(self):
        assert SessionService.sanitize_title("") is None

    def test_whitespace_only_returns_none(self):
        assert SessionService.sanitize_title("   \t\n  ") is None

    def test_control_chars_stripped(self):
        # Null byte, bell, backspace, etc.
        assert SessionService.sanitize_title("hello\x00world") == "helloworld"
        assert SessionService.sanitize_title("\x07\x08test\x1b") == "test"

    def test_del_char_stripped(self):
        assert SessionService.sanitize_title("hello\x7fworld") == "helloworld"

    def test_zero_width_chars_stripped(self):
        # Zero-width space (U+200B), zero-width joiner (U+200D)
        assert SessionService.sanitize_title("hello\u200bworld") == "helloworld"
        assert SessionService.sanitize_title("hello\u200dworld") == "helloworld"

    def test_rtl_override_stripped(self):
        # Right-to-left override (U+202E) — used in filename spoofing attacks
        assert SessionService.sanitize_title("hello\u202eworld") == "helloworld"

    def test_bom_stripped(self):
        # Byte order mark (U+FEFF)
        assert SessionService.sanitize_title("\ufeffhello") == "hello"

    def test_only_control_chars_returns_none(self):
        assert SessionService.sanitize_title("\x00\x01\x02\u200b\ufeff") is None

    def test_max_length_allowed(self):
        title = "A" * 100
        assert SessionService.sanitize_title(title) == title

    def test_exceeds_max_length_raises(self):
        title = "A" * 101
        with pytest.raises(ValueError, match="too long"):
            SessionService.sanitize_title(title)

    def test_unicode_emoji_allowed(self):
        assert SessionService.sanitize_title("🚀 My Project 🎉") == "🚀 My Project 🎉"

    def test_cjk_characters_allowed(self):
        assert SessionService.sanitize_title("我的项目") == "我的项目"

    def test_accented_characters_allowed(self):
        assert SessionService.sanitize_title("Résumé éditing") == "Résumé éditing"

    def test_special_punctuation_allowed(self):
        title = "PR #438 — fixing the 'auth' middleware"
        assert SessionService.sanitize_title(title) == title

    def test_sanitize_applied_in_set_session_title(self, db):
        """set_session_title applies sanitize_title internally."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "  hello\x00  world  ")
        assert db.sessions.get("s1")["title"] == "hello world"

    def test_too_long_title_rejected_by_set(self, db):
        """set_session_title raises ValueError for overly long titles."""
        db.sessions.create("s1", "cli")
        with pytest.raises(ValueError, match="too long"):
            db.sessions.set_title("s1", "X" * 150)


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

    def test_schema_version(self, db):
        cursor = db._conn.execute("SELECT version FROM schema_version")
        version = cursor.fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION

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

        db = open_cli_session_store(db_path=old_db)
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

        db = open_cli_session_store(db_path=old_db)
        db.telegram_topics.apply_telegram_topic_migration()

        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "telegram_dm_topic_mode" in tables
        assert "telegram_dm_topic_bindings" in tables
        assert db.metadata.get("telegram_dm_topic_schema_version") == "2"
        db.close()

    def test_telegram_topic_binding_roundtrip_requires_explicit_schema(self, tmp_path):
        db = open_cli_session_store(db_path=tmp_path / "state.db")
        db.sessions.create(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )

        assert db.telegram_topics.get_telegram_topic_binding(chat_id="208214988", thread_id="17585") is None

        db.telegram_topics.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="telegram:dm:208214988:thread:17585",
            session_id="topic-session",
        )

        binding = db.telegram_topics.get_telegram_topic_binding(chat_id="208214988", thread_id="17585")
        assert binding is not None
        assert binding["chat_id"] == "208214988"
        assert binding["thread_id"] == "17585"
        assert binding["user_id"] == "208214988"
        assert binding["session_key"] == "telegram:dm:208214988:thread:17585"
        assert binding["session_id"] == "topic-session"
        assert db.metadata.get("telegram_dm_topic_schema_version") == "2"
        db.close()

    def test_telegram_topic_binding_refuses_to_relink_session_to_another_topic(self, tmp_path):
        db = open_cli_session_store(db_path=tmp_path / "state.db")
        db.sessions.create(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )
        db.telegram_topics.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="topic-session",
        )

        with pytest.raises(ValueError, match="already linked"):
            db.telegram_topics.bind_telegram_topic(
                chat_id="208214988",
                thread_id="99999",
                user_id="208214988",
                session_key="key-99999",
                session_id="topic-session",
            )
        db.close()

    def test_list_unlinked_telegram_sessions_for_user_excludes_bound_and_other_users(self, tmp_path):
        db = open_cli_session_store(db_path=tmp_path / "state.db")
        db.sessions.create(
            session_id="old-unlinked",
            source="telegram",
            user_id="208214988",
        )
        db.sessions.set_title("old-unlinked", "Old research")
        db.messages.append("old-unlinked", "user", "first prompt")
        db.sessions.create(
            session_id="already-linked",
            source="telegram",
            user_id="208214988",
        )
        db.telegram_topics.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="already-linked",
        )
        db.sessions.create(
            session_id="other-user",
            source="telegram",
            user_id="someone-else",
        )

        sessions = db.telegram_topics.list_unlinked_telegram_sessions_for_user(
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

        # Open with CliSessionStore — should migrate to v9
        migrated_db = open_cli_session_store(db_path=db_path)

        # Verify migration
        cursor = migrated_db._conn.execute("SELECT version FROM schema_version")
        assert cursor.fetchone()[0] == CURRENT_SCHEMA_VERSION

        # Verify title column exists and is NULL for existing sessions
        session = migrated_db.sessions.get("existing")
        assert session is not None
        assert session["title"] is None

        # Verify api_call_count column was added with default 0
        cursor = migrated_db._conn.execute(
            "SELECT api_call_count FROM sessions WHERE id = 'existing'"
        )
        assert cursor.fetchone()[0] == 0

        # Verify we can set title on migrated session
        assert migrated_db.sessions.set_title("existing", "Migrated Title") is True
        session = migrated_db.sessions.get("existing")
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

        # Open with CliSessionStore — reconciliation should add the missing column
        migrated_db = open_cli_session_store(db_path=db_path)

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

    def test_reconciliation_defers_indexes_that_reference_new_columns(self, tmp_path):
        """Indexes that reference newly declared columns must run after reconcile.

        Team mission conversations added team_missions.conversation_id after
        the original team_missions table shipped. Keeping the index in
        SCHEMA_SQL made existing databases fail during executescript() before
        _reconcile_columns() could add the missing column.
        """
        import sqlite3

        db_path = tmp_path / "team_mission_old_schema.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (16);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            );

            CREATE TABLE team_missions (
                mission_id TEXT PRIMARY KEY,
                team_id TEXT,
                title TEXT NOT NULL,
                objective TEXT,
                workspace_id TEXT,
                workspace_path TEXT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                leader_session_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                metadata_json TEXT
            );
        """)
        conn.execute(
            """
            INSERT INTO team_missions (
                mission_id, team_id, title, mode, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("mission-1", "team-1", "Legacy mission", "supervised", "completed", 1.0, 1.0),
        )
        conn.commit()
        conn.close()

        migrated_db = open_cli_session_store(db_path=db_path)

        mission_cols = {
            r[1]
            for r in migrated_db._conn.execute(
                "PRAGMA table_info(team_missions)"
            ).fetchall()
        }
        assert "conversation_id" in mission_cols

        indexes = {
            row[1]
            for row in migrated_db._conn.execute(
                "PRAGMA index_list(team_missions)"
            ).fetchall()
        }
        assert "idx_team_missions_conversation" in indexes

        cursor = migrated_db._conn.execute(
            """
            SELECT mission_id
            FROM team_missions
            WHERE conversation_id IS NULL
            ORDER BY updated_at DESC
            """
        )
        assert cursor.fetchone()[0] == "mission-1"

        migrated_db.close()

    def test_reconciliation_is_idempotent(self, tmp_path):
        """Opening the same database twice doesn't error or duplicate columns."""
        db_path = tmp_path / "idempotent.db"
        db1 = open_cli_session_store(db_path=db_path)
        cols1 = {r[1] for r in db1._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db1.close()

        db2 = open_cli_session_store(db_path=db_path)
        cols2 = {r[1] for r in db2._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db2.close()

        assert cols1 == cols2

    def test_schema_sql_is_source_of_truth(self, db):
        """Every column in SCHEMA_SQL exists in the live database.

        This is the architectural invariant: SCHEMA_SQL declares the
        desired schema, _reconcile_columns ensures it matches reality.
        """

        expected = parse_schema_columns(SCHEMA_SQL)
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


class TestTitleUniqueness:
    """Tests for unique title enforcement and title-based lookups."""

    def test_duplicate_title_raises(self, db):
        """Setting a title already used by another session raises ValueError."""
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s1", "my project")
        with pytest.raises(ValueError, match="already in use"):
            db.sessions.set_title("s2", "my project")

    def test_same_session_can_keep_title(self, db):
        """A session can re-set its own title without error."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        # Should not raise — it's the same session
        assert db.sessions.set_title("s1", "my project") is True

    def test_null_titles_not_unique(self, db):
        """Multiple sessions can have NULL titles (no constraint violation)."""
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "cli")
        # Both have NULL titles — no error
        assert db.sessions.get("s1")["title"] is None
        assert db.sessions.get("s2")["title"] is None

    def test_get_session_by_title(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "refactoring auth")
        result = db.sessions.get_by_title("refactoring auth")
        assert result is not None
        assert result["id"] == "s1"

    def test_get_session_by_title_not_found(self, db):
        assert db.sessions.get_by_title("nonexistent") is None

    def test_get_session_title(self, db):
        db.sessions.create("s1", "cli")
        assert db.sessions.get_title("s1") is None
        db.sessions.set_title("s1", "my title")
        assert db.sessions.get_title("s1") == "my title"

    def test_get_session_title_nonexistent(self, db):
        assert db.sessions.get_title("nonexistent") is None


class TestTitleLineage:
    """Tests for title lineage resolution and auto-numbering."""

    def test_resolve_exact_title(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        assert db.sessions.resolve_by_title("my project") == "s1"

    def test_resolve_returns_latest_numbered(self, db):
        """When numbered variants exist, return the most recent one."""
        import time
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        time.sleep(0.01)
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        time.sleep(0.01)
        db.sessions.create("s3", "cli")
        db.sessions.set_title("s3", "my project #3")
        # Resolving "my project" should return s3 (latest numbered variant)
        assert db.sessions.resolve_by_title("my project") == "s3"

    def test_resolve_exact_numbered(self, db):
        """Resolving an exact numbered title returns that specific session."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        # Resolving "my project #2" exactly should return s2
        assert db.sessions.resolve_by_title("my project #2") == "s2"

    def test_resolve_nonexistent_title(self, db):
        assert db.sessions.resolve_by_title("nonexistent") is None

    def test_next_title_no_existing(self, db):
        """With no existing sessions, base title is returned as-is."""
        assert db.sessions.next_title_in_lineage("my project") == "my project"

    def test_next_title_first_continuation(self, db):
        """First continuation after the original gets #2."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        assert db.sessions.next_title_in_lineage("my project") == "my project #2"

    def test_next_title_increments(self, db):
        """Each continuation increments the number."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        db.sessions.create("s3", "cli")
        db.sessions.set_title("s3", "my project #3")
        assert db.sessions.next_title_in_lineage("my project") == "my project #4"

    def test_next_title_strips_existing_number(self, db):
        """Passing a numbered title strips the number and finds the base."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        # Even when called with "my project #2", it should return #3
        assert db.sessions.next_title_in_lineage("my project #2") == "my project #3"


class TestTitleSqlWildcards:
    """Titles containing SQL LIKE wildcards (%, _) must not cause false matches."""

    def test_resolve_title_with_underscore(self, db):
        """A title like 'test_project' should not match 'testXproject #2'."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "test_project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "testXproject #2")
        # Resolving "test_project" should return s1 (exact), not s2
        assert db.sessions.resolve_by_title("test_project") == "s1"

    def test_resolve_title_with_percent(self, db):
        """A title with '%' should not wildcard-match unrelated sessions."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "100% done")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "100X done #2")
        # Should resolve to s1 (exact), not s2
        assert db.sessions.resolve_by_title("100% done") == "s1"

    def test_next_lineage_with_underscore(self, db):
        """get_next_title_in_lineage with underscores doesn't match wrong sessions."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "test_project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "testXproject #2")
        # Only "test_project" exists, so next should be "test_project #2"
        assert db.sessions.next_title_in_lineage("test_project") == "test_project #2"


class TestListSessionsRich:
    """Tests for enhanced session listing with preview and last_active."""

    def test_preview_from_first_user_message(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "system", "You are a helpful assistant.")
        db.messages.append("s1", "user", "Help me refactor the auth module please")
        db.messages.append("s1", "assistant", "Sure, let me look at it.")
        sessions = db.sessions.list_rich()
        assert len(sessions) == 1
        assert "Help me refactor the auth module" in sessions[0]["preview"]

    def test_preview_truncated_at_60(self, db):
        db.sessions.create("s1", "cli")
        long_msg = "A" * 100
        db.messages.append("s1", "user", long_msg)
        sessions = db.sessions.list_rich()
        assert len(sessions[0]["preview"]) == 63  # 60 chars + "..."
        assert sessions[0]["preview"].endswith("...")

    def test_preview_empty_when_no_user_messages(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "system", "System prompt")
        sessions = db.sessions.list_rich()
        assert sessions[0]["preview"] == ""

    def test_last_active_from_latest_message(self, db):
        import time
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Hello")
        time.sleep(0.01)
        db.messages.append("s1", "assistant", "Hi there!")
        sessions = db.sessions.list_rich()
        # last_active should be close to now (the assistant message)
        assert sessions[0]["last_active"] > sessions[0]["started_at"]

    def test_last_active_fallback_to_started_at(self, db):
        db.sessions.create("s1", "cli")
        sessions = db.sessions.list_rich()
        # No messages, so last_active falls back to started_at
        assert sessions[0]["last_active"] == sessions[0]["started_at"]

    def test_list_sessions_rich_reads_summary_not_transcript_rows(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "first summary")
        db.sessions.create("s2", "cli")
        db.messages.append("s2", "user", "second summary")

        statements = []
        with db._lock:
            db._conn.set_trace_callback(statements.append)
        try:
            sessions = db.sessions.list_rich(limit=2, order_by_last_active=True)
        finally:
            with db._lock:
                db._conn.set_trace_callback(None)

        assert [session["id"] for session in sessions] == ["s2", "s1"]
        traced_sql = "\n".join(statements).lower()
        assert "from messages" not in traced_sql
        assert "join messages" not in traced_sql

    def test_order_by_last_active_surfaces_recently_touched_older_session_first(self, db):
        t0 = 1709500000.0
        db.sessions.create("old", "cli")
        db.sessions.create("new", "cli")

        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "old"))
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 10, "new"))

        db.messages.append("old", "user", "old first")
        db.messages.append("new", "user", "new first")
        db.messages.append("old", "assistant", "old touched later")

        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 1, "old", "user", "old first"),
            )
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 11, "new", "user", "new first"),
            )
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 20, "old", "assistant", "old touched later"),
            )
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0 + 20, "old"),
            )
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0 + 11, "new"),
            )
            db._conn.commit()

        assert [s["id"] for s in db.sessions.list_rich(limit=5)] == ["new", "old"]
        assert [
            s["id"] for s in db.sessions.list_rich(limit=5, order_by_last_active=True)
        ] == ["old", "new"]

    def test_page_cursor_paginates_order_by_last_active(self, db):
        t0 = 1709500000.0
        rows = [
            ("old-active", t0, t0 + 30),
            ("mid-active", t0 + 10, t0 + 20),
            ("new-start", t0 + 20, t0 + 10),
        ]
        for session_id, started_at, message_ts in rows:
            db.sessions.create(session_id, "cli")
            with db._lock:
                db._conn.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?",
                    (started_at, session_id),
                )
            db.messages.append(session_id, "user", session_id)
            with db._lock:
                db._conn.execute(
                    "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                    (message_ts, session_id, session_id),
                )
                db._conn.execute(
                    "UPDATE sessions SET last_active=? WHERE id=?",
                    (message_ts, session_id),
                )
                db._conn.commit()

        first_page = db.sessions.list_rich(limit=2, order_by_last_active=True)
        assert [s["id"] for s in first_page] == ["old-active", "mid-active"]
        assert first_page[-1]["_page_cursor"] == {
            "effective_last_active": t0 + 20,
            "started_at": t0 + 10,
            "id": "mid-active",
        }

        second_page = db.sessions.list_rich(
            limit=2,
            order_by_last_active=True,
            page_cursor=first_page[-1]["_page_cursor"],
        )
        assert [s["id"] for s in second_page] == ["new-start"]

    def test_page_cursor_paginates_started_at_order(self, db):
        t0 = 1709500000.0
        for index, session_id in enumerate(("s1", "s2", "s3")):
            db.sessions.create(session_id, "cli")
            with db._lock:
                db._conn.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?",
                    (t0 + index, session_id),
                )
                db._conn.commit()

        first_page = db.sessions.list_rich(limit=2)
        assert [s["id"] for s in first_page] == ["s3", "s2"]

        second_page = db.sessions.list_rich(
            limit=2,
            page_cursor=first_page[-1]["_page_cursor"],
        )
        assert [s["id"] for s in second_page] == ["s1"]

    def test_order_by_last_active_uses_compression_tip_activity(self, db):
        """A compression root whose tip was touched recently must rank above
        a newer uncompressed session, even when that tip activity lives in a
        different row and the outer LIMIT could otherwise cut it.

        This is the case that forced SQL-level chain walking: a naive "cap
        the SQL fetch at limit*K" optimization would drop the old root off
        the SQL page before post-projection could promote it.
        """
        t0 = 1709500000.0
        db.sessions.create("root1", "cli")
        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root1"))
            db._conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                (t0 + 100, "compression", "root1"),
            )
        db.messages.append("root1", "user", "old ask")
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0, "root1"),
            )
            db._conn.commit()

        # Continuation tip created after root ended; last activity much later.
        db.sessions.create("tip1", "cli", parent_session_id="root1")
        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 101, "tip1"))
        db.messages.append("tip1", "user", "latest message")

        # Bunch of newer, uncompressed sessions — fresher start_at but older
        # last activity than the tip. Explicitly pin message timestamps so
        # they don't pick up wall-clock from append_message.
        for i in range(5):
            sid = f"newer{i}"
            db.sessions.create(sid, "cli")
            with db._lock:
                db._conn.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?",
                    (t0 + 500 + i, sid),
                )
            db.messages.append(sid, "user", f"msg {i}")
            with db._lock:
                db._conn.execute(
                    "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                    (t0 + 500 + i, sid, f"msg {i}"),
                )
                db._conn.execute(
                    "UPDATE sessions SET last_active=? WHERE id=?",
                    (t0 + 500 + i, sid),
                )

        # Tip activity timestamp is the latest thing in the DB.
        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                (t0 + 10_000, "tip1", "latest message"),
            )
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0 + 10_000, "tip1"),
            )
            db._conn.commit()

        # limit=1 is the stress test: the old root must win the single slot.
        top = db.sessions.list_rich(limit=1, order_by_last_active=True)
        assert len(top) == 1
        # Projection surfaces the tip's id in the root's slot.
        assert top[0]["id"] == "tip1"
        assert top[0]["_lineage_root_id"] == "root1"

    def test_rich_list_includes_title(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "refactoring auth")
        sessions = db.sessions.list_rich()
        assert sessions[0]["title"] == "refactoring auth"

    def test_rich_list_source_filter(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "telegram")
        sessions = db.sessions.list_rich(source="cli")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"

    def test_preview_newlines_collapsed(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Line one\nLine two\nLine three")
        sessions = db.sessions.list_rich()
        assert "\n" not in sessions[0]["preview"]
        assert "Line one Line two" in sessions[0]["preview"]

    def test_branch_session_visible_in_list(self, db):
        """Branch sessions (parent ended with 'branched') must appear in list_sessions_rich."""
        db.sessions.create("parent", "cli")
        db.sessions.end("parent", "branched")
        db.sessions.create("branch", "cli", parent_session_id="parent")
        db.messages.append("branch", "user", "Exploring the alternative approach")

        sessions = db.sessions.list_rich()
        ids = [s["id"] for s in sessions]
        assert "branch" in ids, "Branch session should be visible in default list"

    def test_subagent_session_still_hidden(self, db):
        """Sub-agent children (parent NOT ended with 'branched') remain hidden."""
        db.sessions.create("root", "cli")
        db.sessions.create("delegate", "cli", parent_session_id="root")

        sessions = db.sessions.list_rich()
        ids = [s["id"] for s in sessions]
        assert "delegate" not in ids, "Delegate sub-agent should not appear in default list"
        assert "root" in ids

    def test_compression_child_still_hidden(self, db):
        """Compression continuation sessions remain hidden (parent ended with 'compression')."""
        import time as _time
        t0 = _time.time()
        db.sessions.create("root", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
            (t0 + 1800, "root"),
        )
        db._conn.commit()
        db.sessions.create("continuation", "cli", parent_session_id="root")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?", (t0 + 1801, "continuation")
        )
        db._conn.commit()

        sessions = db.sessions.list_rich(project_compression_tips=False)
        ids = [s["id"] for s in sessions]
        assert "continuation" not in ids, "Compression continuation should stay hidden"


class TestCompressionChainProjection:
    """Tests for lineage-aware list_sessions_rich — compressed conversations
    surface as their live continuation tip, not the dead parent root.
    """

    def _build_compression_chain(self, db, t0: float):
        """Helper: builds root -> delegate -> compression-child -> tip chain.

        Returns (root_id, delegate_id, mid_id, tip_id).
        """
        import time as _time
        # Root that gets compressed
        db.sessions.create("root1", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root1"))
        db.messages.append("root1", "user", "help me refactor auth")

        # Delegate subagent spawned while root1 was live (before it ended)
        db.sessions.create("delegate1", "cli", parent_session_id="root1")
        db._conn.execute(
            "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
            (t0 + 600, t0 + 650, "delegate1"),
        )
        db.messages.append("delegate1", "user", "delegate task")

        # root1 compressed at t0+1800
        t_compress_root = t0 + 1800
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t_compress_root, "compression", "root1"),
        )

        # Continuation mid created 1s after parent ended
        db.sessions.create("mid1", "cli", parent_session_id="root1")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?",
            (t_compress_root + 1, "mid1"),
        )
        db.messages.append("mid1", "user", "continuing")

        # mid1 also compressed
        t_compress_mid = t_compress_root + 1800
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t_compress_mid, "compression", "mid1"),
        )

        # Tip — latest continuation
        db.sessions.create("tip1", "cli", parent_session_id="mid1")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?",
            (t_compress_mid + 1, "tip1"),
        )
        db.messages.append("tip1", "user", "latest message")

        db._conn.commit()
        return ("root1", "delegate1", "mid1", "tip1")

    def test_get_compression_tip_walks_full_chain(self, db):
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        assert db.sessions.compression_tip("root1") == "tip1"
        assert db.sessions.compression_tip("mid1") == "tip1"
        assert db.sessions.compression_tip("tip1") == "tip1"

    def test_get_compression_tip_returns_self_for_uncompressed(self, db):
        db.sessions.create("solo", "cli")
        assert db.sessions.compression_tip("solo") == "solo"

    def test_get_compression_tip_skips_delegate_children(self, db):
        """Delegate subagents have parent_session_id set but were created
        BEFORE the parent ended. They must not be followed as compression
        continuations — the started_at >= ended_at guard handles this.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        # delegate1 is a child of root1 but NOT a compression continuation.
        # root1's tip must be tip1 (via mid1), not delegate1.
        assert db.sessions.compression_tip("root1") == "tip1"

    def test_list_surfaces_tip_for_compressed_root(self, db):
        """The list must show the tip's id/message_count/preview in place of
        the root row, so users can see and resume the live conversation.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        # Add an uncompressed root for comparison.
        db.sessions.create("solo", "cli")
        db.messages.append("solo", "user", "standalone")
        db._conn.commit()

        sessions = db.sessions.list_rich(source="cli", limit=20)
        ids = [s["id"] for s in sessions]
        # Only top-level conversations appear: tip1 (projected from root1) + solo.
        # Delegate children, mid1, and the dead root1 must NOT be in the list.
        assert "tip1" in ids
        assert "solo" in ids
        assert "root1" not in ids
        assert "mid1" not in ids
        assert "delegate1" not in ids

        tip_row = next(s for s in sessions if s["id"] == "tip1")
        # The row surfaces the tip's identity but preserves the root's start
        # timestamp for stable ordering and lineage tracking.
        assert tip_row["_lineage_root_id"] == "root1"
        assert tip_row["preview"].startswith("latest message")
        assert tip_row["ended_at"] is None  # tip is still live
        assert tip_row["end_reason"] is None

    def test_list_without_projection_returns_raw_root(self, db):
        """project_compression_tips=False returns the raw parent-NULL root
        rows — useful for admin/debug UIs.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        sessions = db.sessions.list_rich(
            source="cli", limit=20, project_compression_tips=False
        )
        ids = [s["id"] for s in sessions]
        assert "root1" in ids
        assert "tip1" not in ids

        root_row = next(s for s in sessions if s["id"] == "root1")
        assert root_row["end_reason"] == "compression"
        assert "_lineage_root_id" not in root_row

    def test_list_preserves_sort_by_started_at(self, db):
        """Chronological ordering uses the ROOT's started_at (conversation
        start), not the tip's. This keeps lineage entries stable in the list
        even as new compressions push the tip forward in time.
        """
        import time as _time
        t0 = _time.time() - 3600
        self._build_compression_chain(db, t0)

        # Create a newer standalone session that should sort above the lineage
        # if we used tip.started_at, but below if we correctly use root.started_at.
        t_between = t0 + 120  # between root1 and its compression
        db.sessions.create("newer", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t_between, "newer"))
        db.messages.append("newer", "user", "newer session started after root1")
        db._conn.commit()

        sessions = db.sessions.list_rich(source="cli", limit=20)
        ids_in_order = [s["id"] for s in sessions]
        # 'newer' started AFTER root1 but BEFORE tip1's actual started_at.
        # Correct ordering (by root started_at): newer > tip1's lineage entry.
        assert ids_in_order.index("newer") < ids_in_order.index("tip1")

    def test_list_handles_broken_chain_gracefully(self, db):
        """A compression root with no child (e.g. DB corruption or a partial
        end_session call that didn't finish creating the child) must not
        crash the list — it should fall back to surfacing the root as-is.
        """
        import time as _time
        t0 = _time.time() - 100
        db.sessions.create("orphan", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "orphan"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t0 + 10, "compression", "orphan"),
        )
        db._conn.commit()

        sessions = db.sessions.list_rich(source="cli", limit=10)
        ids = [s["id"] for s in sessions]
        assert "orphan" in ids
        row = next(s for s in sessions if s["id"] == "orphan")
        # No tip means no projection — row stays raw.
        assert "_lineage_root_id" not in row
        assert row["end_reason"] == "compression"
