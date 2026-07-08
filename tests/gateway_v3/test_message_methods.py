"""Phase G — ``message.*`` gateway methods E2E."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods import message_methods
from hermes_agent.repositories import MessageRepoImpl, MessageSpec


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            participant_id TEXT NOT NULL DEFAULT '',
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            reasoning TEXT,
            conversation_message_id TEXT NOT NULL DEFAULT '',
            platform_message_id TEXT,
            metadata_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()
    return conn


def _wired():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    registry = MethodRegistry()
    message_methods.register(registry, repo)
    return conn, repo, registry


def test_message_append_persists_and_returns_projection():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.append",
            "params": {
                "sessionId": "s1",
                "role": "user",
                "content": "hi",
                "metadata": {"key": "val"},
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    msg = resp["result"]
    assert msg["session_id"] == "s1"
    assert msg["role"] == "user"
    assert msg["content"] == "hi"
    assert msg["metadata"] == {"key": "val"}


def test_message_append_rejects_missing_session_and_role():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "message.append", "params": {"role": "user"}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value
    resp = dispatch(
        registry,
        {"id": "req", "method": "message.append", "params": {"sessionId": "s1"}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_message_append_rejects_non_dict_metadata():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.append",
            "params": {"sessionId": "s1", "role": "user", "metadata": [1, 2, 3]},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_message_get_page_tail_returns_chronological_order():
    conn, repo, registry = _wired()
    for i in range(3):
        repo.append("s1", MessageSpec(session_id="s1", role="user", content=f"m{i}"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get_page",
            "params": {"sessionId": "s1", "direction": "tail", "limit": 10},
        },
        resolver=AllowAllResolver(),
    )
    contents = [m["content"] for m in resp["result"]["messages"]]
    assert contents == ["m0", "m1", "m2"]


def test_message_get_page_head_and_cursor():
    conn, repo, registry = _wired()
    for i in range(10):
        repo.append("s1", MessageSpec(session_id="s1", role="user", content=f"m{i}"))

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get_page",
            "params": {"sessionId": "s1", "direction": "head", "limit": 3},
        },
        resolver=AllowAllResolver(),
    )
    result = resp["result"]
    assert [m["content"] for m in result["messages"]] == ["m0", "m1", "m2"]
    assert result["has_more"] is True

    # `next_cursor_id` is the forward-navigation anchor in HEAD direction.
    cursor = result["next_cursor_id"]
    resp2 = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get_page",
            "params": {
                "sessionId": "s1",
                "direction": "head",
                "limit": 3,
                "cursorId": cursor,
            },
        },
        resolver=AllowAllResolver(),
    )
    assert [m["content"] for m in resp2["result"]["messages"]] == ["m3", "m4", "m5"]


def test_message_get_page_rejects_invalid_direction():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get_page",
            "params": {"sessionId": "s1", "direction": "sideways"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_message_search_fts_finds_matching_content():
    conn, repo, registry = _wired()
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="hello world"))
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="goodbye"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.search_fts",
            "params": {"query": "hello", "sessionId": "s1"},
        },
        resolver=AllowAllResolver(),
    )
    contents = [m["content"] for m in resp["result"]["messages"]]
    assert contents == ["hello world"]


def test_message_search_fts_requires_query():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "message.search_fts", "params": {"query": ""}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_message_merge_metadata_shallow_merges():
    conn, repo, registry = _wired()
    msg = repo.append(
        "s1", MessageSpec(session_id="s1", role="user", metadata={"a": 1})
    )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.merge_metadata",
            "params": {
                "sessionId": "s1",
                "messageId": msg.id,
                "patch": {"b": 2},
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["metadata"] == {"a": 1, "b": 2}


def test_message_merge_metadata_missing_returns_message_not_found():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.merge_metadata",
            "params": {"sessionId": "s1", "messageId": 9999, "patch": {"x": 1}},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.MESSAGE_NOT_FOUND.value


def test_message_write_permission_denied_returns_4003():
    conn, repo, registry = _wired()

    class _ReadOnly:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.append",
            "params": {"sessionId": "s1", "role": "user", "content": "hi"},
        },
        resolver=_ReadOnly(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


def test_message_get_page_alias_fold_conversation_session_id():
    conn, repo, registry = _wired()
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="m"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get_page",
            "params": {"conversationSessionId": "s1"},  # legacy alias
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["session_id"] == "s1"
