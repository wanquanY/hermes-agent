"""P2 Slice 1: session dispatch must persist through SessionRepoImpl."""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes_agent.gateway import AllowAllResolver, MethodRegistry
from hermes_agent.gateway.methods import session_methods
from hermes_agent.gateway.pipeline import dispatch
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.storage.session_repository_db import connect_session_repository_db


def _registry(conn: sqlite3.Connection) -> MethodRegistry:
    registry = MethodRegistry()
    session_methods.register(registry, SessionRepoImpl(conn))
    return registry


def _dispatch(
    registry: MethodRegistry,
    method: str,
    params: dict[str, Any],
    request_id: str = "req-1",
) -> dict[str, Any]:
    return dispatch(
        registry,
        {"id": request_id, "method": method, "params": params},
        resolver=AllowAllResolver(),
    )


def _assert_no_legacy_session_alias(value: Any) -> None:
    if isinstance(value, dict):
        for legacy_key in ("stored_session_id", "stable_session_id", "runtime_session_id"):
            assert legacy_key not in value
        for child in value.values():
            _assert_no_legacy_session_alias(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_legacy_session_alias(child)


def test_session_create_get_list_dispatches_through_repo_sqlite(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    registry = _registry(conn)

    created = _dispatch(
        registry,
        "session.create",
        {
            "sessionId": "session-1",
            "source": "dovie",
            "title": "Root task",
            "displayTitle": "Root task display",
            "sessionKind": "team_mission",
            "conversationKind": "team",
            "runtimeScopeKey": "scope-1",
        },
    )

    assert "error" not in created
    result = created["result"]
    assert result["session_id"] == "session-1"
    assert result["source"] == "dovie"
    assert result["title"] == "Root task"
    assert result["display_title"] == "Root task display"
    assert result["session_kind"] == "team_mission"
    assert result["conversation_kind"] == "team"
    assert isinstance(result["started_at"], float)
    assert isinstance(result["updated_at"], float)
    _assert_no_legacy_session_alias(result)

    session_row = conn.execute(
        """
        SELECT id, source, title, display_title, session_kind, conversation_kind,
               started_at, updated_at
          FROM sessions
         WHERE id = ?
        """,
        ("session-1",),
    ).fetchone()
    assert dict(session_row) == {
        "id": "session-1",
        "source": "dovie",
        "title": "Root task",
        "display_title": "Root task display",
        "session_kind": "team_mission",
        "conversation_kind": "team",
        "started_at": result["started_at"],
        "updated_at": result["updated_at"],
    }

    index_row = conn.execute(
        """
        SELECT session_id, source, title, session_kind, conversation_kind,
               runtime_scope_key, started_at, updated_at
          FROM session_index
         WHERE session_id = ?
        """,
        ("session-1",),
    ).fetchone()
    assert dict(index_row) == {
        "session_id": "session-1",
        "source": "dovie",
        "title": "Root task",
        "session_kind": "team_mission",
        "conversation_kind": "team",
        "runtime_scope_key": "scope-1",
        "started_at": result["started_at"],
        "updated_at": result["updated_at"],
    }

    got = _dispatch(
        registry,
        "session.get",
        {"conversationSessionId": "session-1"},
        request_id="req-2",
    )
    assert "error" not in got
    assert got["result"]["session_id"] == "session-1"
    assert got["result"]["display_title"] == "Root task display"
    _assert_no_legacy_session_alias(got["result"])

    listed = _dispatch(
        registry,
        "session.list",
        {"conversationKind": "team", "limit": 10},
        request_id="req-3",
    )
    assert "error" not in listed
    assert [item["session_id"] for item in listed["result"]["sessions"]] == ["session-1"]
    _assert_no_legacy_session_alias(listed["result"])

    conn.close()
