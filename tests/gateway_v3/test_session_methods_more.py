"""Phase G — ``session.list`` / ``session.close`` / ``session.branch`` E2E."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods import session_methods
from hermes_agent.repositories import SessionRepoImpl, SessionSpec


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
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
    conn.commit()
    return conn


def _wired():
    conn = _make_conn()
    repo = SessionRepoImpl(conn)
    registry = MethodRegistry()
    session_methods.register(registry, repo)
    return conn, repo, registry


def test_session_list_returns_active_sessions_only_by_default():
    conn, repo, registry = _wired()
    repo.create(SessionSpec(session_id="s1", source="test"))
    repo.create(SessionSpec(session_id="s2", source="test"))
    repo.close("s1", reason="done")

    resp = dispatch(
        registry,
        {"id": "req", "method": "session.list", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    ids = [s["session_id"] for s in resp["result"]["sessions"]]
    assert ids == ["s2"]


def test_session_list_include_ended_returns_all():
    conn, repo, registry = _wired()
    repo.create(SessionSpec(session_id="s1", source="test"))
    repo.create(SessionSpec(session_id="s2", source="test"))
    repo.close("s1", reason="done")

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.list",
            "params": {"includeEnded": True},
        },
        resolver=AllowAllResolver(),
    )
    ids = {s["session_id"] for s in resp["result"]["sessions"]}
    assert ids == {"s1", "s2"}


def test_session_list_respects_source_filter():
    conn, repo, registry = _wired()
    repo.create(SessionSpec(session_id="s1", source="team"))
    repo.create(SessionSpec(session_id="s2", source="direct"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.list",
            "params": {"source": "team"},
        },
        resolver=AllowAllResolver(),
    )
    ids = [s["session_id"] for s in resp["result"]["sessions"]]
    assert ids == ["s1"]


def test_session_close_marks_ended_and_returns_ended_at():
    conn, repo, registry = _wired()
    repo.create(SessionSpec(session_id="s1", source="test"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.close",
            "params": {"sessionId": "s1", "reason": "user_ended"},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["reason"] == "user_ended"
    assert resp["result"]["ended_at"] is not None


def test_session_close_missing_returns_5005():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.close",
            "params": {"sessionId": "no-such", "reason": "x"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value


def test_session_close_missing_session_id_returns_4002():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "session.close", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_session_branch_creates_child_and_returns_projection():
    conn, repo, registry = _wired()
    repo.create(SessionSpec(session_id="s1", source="test", title="Parent"))

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.branch",
            "params": {
                "sourceId": "s1",
                "newSessionId": "s1-child",
                "branchFromSeq": 42,
                "title": "Child",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    child = resp["result"]
    assert child["session_id"] == "s1-child"
    assert child["parent_session_id"] == "s1"
    assert child["title"] == "Child"


def test_session_branch_missing_source_returns_5005():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.branch",
            "params": {"sourceId": "no-such", "newSessionId": "child"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value


def test_session_close_denied_when_write_permission_missing():
    conn, repo, registry = _wired()
    repo.create(SessionSpec(session_id="s1", source="test"))

    class _ReadOnly:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.close",
            "params": {"sessionId": "s1", "reason": "x"},
        },
        resolver=_ReadOnly(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


def test_session_branch_alias_source_id_via_session_id_fallback():
    """If the caller sends only session_id (typo/legacy), branch honors it."""
    conn, repo, registry = _wired()
    repo.create(SessionSpec(session_id="s1", source="test"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.branch",
            "params": {"sessionId": "s1", "newSessionId": "child"},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["session_id"] == "child"
