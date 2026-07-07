"""Phase G — ``session.create`` completes the session lifecycle E2E."""

from __future__ import annotations

import sqlite3

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods import session_methods
from hermes_agent.repositories import SessionRepoImpl


def _make_conn() -> sqlite3.Connection:
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
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            parent_session_id TEXT,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY,
            owner_agent_profile_id TEXT NOT NULL DEFAULT '',
            owner_profile_version_id TEXT NOT NULL DEFAULT '',
            runtime_scope_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            session_kind TEXT NOT NULL DEFAULT '',
            conversation_kind TEXT NOT NULL DEFAULT '',
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
    return conn


def _wired():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    registry = MethodRegistry()
    session_methods.register(registry, repo)
    return conn, repo, registry


def test_session_create_persists_row_and_returns_projection():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.create",
            "params": {
                "sessionId": "s1",
                "source": "test",
                "title": "Hello",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert result["session_id"] == "s1"
    assert result["source"] == "test"
    assert result["title"] == "Hello"

    row = conn.execute("SELECT * FROM sessions WHERE id='s1'").fetchone()
    assert row is not None
    assert row["title"] == "Hello"


def test_session_create_provisions_index_row_too():
    conn, repo, registry = _wired()
    dispatch(
        registry,
        {
            "id": "req",
            "method": "session.create",
            "params": {"sessionId": "s1", "source": "test", "title": "T1"},
        },
        resolver=AllowAllResolver(),
    )
    row = conn.execute("SELECT * FROM session_index WHERE session_id='s1'").fetchone()
    assert row is not None
    assert row["title"] == "T1"


def test_session_create_rejects_missing_session_id():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "session.create", "params": {"source": "test"}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_session_create_rejects_missing_source():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "session.create", "params": {"sessionId": "s1"}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_session_create_folds_stored_session_id_alias():
    """Legacy alias in request still gets picked up and normalised."""
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.create",
            "params": {"storedSessionId": "s1", "source": "test"},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["session_id"] == "s1"


def test_session_create_denied_when_read_only_resolver():
    conn, repo, registry = _wired()

    class _RO:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.create",
            "params": {"sessionId": "s1", "source": "test"},
        },
        resolver=_RO(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


def test_session_lifecycle_end_to_end():
    """create → get → list → branch → close via dispatch pipeline only."""
    conn, repo, registry = _wired()
    dispatch(
        registry,
        {
            "id": "req",
            "method": "session.create",
            "params": {"sessionId": "s1", "source": "test", "title": "Parent"},
        },
        resolver=AllowAllResolver(),
    )
    got = dispatch(
        registry,
        {"id": "req", "method": "session.get", "params": {"sessionId": "s1"}},
        resolver=AllowAllResolver(),
    )
    assert got["result"]["session_id"] == "s1"

    listed = dispatch(
        registry,
        {"id": "req", "method": "session.list", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert any(s["session_id"] == "s1" for s in listed["result"]["sessions"])

    branched = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.branch",
            "params": {
                "sourceId": "s1",
                "newSessionId": "s1-child",
                "branchFromSeq": 0,
                "title": "Child",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert branched["result"]["parent_session_id"] == "s1"

    closed = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.close",
            "params": {"sessionId": "s1", "reason": "user_ended"},
        },
        resolver=AllowAllResolver(),
    )
    assert closed["result"]["ended_at"] is not None
