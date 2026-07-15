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
from hermes_agent.storage.state_schema import SCHEMA_SQL


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
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


def test_update_index_folds_conversation_session_id_alias():
    """Spec §5.1 — identity fold on the way in."""
    conn, _repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "session.update_index",
            "params": {
                "conversationSessionId": "s1",  # legacy alias — camelCase too
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
