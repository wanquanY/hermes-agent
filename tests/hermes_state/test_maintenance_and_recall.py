"""Maintenance, indexing, rewind, and recall contracts."""

import time

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_agent.composition.migrations import CURRENT_SCHEMA_VERSION
from tests.hermes_state.support import business_payload


# =========================================================================
# Session source exclusion (--source flag for third-party isolation)
# =========================================================================

class TestExcludeSources:
    """Tests for exclude_sources on list_sessions_rich and search_messages."""

    def test_list_sessions_rich_excludes_tool_source(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        db.sessions.create("s3", "telegram")
        sessions = db.sessions.list_rich(exclude_sources=["tool"])
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s3" in ids
        assert "s2" not in ids

    def test_list_sessions_rich_no_exclusion_returns_all(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        sessions = db.sessions.list_rich()
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s2" in ids

    def test_list_sessions_rich_source_and_exclude_combined(self, db):
        """When source= is explicit, exclude_sources should not conflict."""
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        db.sessions.create("s3", "telegram")
        # Explicit source filter: only tool sessions, no exclusion
        sessions = db.sessions.list_rich(source="tool")
        ids = [s["id"] for s in sessions]
        assert ids == ["s2"]

    def test_list_sessions_rich_exclude_multiple_sources(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        db.sessions.create("s3", "cron")
        db.sessions.create("s4", "telegram")
        sessions = db.sessions.list_rich(exclude_sources=["tool", "cron"])
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s4" in ids
        assert "s2" not in ids
        assert "s3" not in ids

    def test_search_messages_excludes_tool_source(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Python deployment question")
        db.sessions.create("s2", "tool")
        db.messages.append("s2", "user", "Python automated question")
        results = db.messages.search("Python", exclude_sources=["tool"])
        sources = [r["source"] for r in results]
        assert "cli" in sources
        assert "tool" not in sources

    def test_search_messages_no_exclusion_returns_all_sources(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Rust deployment question")
        db.sessions.create("s2", "tool")
        db.messages.append("s2", "user", "Rust automated question")
        results = db.messages.search("Rust")
        sources = [r["source"] for r in results]
        assert "cli" in sources
        assert "tool" in sources

    def test_search_messages_source_include_and_exclude(self, db):
        """source_filter (include) and exclude_sources can coexist."""
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Golang test")
        db.sessions.create("s2", "telegram")
        db.messages.append("s2", "user", "Golang test")
        db.sessions.create("s3", "tool")
        db.messages.append("s3", "user", "Golang test")
        # Include cli+tool, but exclude tool → should only return cli
        results = db.messages.search(
            "Golang", source_filter=["cli", "tool"], exclude_sources=["tool"]
        )
        sources = [r["source"] for r in results]
        assert sources == ["cli"]


class TestResolveSessionByNameOrId:
    """Tests for the main.py helper that resolves names or IDs."""

    def test_resolve_by_id(self, db):
        db.sessions.create("test-id-123", "cli")
        session = db.sessions.get("test-id-123")
        assert session is not None
        assert session["id"] == "test-id-123"

    def test_resolve_by_title_falls_back(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        result = db.sessions.resolve_by_title("my project")
        assert result == "s1"


# =========================================================================
# Concurrent write safety / lock contention fixes (#3139)
# =========================================================================

class TestConcurrentWriteSafety:
    def test_create_session_insert_or_ignore_is_idempotent(self, db):
        """create_session with the same ID twice must not raise (INSERT OR IGNORE)."""
        db.sessions.create(session_id="dup-1", source="cli", model="m")
        # Second call should be silent — no IntegrityError
        db.sessions.create(session_id="dup-1", source="gateway", model="m2")
        session = db.sessions.get("dup-1")
        # Row should exist (first write wins with OR IGNORE)
        assert session is not None
        assert session["source"] == "cli"

    def test_ensure_session_creates_missing_row(self, db):
        """ensure_session must create a minimal row when the session doesn't exist."""
        assert db.sessions.get("orphan-session") is None
        db.sessions.ensure("orphan-session", source="gateway", model="test-model")
        row = db.sessions.get("orphan-session")
        assert row is not None
        assert row["source"] == "gateway"
        assert row["model"] == "test-model"

    def test_ensure_session_is_idempotent(self, db):
        """ensure_session on an existing row must be a no-op (no overwrite)."""
        db.sessions.create(session_id="existing", source="cli", model="original-model")
        db.sessions.ensure("existing", source="gateway", model="overwrite-model")
        row = db.sessions.get("existing")
        # First write wins — ensure_session must not overwrite
        assert row["source"] == "cli"
        assert row["model"] == "original-model"

    def test_ensure_session_allows_append_message_after_failed_create(self, db):
        """Messages can be flushed even when create_session failed at startup.

        Simulates the #3139 scenario: create_session raises (lock), then
        ensure_session is called during flush, then append_message succeeds.
        """
        # Simulate failed create_session — row absent
        db.sessions.ensure("late-session", source="gateway", model="gpt-4")
        db.messages.append(
            session_id="late-session",
            role="user",
            content="hello after lock",
        )
        msgs = db.messages.list("late-session")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello after lock"

    def test_sqlite_busy_timeout_is_configured(self, db):
        """Repository connections wait for transient writer contention."""
        timeout_ms = int(db._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        assert timeout_ms >= 5000


# =========================================================================
# Auto-maintenance: state_meta + vacuum + maybe_auto_prune_and_vacuum
# =========================================================================

class TestStateMeta:
    def test_get_meta_missing_returns_none(self, db):
        assert db.metadata.get("nonexistent") is None

    def test_set_then_get_meta(self, db):
        db.metadata.set("foo", "bar")
        assert db.metadata.get("foo") == "bar"

    def test_set_meta_upsert(self, db):
        """set_meta overwrites existing value (ON CONFLICT DO UPDATE)."""
        db.metadata.set("key", "v1")
        db.metadata.set("key", "v2")
        assert db.metadata.get("key") == "v2"


class TestVacuum:
    def test_vacuum_runs_without_error(self, db):
        """VACUUM must succeed on a fresh DB (no rows to reclaim)."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(session_id="s1", role="user", content="hi")
        # Should not raise, even though there's nothing significant to reclaim.
        db.maintenance.vacuum()


class TestAutoMaintenance:
    def _make_old_ended(self, db, sid: str, days_old: int = 100):
        """Create a session that is ended and was started `days_old` days ago."""
        db.sessions.create(session_id=sid, source="cli")
        db.sessions.end(sid, reason="done")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - days_old * 86400, sid),
        )
        db._conn.commit()

    def test_first_run_prunes_and_vacuums(self, db):
        self._make_old_ended(db, "old1", days_old=100)
        self._make_old_ended(db, "old2", days_old=100)
        db.sessions.create(session_id="new", source="cli")  # active, must survive

        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 2
        assert result["vacuumed"] is True
        assert result.get("error") is None
        assert db.sessions.get("old1") is None
        assert db.sessions.get("old2") is None
        assert db.sessions.get("new") is not None

    def test_second_call_within_interval_skips(self, db):
        self._make_old_ended(db, "old", days_old=100)
        first = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert first["skipped"] is False
        assert first["pruned"] == 1

        # Create another prunable session; a second call within
        # min_interval_hours should still skip without touching it.
        self._make_old_ended(db, "old2", days_old=100)
        second = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert second["skipped"] is True
        assert second["pruned"] == 0
        assert db.sessions.get("old2") is not None  # untouched

    def test_second_call_after_interval_runs_again(self, db):
        self._make_old_ended(db, "old", days_old=100)
        db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90, min_interval_hours=24)

        # Backdate the last-run marker to force another run.
        db.metadata.set("last_auto_prune", str(time.time() - 48 * 3600))

        self._make_old_ended(db, "old2", days_old=100)
        result = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert result["skipped"] is False
        assert result["pruned"] == 1
        assert db.sessions.get("old2") is None

    def test_no_prunable_sessions_no_vacuum(self, db):
        """When prune deletes 0 rows, VACUUM is skipped (wasted I/O)."""
        db.sessions.create(session_id="fresh", source="cli")  # too recent
        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 0
        assert result["vacuumed"] is False
        # But last-run is still recorded so we don't retry immediately.
        assert db.metadata.get("last_auto_prune") is not None

    def test_auto_run_event_compaction_prunes_terminal_message_delta_chunks(self, db):
        db.runs.upsert(
            run_id="run-1",
            session_id="stored-1",
            runtime_scope_key="stored-1",
            turn_id="turn-1",
            execution_session_id="runtime-1",
            status="completed",
        )
        for seq, payload in (
            (1, '{"mode":"append","text":"A","delta":"A","offset":0}'),
            (2, '{"mode":"append","text":"B","delta":"B","offset":1}'),
            (3, '{"status":"complete","text":"AB"}'),
        ):
            db._conn.execute(
                """
                INSERT INTO run_events (
                    session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                    event_type, seq, timestamp, payload_json, event_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "stored-1",
                    "run-1",
                    "turn-1",
                    "runtime-1",
                    "stored-1",
                    "message.complete" if seq == 3 else "message.delta",
                    seq,
                    float(seq),
                    payload,
                    (
                        f'{{"type":"{"message.complete" if seq == 3 else "message.delta"}","session_id":"runtime-1",'
                        '"conversation_session_id":"stored-1","run_id":"run-1",'
                        '"turn_id":"turn-1","runtime_scope_key":"stored-1",'
                        f'"seq":{seq},"timestamp":{float(seq)},"payload":{payload}}}'
                    ),
                    "completed" if seq == 3 else "",
                ),
            )
        db._conn.commit()

        result = db.run_event_maintenance.maybe_auto_compact(vacuum=False)
        second = db.run_event_maintenance.maybe_auto_compact(vacuum=False)
        events = db.runs.list_events("stored-1")

        assert result["skipped"] is False
        assert result["deleted_events"] == 2
        assert result["pruned_terminal_stream_events"] == 2
        assert result["compacted_segments"] == 0
        assert result["vacuumed"] is False
        assert db.metadata.get("last_auto_run_event_compaction_v1") is not None
        assert second["skipped"] is True
        assert [event["type"] for event in events] == ["message.complete"]
        assert business_payload(events[0]) == {"status": "complete", "text": "AB"}

    def test_vacuum_disabled_via_flag(self, db):
        self._make_old_ended(db, "old", days_old=100)
        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90, vacuum=False)
        assert result["pruned"] == 1
        assert result["vacuumed"] is False

    def test_corrupt_last_run_marker_treated_as_no_prior_run(self, db):
        """A non-numeric marker must not break maintenance."""
        db.metadata.set("last_auto_prune", "not-a-timestamp")
        self._make_old_ended(db, "old", days_old=100)
        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 1

    def test_state_meta_survives_vacuum(self, db):
        """Marker written just before VACUUM must still be readable after."""
        self._make_old_ended(db, "old", days_old=100)
        db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        marker = db.metadata.get("last_auto_prune")
        assert marker is not None
        # Should parse as a float timestamp close to now.
        assert abs(float(marker) - time.time()) < 60

    def test_auto_prune_deletes_transcript_files(self, db, tmp_path):
        """Issue #3015: auto-prune must also delete on-disk transcript files."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        self._make_old_ended(db, "old1", days_old=100)
        self._make_old_ended(db, "old2", days_old=100)
        db.sessions.create(session_id="new", source="cli")  # active

        # Transcript files mimicking real gateway/CLI layout
        (sessions_dir / "old1.json").write_text("{}")
        (sessions_dir / "old1.jsonl").write_text("{}\n")
        (sessions_dir / "old2.jsonl").write_text("{}\n")
        (sessions_dir / "request_dump_old1_001.json").write_text("{}")
        (sessions_dir / "new.jsonl").write_text("{}\n")  # active, must survive

        result = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, sessions_dir=sessions_dir
        )
        assert result["pruned"] == 2

        # Pruned transcript files are gone
        assert not (sessions_dir / "old1.json").exists()
        assert not (sessions_dir / "old1.jsonl").exists()
        assert not (sessions_dir / "old2.jsonl").exists()
        assert not (sessions_dir / "request_dump_old1_001.json").exists()
        # Active session's transcript is untouched
        assert (sessions_dir / "new.jsonl").exists()

    def test_auto_prune_without_sessions_dir_preserves_files(self, db, tmp_path):
        """Backward-compat: no sessions_dir = DB-only cleanup (legacy behavior)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        self._make_old_ended(db, "old", days_old=100)
        (sessions_dir / "old.jsonl").write_text("{}\n")

        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["pruned"] == 1
        # File stays — caller didn't opt in
        assert (sessions_dir / "old.jsonl").exists()

    def test_prune_sessions_deletes_files_for_pruned_only(self, db, tmp_path):
        """Active-session transcripts must never be deleted by prune."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        self._make_old_ended(db, "old", days_old=100)
        db.sessions.create(session_id="active", source="cli")  # not ended
        (sessions_dir / "old.jsonl").write_text("{}\n")
        (sessions_dir / "active.jsonl").write_text("{}\n")

        count = db.maintenance.prune_sessions(older_than_days=90, sessions_dir=sessions_dir)
        assert count == 1
        assert not (sessions_dir / "old.jsonl").exists()
        assert (sessions_dir / "active.jsonl").exists()


# =========================================================================
# FTS5 indexing of tool_calls / tool_name (#16751)
# =========================================================================

class TestFTS5ToolCallIndexing:
    """Regression tests: search_messages must see tool_name and tool_calls.

    Before #16751's fix, `messages_fts` only indexed `messages.content`, so
    tokens that only appeared in `tool_name` or the serialized `tool_calls`
    JSON were invisible to session_search even though the row was in the DB.
    """

    def test_tool_name_is_searchable(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_name="UNIQUETOOLNAME",
        )
        results = db.messages.search("UNIQUETOOLNAME")
        assert len(results) == 1

    def test_tool_calls_args_are_searchable(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": '{"query": "UNIQUESEARCHTOKEN"}',
                },
            }],
        )
        results = db.messages.search("UNIQUESEARCHTOKEN")
        assert len(results) == 1

    def test_tool_function_name_in_tool_calls_is_searchable(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "UNIQUEFUNCNAME", "arguments": "{}"},
            }],
        )
        results = db.messages.search("UNIQUEFUNCNAME")
        assert len(results) == 1

    def test_delete_message_row_does_not_crash(self, db):
        """DELETE on messages must not raise when FTS rows reference tool fields.

        Previously the messages_fts_delete trigger passed old.content to the
        FTS5 delete-command but the inserted row was the concatenation of
        content || tool_name || tool_calls, so FTS5 rejected the delete with
        'SQL logic error' and every session delete path broke.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="hello",
            tool_name="web_search",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"q": "x"}'},
            }],
        )
        # end_session + end-time prune path would exercise DELETE; hit the
        # row directly through the write helper to keep the regression focused.
        def _delete(conn):
            conn.execute("DELETE FROM messages WHERE session_id = ?", ("s1",))
        db._execute_write(_delete)  # must not raise

        assert db.messages.search("hello") == []
        assert db.messages.search("web_search") == []

    def test_update_message_reindexes_tool_fields(self, db):
        """UPDATE must refresh the FTS row so old tokens drop out and new tokens appear."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_name="ORIGINALTOOL",
        )
        assert len(db.messages.search("ORIGINALTOOL")) == 1

        def _update(conn):
            conn.execute(
                "UPDATE messages SET tool_name = ? WHERE session_id = ?",
                ("RENAMEDTOOL", "s1"),
            )
        db._execute_write(_update)

        assert db.messages.search("ORIGINALTOOL") == []
        assert len(db.messages.search("RENAMEDTOOL")) == 1


class TestFTS5ToolCallMigration:
    """v11 migration: pre-existing state.db with old external-content FTS tables
    must be re-indexed so tool_name / tool_calls become searchable after upgrade."""

    def test_v10_to_v11_upgrade_backfills_tool_fields(self, tmp_path):
        """Simulate an existing user: build a v10-shaped DB by hand, insert a
        row with tool_calls, then open via CliSessionStore (which runs migrations).
        After upgrade, the tool_calls token must be searchable."""
        import sqlite3

        db_path = tmp_path / "legacy.db"

        # Build the pre-v11 schema by hand: external-content FTS tables +
        # old triggers that only reference new.content.
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (10);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                started_at REAL,
                ended_at REAL,
                title TEXT,
                parent_session_id TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                api_call_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_name TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT
            );

            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content, content=messages, content_rowid=id
            );
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;

            CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
                content, content=messages, content_rowid=id, tokenize='trigram'
            );
            CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
            END;
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "cli", time.time()),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role, content, tool_name, tool_calls) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", time.time(), "assistant", "", "LEGACYTOOL",
             '{"function":{"name":"web_search","arguments":"{\\"q\\":\\"LEGACYARG\\"}"}}'),
        )
        conn.commit()

        # Verify the legacy FTS rows don't contain the tool tokens yet.
        legacy_hits = conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'LEGACYTOOL'"
        ).fetchall()
        assert legacy_hits == [], "sanity: legacy FTS must NOT contain tool_name"
        conn.close()

        # Now open via CliSessionStore — migration runs.
        session_db = open_cli_session_store(db_path=db_path)
        try:
            assert len(session_db.messages.search("LEGACYTOOL")) == 1, \
                "v11 migration must backfill tool_name into FTS"
            assert len(session_db.messages.search("LEGACYARG")) == 1, \
                "v11 migration must backfill tool_calls JSON into FTS"
            # schema_version bumped
            row = session_db._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            version = row["version"] if hasattr(row, "keys") else row[0]
            assert version == CURRENT_SCHEMA_VERSION
        finally:
            session_db.close()


class TestSessionRewindSoftDelete:
    """Rewind keeps an audit trail while active transcript reads move back."""

    def test_rewind_soft_deletes_target_and_tail(self, db):
        db.sessions.create(session_id="rewind-s1", source="tui")
        db.messages.append("rewind-s1", role="user", content="question 1")
        db.messages.append("rewind-s1", role="assistant", content="answer 1")
        target_id = db.messages.append("rewind-s1", role="user", content="question 2")
        db.messages.append("rewind-s1", role="assistant", content="answer 2")

        result = db.messages.rewind("rewind-s1", target_id)

        assert result["rewound_count"] == 2
        assert result["target_message"]["content"] == "question 2"
        assert result["new_head_id"] == target_id - 1
        assert [m["content"] for m in db.messages.list("rewind-s1")] == [
            "question 1",
            "answer 1",
        ]
        all_rows = db.messages.list("rewind-s1", include_inactive=True)
        assert [row["active"] for row in all_rows] == [1, 1, 0, 0]
        assert db.sessions.get("rewind-s1")["rewind_count"] == 1

    def test_rewind_requires_user_target(self, db):
        db.sessions.create(session_id="rewind-s2", source="tui")
        target_id = db.messages.append("rewind-s2", role="assistant", content="answer")

        with pytest.raises(ValueError, match="user"):
            db.messages.rewind("rewind-s2", target_id)

    def test_rewound_rows_are_hidden_from_search_by_default(self, db):
        db.sessions.create(session_id="rewind-s3", source="tui")
        db.messages.append("rewind-s3", role="user", content="keep this")
        target_id = db.messages.append("rewind-s3", role="user", content="UNIQUE_REWIND_TOKEN")

        db.messages.rewind("rewind-s3", target_id)

        assert db.messages.search("UNIQUE_REWIND_TOKEN") == []
        assert len(db.messages.search("UNIQUE_REWIND_TOKEN", include_inactive=True)) == 1

    def test_list_recent_user_messages_defaults_to_active_rows(self, db):
        db.sessions.create(session_id="rewind-s4", source="tui")
        db.messages.append("rewind-s4", role="user", content="old prompt")
        target_id = db.messages.append("rewind-s4", role="user", content="new prompt")
        db.messages.rewind("rewind-s4", target_id)

        assert [row["preview"] for row in db.messages.recent_user_messages("rewind-s4")] == [
            "old prompt"
        ]
        assert [
            row["preview"]
            for row in db.messages.recent_user_messages("rewind-s4", include_inactive=True)
        ] == ["new prompt", "old prompt"]


class TestSessionIdSearch:
    """Session id search backs desktop/web session search without O(n) scans."""

    def _seed(self, db, sid, *, content="ordinary message"):
        db.sessions.create(session_id=sid, source="cli", model="test-model")
        db.messages.append(session_id=sid, role="user", content=content)

    def test_search_sessions_by_id_matches_exact_prefix_and_substring(self, db):
        self._seed(db, "20260603_090200_abcd12")
        self._seed(db, "20260602_111111_other99")

        assert [s["id"] for s in db.sessions.search_by_id("20260603_090200_abcd12")] == [
            "20260603_090200_abcd12"
        ]
        assert [s["id"] for s in db.sessions.search_by_id("20260603")] == [
            "20260603_090200_abcd12"
        ]
        assert [s["id"] for s in db.sessions.search_by_id("ABCD12")] == [
            "20260603_090200_abcd12"
        ]

    def test_search_sessions_by_id_prioritizes_exact_then_prefix(self, db):
        self._seed(db, "20260603_090200_abcd12")
        self._seed(db, "20260603_090200_abcd12_child")
        self._seed(db, "x_20260603_090200_abcd12")

        ids = [s["id"] for s in db.sessions.search_by_id("20260603_090200_abcd12", limit=2)]

        assert ids == ["20260603_090200_abcd12", "20260603_090200_abcd12_child"]

    def test_search_sessions_by_id_matches_projected_compression_root(self, db):
        root = "20260602_235959_root99"
        tip = "20260603_010000_tip01"
        db.sessions.create(session_id=root, source="cli")
        db.messages.append(root, role="user", content="root conversation")
        db.sessions.end(root, "compression")
        db.sessions.create(session_id=tip, source="cli", parent_session_id=root)
        db.messages.append(tip, role="user", content="continued conversation")

        matches = db.sessions.search_by_id("root99")

        assert [s["id"] for s in matches] == [tip]
        assert matches[0]["_lineage_root_id"] == root


class TestDovieLineageBranchListing:
    def test_session_lineage_branch_stays_visible_after_parent_reopen(self, db):
        db.sessions.create("source-reopen", "tui")
        db.sessions.set_title("source-reopen", "Source")
        target_id = db.messages.append("source-reopen", role="user", content="branch point")
        db.messages.append("source-reopen", role="assistant", content="answer")

        db.branches.branch_session(
            source_session_id="source-reopen",
            new_session_id="branch-reopen",
            branch_point={"message_id": str(target_id)},
            idempotency_key="branch-reopen-key",
        )
        db.sessions.reopen("source-reopen")
        db.sessions.end("source-reopen", "user_exit")

        ids = {row["id"] for row in db.sessions.list_rich(limit=10)}

        assert "source-reopen" in ids
        assert "branch-reopen" in ids
