"""Phase D2 — SessionRepoImpl concrete behavior (spec §4.1)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.repositories import (
    BranchSpec,
    Session,
    SessionFilter,
    SessionIndexPatch,
    SessionNotFound,
    SessionRepo,
    SessionRepoImpl,
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
            ended_at REAL,
            end_reason TEXT,
            transient INTEGER NOT NULL DEFAULT 0
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
            active_runtime_session_id TEXT NOT NULL DEFAULT '',
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


def test_resolve_resume_session_id_follows_compression_tip_with_messages():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="parent", source="test"))
    repo.close("parent", reason="compression")
    repo.create(SessionSpec(session_id="tip", source="test", parent_session_id="parent"))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO messages (session_id, active) VALUES ('tip', 1);
        """
    )

    assert repo.resolve_resume_session_id("parent") == "tip"


def test_resolve_resume_session_id_follows_latest_child_when_parent_has_no_messages():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    repo.create(SessionSpec(session_id="parent", source="test"))
    repo.create(SessionSpec(session_id="empty-child", source="test", parent_session_id="parent"))
    repo.create(SessionSpec(session_id="message-child", source="test", parent_session_id="empty-child"))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO messages (session_id, active) VALUES ('message-child', 1);
        """
    )

    assert repo.resolve_resume_session_id("parent") == "message-child"


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
