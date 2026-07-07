"""``session.update_index`` gateway method (spec §4.1)."""

from __future__ import annotations

import sqlite3

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
    repo.create(SessionSpec(session_id="s1", source="test", title="original"))
    registry = MethodRegistry()
    session_methods.register(registry, repo)
    return conn, repo, registry


# ---------------------------------------------------------------------------


def test_update_index_applies_status_and_running():
    conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {
                "session_id": "s1",
                "patch": {"status": "running", "running": 1},
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp, resp
    row = conn.execute(
        "SELECT status, running FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert row["status"] == "running"
    assert row["running"] == 1
    assert set(resp["result"]["applied_fields"]) == {"status", "running"}


def test_update_index_missing_session_returns_5001():
    conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {
                "session_id": "does_not_exist",
                "patch": {"status": "running"},
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value


def test_update_index_requires_session_id():
    _conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {"patch": {"status": "running"}},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_update_index_requires_non_empty_patch():
    _conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {"session_id": "s1", "patch": {}},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_update_index_rejects_unknown_fields():
    _conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {
                "session_id": "s1",
                "patch": {"status": "idle", "hackery": "gotcha"},
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value
    assert "hackery" in resp["error"]["message"]


def test_update_index_folds_stored_session_id_alias():
    """Spec §5.1 — identity fold on the way in."""
    conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {
                "storedSessionId": "s1",  # legacy alias — camelCase too
                "patch": {"status": "waiting"},
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    row = conn.execute(
        "SELECT status FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert row["status"] == "waiting"


def test_update_index_updates_running_pending_count_together():
    conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {
                "session_id": "s1",
                "patch": {
                    "running": 1,
                    "pending_approval_count": 3,
                    "waiting_approval": 1,
                    "active_run_id": "run-abc",
                },
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp, resp
    row = conn.execute(
        "SELECT running, pending_approval_count, waiting_approval, active_run_id "
        "FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert row["running"] == 1
    assert row["pending_approval_count"] == 3
    assert row["waiting_approval"] == 1
    assert row["active_run_id"] == "run-abc"
