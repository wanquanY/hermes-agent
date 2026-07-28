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
from hermes_agent.composition.session_repository_db import (
    ensure_session_repository_schema,
)
from hermes_agent.storage.state_schema import SCHEMA_SQL


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    ensure_session_repository_schema(conn)
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


def test_session_create_folds_conversation_session_id_alias():
    """Legacy alias in request still gets picked up and normalised."""
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.create",
            "params": {"conversationSessionId": "s1", "source": "test"},
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
