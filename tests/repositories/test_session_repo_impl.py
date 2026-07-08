"""Phase D2 — SessionRepoImpl concrete behavior (spec §4.1)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.repositories import (
    BranchSpec,
    Session,
    SessionFilter,
    SessionIndexPatch,
    SessionMessageAppendProjection,
    SessionMessageSnapshotProjection,
    SessionNotFound,
    SessionRepo,
    SessionRepoImpl,
    SessionRunProjection,
    SessionSpec,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            title TEXT,
            display_title TEXT,
            display_title_source TEXT,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_active REAL,
            ended_at REAL,
            end_reason TEXT,
            transient INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            preview TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY,
            owner_agent_profile_id TEXT NOT NULL DEFAULT '',
            owner_profile_version_id TEXT NOT NULL DEFAULT '',
            runtime_scope_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'unknown',
            transient INTEGER NOT NULL DEFAULT 0,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            status TEXT NOT NULL DEFAULT 'idle',
            running INTEGER NOT NULL DEFAULT 0,
            waiting_approval INTEGER NOT NULL DEFAULT 0,
            active_run_id TEXT NOT NULL DEFAULT '',
            active_execution_session_id TEXT NOT NULL DEFAULT '',
            mission_id TEXT NOT NULL DEFAULT '',
            pending_approval_count INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            last_activity REAL
        );
        CREATE TABLE session_branches (
            child_session_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            branch_from_seq INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def test_impl_is_structural_session_repo():
    """SessionRepoImpl is a structural SessionRepo (runtime_checkable)."""
    repo = SessionRepoImpl(_make_conn())
    assert isinstance(repo, SessionRepo)


def test_create_persists_session_row():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    session = repo.create(SessionSpec(session_id="s1", source="test", title="Hello"))

    assert isinstance(session, Session)
    assert session.session_id == "s1"
    assert session.source == "test"
    assert session.title == "Hello"

    row = conn.execute("SELECT id, source, title FROM sessions WHERE id='s1'").fetchone()
    assert row["id"] == "s1"
    assert row["title"] == "Hello"


def test_create_provisions_session_index_row():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test", title="T1"))

    row = conn.execute("SELECT session_id, title FROM session_index WHERE session_id='s1'").fetchone()
    assert row is not None
    assert row["session_id"] == "s1"
    assert row["title"] == "T1"


def test_create_persists_tui_session_metadata():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(
        SessionSpec(
            session_id="s1",
            source="tui",
            user_id="user-1",
            model="gpt-test",
            model_config={"runtime_executor": "codex_app_server"},
            transient=True,
        )
    )

    session_row = conn.execute(
        "SELECT user_id, model, model_config, transient FROM sessions WHERE id='s1'"
    ).fetchone()
    assert session_row["user_id"] == "user-1"
    assert session_row["model"] == "gpt-test"
    assert session_row["model_config"] == '{"runtime_executor":"codex_app_server"}'
    assert session_row["transient"] == 1

    index_row = conn.execute(
        "SELECT transient FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert index_row["transient"] == 1


def test_create_empty_session_id_rejected():
    repo = SessionRepoImpl(_make_conn())
    with pytest.raises(ValueError):
        repo.create(SessionSpec(session_id="", source="test"))


def test_get_returns_session_or_none():
    repo = SessionRepoImpl(_make_conn())
    assert repo.get("missing") is None
    repo.create(SessionSpec(session_id="s1", source="test"))
    got = repo.get("s1")
    assert got is not None
    assert got.session_id == "s1"


def test_get_supports_legacy_session_schema_without_updated_at_or_kind_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT,
            display_title TEXT,
            display_title_source TEXT,
            started_at REAL NOT NULL,
            last_active REAL,
            ended_at REAL,
            parent_session_id TEXT
        );
        INSERT INTO sessions (
            id, source, title, display_title, display_title_source,
            started_at, last_active, ended_at, parent_session_id
        ) VALUES (
            'legacy-team', 'team_mission', 'Legacy', 'Legacy Display', 'user',
            10, 20, NULL, ''
        );
        """
    )
    repo = SessionRepoImpl(conn)

    got = repo.get("legacy-team")

    assert got is not None
    assert got.session_id == "legacy-team"
    assert got.display_title == "Legacy Display"
    assert got.session_kind == "hermes_session"
    assert got.conversation_kind == "team"
    assert got.updated_at == 20


def test_list_default_excludes_ended_sessions():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test"))
    repo.create(SessionSpec(session_id="s2", source="test"))
    repo.close("s1", reason="done")

    active = repo.list(SessionFilter())
    assert [s.session_id for s in active] == ["s2"]

    with_ended = repo.list(SessionFilter(include_ended=True))
    assert {s.session_id for s in with_ended} == {"s1", "s2"}


def test_list_filters_by_source_and_kind():
    repo = SessionRepoImpl(_make_conn())
    repo.create(SessionSpec(session_id="s1", source="team", session_kind="team_room"))
    repo.create(SessionSpec(session_id="s2", source="direct", session_kind="hermes_session"))

    only_team = repo.list(SessionFilter(source="team"))
    assert [s.session_id for s in only_team] == ["s1"]

    only_hermes_kind = repo.list(SessionFilter(session_kind="hermes_session"))
    assert [s.session_id for s in only_hermes_kind] == ["s2"]


def test_update_index_partial_patch():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test"))

    repo.update_index(
        "s1",
        SessionIndexPatch(
            status="running",
            running=1,
            active_run_id="run-1",
            fields={"preview": "a preview"},
        ),
    )

    row = conn.execute(
        """
        SELECT status, running, active_run_id, preview
          FROM session_index
         WHERE session_id = 's1'
        """
    ).fetchone()
    assert row["status"] == "running"
    assert row["running"] == 1
    assert row["active_run_id"] == "run-1"
    assert row["preview"] == "a preview"


def test_update_index_noop_when_no_fields_provided():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test", title="original"))
    before = conn.execute(
        "SELECT updated_at FROM session_index WHERE session_id='s1'"
    ).fetchone()["updated_at"]
    repo.update_index("s1", SessionIndexPatch())
    after = conn.execute(
        "SELECT updated_at FROM session_index WHERE session_id='s1'"
    ).fetchone()["updated_at"]
    # No columns provided → row untouched.
    assert before == after


def test_project_run_state_marks_existing_session_index_running():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test", runtime_scope_key="old-scope"))

    repo.project_run_state(
        SessionRunProjection(
            session_id="s1",
            run_id="run-1",
            execution_session_id="exec-1",
            runtime_scope_key="scope-1",
            status="running",
            updated_at=100.0,
        )
    )

    row = conn.execute(
        """
        SELECT status, running, active_run_id, active_execution_session_id,
               runtime_scope_key, updated_at
          FROM session_index
         WHERE session_id = 's1'
        """
    ).fetchone()
    got = dict(row)
    assert got.pop("updated_at") >= 100.0
    assert got == {
        "status": "running",
        "running": 1,
        "active_run_id": "run-1",
        "active_execution_session_id": "exec-1",
        "runtime_scope_key": "scope-1",
    }


def test_project_run_state_updates_bound_team_conversation_index():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="team:mission-1:node:root", source="runtime"))
    repo.create(
        SessionSpec(
            session_id="team-conversation-1",
            source="team_mission",
            session_kind="team_mission",
            conversation_kind="team",
        )
    )

    repo.project_run_state(
        SessionRunProjection(
            session_id="team:mission-1:node:root",
            run_id="run-node-1",
            conversation_session_id="team-conversation-1",
            conversation_scope_key="team:conversation-1:leader-conversation",
            execution_session_id="exec-node-1",
            runtime_scope_key="team:mission-1:node:root",
            status="running",
            updated_at=200.0,
        )
    )

    rows = conn.execute(
        """
        SELECT session_id, status, running, active_run_id,
               active_execution_session_id, runtime_scope_key
          FROM session_index
         WHERE session_id IN ('team:mission-1:node:root', 'team-conversation-1')
         ORDER BY session_id
        """
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "session_id": "team-conversation-1",
            "status": "running",
            "running": 1,
            "active_run_id": "run-node-1",
            "active_execution_session_id": "exec-node-1",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
        },
        {
            "session_id": "team:mission-1:node:root",
            "status": "running",
            "running": 1,
            "active_run_id": "run-node-1",
            "active_execution_session_id": "exec-node-1",
            "runtime_scope_key": "team:mission-1:node:root",
        },
    ]


def test_ensure_runtime_session_inserts_runtime_row_once():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)

    assert repo.ensure_runtime_session("runtime-session-1", started_at=42.0) is True
    assert repo.ensure_runtime_session("runtime-session-1", started_at=99.0) is False

    row = conn.execute(
        """
        SELECT id, source, started_at, updated_at
          FROM sessions
         WHERE id = 'runtime-session-1'
        """
    ).fetchone()
    assert dict(row) == {
        "id": "runtime-session-1",
        "source": "runtime",
        "started_at": 42.0,
        "updated_at": 42.0,
    }


def test_ensure_runtime_session_supports_legacy_minimal_schema():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL
        );
        """
    )
    repo = SessionRepoImpl(conn)

    assert repo.ensure_runtime_session("legacy-runtime", started_at=7.0) is True

    row = conn.execute("SELECT * FROM sessions WHERE id = 'legacy-runtime'").fetchone()
    assert dict(row) == {
        "id": "legacy-runtime",
        "source": "runtime",
        "started_at": 7.0,
    }


def test_record_message_append_updates_session_and_index_projection():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test", title="Original"))

    repo.record_message_append(
        "s1",
        SessionMessageAppendProjection(
            timestamp=123.0,
            tool_call_count=2,
            user_preview="hello preview",
            user_display_title="hello title",
        ),
    )

    session_row = conn.execute(
        """
        SELECT message_count, tool_call_count, preview, display_title,
               display_title_source, last_active, updated_at
          FROM sessions
         WHERE id = 's1'
        """
    ).fetchone()
    assert session_row["message_count"] == 1
    assert session_row["tool_call_count"] == 2
    assert session_row["preview"] == "hello preview"
    assert session_row["display_title"] == "hello title"
    assert session_row["display_title_source"] == "first_user_message"
    assert session_row["last_active"] == 123.0
    assert session_row["updated_at"] == 123.0

    index_row = conn.execute(
        """
        SELECT title, preview, message_count, last_activity
          FROM session_index
         WHERE session_id = 's1'
        """
    ).fetchone()
    assert index_row["title"] == "hello title"
    assert index_row["preview"] == "hello preview"
    assert index_row["message_count"] == 1
    assert index_row["last_activity"] == 123.0


def test_replace_message_projection_preserves_user_title_source():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test", title="Original"))
    repo.set_title("s1", "Pinned title", title_source="user")

    repo.replace_message_projection(
        "s1",
        SessionMessageSnapshotProjection(
            message_count=3,
            tool_call_count=1,
            first_user_preview="new preview",
            first_user_display_title="generated title",
            last_message_ts=456.0,
        ),
    )

    session_row = conn.execute(
        """
        SELECT message_count, tool_call_count, preview, display_title,
               display_title_source, last_active
          FROM sessions
         WHERE id = 's1'
        """
    ).fetchone()
    assert session_row["message_count"] == 3
    assert session_row["tool_call_count"] == 1
    assert session_row["preview"] == "new preview"
    assert session_row["display_title"] == "Pinned title"
    assert session_row["display_title_source"] == "user"
    assert session_row["last_active"] == 456.0


def test_session_metadata_updates_are_owned_by_session_repo():
    conn = _make_conn()
    conn.executescript(
        """
        ALTER TABLE sessions ADD COLUMN cwd TEXT;
        ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE sessions ADD COLUMN input_tokens INTEGER;
        ALTER TABLE sessions ADD COLUMN output_tokens INTEGER;
        ALTER TABLE sessions ADD COLUMN pricing_version TEXT;
        ALTER TABLE sessions ADD COLUMN system_prompt TEXT;
        ALTER TABLE sessions ADD COLUMN handoff_state TEXT;
        ALTER TABLE sessions ADD COLUMN handoff_platform TEXT;
        ALTER TABLE sessions ADD COLUMN handoff_error TEXT;
        """
    )
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test"))

    assert repo.update_cwd("s1", "/workspace") is True
    assert repo.update_usage(
        "s1",
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "pricing_version": "v1",
            "not_a_session_field": "ignored",
        },
    ) is True
    assert repo.set_archived("s1", True) is True
    assert repo.update_system_prompt("s1", "system") is True
    assert repo.request_handoff("s1", "telegram") is True
    assert repo.fail_handoff("s1", "timeout") is True

    row = conn.execute(
        """
        SELECT cwd, archived, input_tokens, output_tokens, pricing_version,
               system_prompt, handoff_state, handoff_platform, handoff_error
          FROM sessions
         WHERE id = 's1'
        """
    ).fetchone()
    assert dict(row) == {
        "cwd": "/workspace",
        "archived": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "pricing_version": "v1",
        "system_prompt": "system",
        "handoff_state": "failed",
        "handoff_platform": "telegram",
        "handoff_error": "timeout",
    }


def test_finalize_orphaned_compression_sessions_is_session_repo_owned():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="parent", source="test"))
    repo.create(SessionSpec(session_id="orphan", source="test", parent_session_id="parent"))

    assert repo.finalize_orphaned_compression_sessions() == 1

    row = conn.execute(
        "SELECT ended_at, end_reason FROM sessions WHERE id = 'orphan'"
    ).fetchone()
    assert row["ended_at"] is not None
    assert row["end_reason"] == "compression_orphan"


def test_title_methods_update_sessions_and_index():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test", title="old"))

    assert repo.get_title("s1") == "old"
    assert repo.set_title("s1", "  new   title  ") is True
    assert repo.get_title("s1") == "new title"

    by_title = repo.get_by_title("new title")
    assert by_title is not None
    assert by_title.session_id == "s1"
    session_row = conn.execute(
        "SELECT title, display_title, display_title_source FROM sessions WHERE id='s1'"
    ).fetchone()
    assert dict(session_row) == {
        "title": "new title",
        "display_title": "new title",
        "display_title_source": "user",
    }
    index_row = conn.execute(
        "SELECT title FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert index_row["title"] == "new title"


def test_title_methods_reject_auto_source_and_duplicate_titles():
    repo = SessionRepoImpl(_make_conn())
    repo.create(SessionSpec(session_id="s1", source="test", title="one"))
    repo.create(SessionSpec(session_id="s2", source="test", title="two"))

    assert repo.set_title("s1", "auto title", title_source="auto") is False
    assert repo.get_title("s1") == "one"
    with pytest.raises(ValueError):
        repo.set_title("s1", "two")


def test_branch_creates_child_session():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test", title="Parent"))

    child = repo.branch(
        "s1",
        BranchSpec(new_session_id="s1-branch", branch_from_seq=42, title="Child"),
    )
    assert child.session_id == "s1-branch"
    assert child.parent_session_id == "s1"
    assert child.title == "Child"

    row = conn.execute(
        "SELECT parent_session_id, branch_from_seq FROM session_branches WHERE child_session_id='s1-branch'"
    ).fetchone()
    assert row["parent_session_id"] == "s1"
    assert row["branch_from_seq"] == 42


def test_branch_missing_source_raises():
    repo = SessionRepoImpl(_make_conn())
    with pytest.raises(SessionNotFound):
        repo.branch("missing", BranchSpec(new_session_id="new", branch_from_seq=0))


def test_close_marks_session_ended_and_updates_index():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test"))

    repo.close("s1", reason="user_ended")

    session_row = conn.execute(
        "SELECT ended_at, end_reason FROM sessions WHERE id='s1'"
    ).fetchone()
    assert session_row["ended_at"] is not None
    assert session_row["end_reason"] == "user_ended"

    idx_row = conn.execute(
        "SELECT status, running, active_run_id FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert idx_row["status"] == "closed"
    assert idx_row["running"] == 0
    assert idx_row["active_run_id"] == ""


def test_close_preserves_first_terminal_reason():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test"))

    repo.close("s1", reason="first")
    repo.close("s1", reason="second")

    session_row = conn.execute(
        "SELECT end_reason FROM sessions WHERE id='s1'"
    ).fetchone()
    assert session_row["end_reason"] == "first"


def test_reopen_clears_terminal_state():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="s1", source="test"))
    repo.close("s1", reason="done")

    repo.reopen("s1")

    session_row = conn.execute(
        "SELECT ended_at, end_reason FROM sessions WHERE id='s1'"
    ).fetchone()
    assert session_row["ended_at"] is None
    assert session_row["end_reason"] is None

    idx_row = conn.execute(
        "SELECT status, running, waiting_approval, active_run_id FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert idx_row["status"] == "idle"
    assert idx_row["running"] == 0
    assert idx_row["waiting_approval"] == 0
    assert idx_row["active_run_id"] == ""


def test_resolve_resume_session_id_follows_compression_tip_lineage():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="parent", source="test"))
    repo.close("parent", reason="compression")
    repo.create(SessionSpec(session_id="tip", source="test", parent_session_id="parent"))

    assert repo.resolve_resume_session_id("parent") == "tip"


def test_reopen_supports_legacy_session_schema_without_updated_at_or_end_reason():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT,
            display_title TEXT,
            display_title_source TEXT,
            started_at REAL NOT NULL,
            last_active REAL,
            ended_at REAL,
            parent_session_id TEXT
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'closed',
            running INTEGER NOT NULL DEFAULT 1,
            waiting_approval INTEGER NOT NULL DEFAULT 1,
            active_run_id TEXT NOT NULL DEFAULT 'run-old',
            updated_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO sessions (
            id, source, title, display_title, display_title_source,
            started_at, last_active, ended_at, parent_session_id
        ) VALUES (
            'legacy', 'tui', 'Legacy', 'Legacy', 'user', 10, 20, 30, ''
        );
        INSERT INTO session_index (session_id) VALUES ('legacy');
        """
    )
    repo = SessionRepoImpl(conn)

    repo.reopen("legacy")

    session_row = conn.execute(
        "SELECT ended_at, last_active FROM sessions WHERE id = 'legacy'"
    ).fetchone()
    assert session_row["ended_at"] is None
    assert session_row["last_active"] > 20
    index_row = conn.execute(
        "SELECT status, running, waiting_approval, active_run_id FROM session_index WHERE session_id = 'legacy'"
    ).fetchone()
    assert dict(index_row) == {
        "status": "idle",
        "running": 0,
        "waiting_approval": 0,
        "active_run_id": "",
    }
