"""Phase G — edge methods: ``agent_profile.list`` + ``message.get``."""

from __future__ import annotations

import sqlite3

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods import agent_profile_methods, message_methods
from hermes_agent.repositories import (
    AgentProfileRepoImpl,
    MessageRepoImpl,
    MessageSpec,
    ProfileSpec,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE agent_profiles (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            avatar TEXT,
            description TEXT,
            category TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            is_system_default INTEGER NOT NULL DEFAULT 0,
            hermes_profile_name TEXT,
            hermes_home_path TEXT NOT NULL DEFAULT '',
            default_model TEXT,
            current_version_id TEXT NOT NULL DEFAULT '',
            current_version_number INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE agent_profile_versions (
            profile_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            payload_json TEXT,
            created_at REAL NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (profile_id, version_id)
        );
        CREATE TABLE agent_profile_growth_summary (
            profile_id TEXT PRIMARY KEY,
            total_runs INTEGER NOT NULL DEFAULT 0,
            total_messages INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            growth_score REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
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
    profile_repo = AgentProfileRepoImpl(conn)
    message_repo = MessageRepoImpl(conn)
    registry = MethodRegistry()

    def provider(_):
        return conn

    agent_profile_methods.register(registry, profile_repo, conn_provider=provider)
    message_methods.register(registry, message_repo, conn_provider=provider)
    return conn, profile_repo, message_repo, registry


# --- agent_profile.list ---


def test_agent_profile_list_returns_all_profiles():
    conn, profile_repo, _, registry = _wired()
    for i in range(3):
        profile_repo.create_profile(
            ProfileSpec(
                profile_id=f"p{i}",
                slug=f"s{i}",
                name=f"n{i}",
                hermes_home_path=f"/tmp/{i}",
            )
        )
    resp = dispatch(
        registry,
        {"id": "req", "method": "agent_profile.list", "params": {}},
        resolver=AllowAllResolver(),
    )
    ids = [p["profile_id"] for p in resp["result"]["profiles"]]
    assert ids == ["p0", "p1", "p2"]


def test_agent_profile_list_filters_by_status():
    conn, profile_repo, _, registry = _wired()
    for i, status in enumerate(("active", "archived", "active")):
        profile_repo.create_profile(
            ProfileSpec(
                profile_id=f"p{i}",
                slug=f"s{i}",
                name=f"n{i}",
                hermes_home_path=f"/tmp/{i}",
                status=status,
            )
        )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.list",
            "params": {"status": "active"},
        },
        resolver=AllowAllResolver(),
    )
    ids = {p["profile_id"] for p in resp["result"]["profiles"]}
    assert ids == {"p0", "p2"}


def test_agent_profile_list_respects_limit():
    conn, profile_repo, _, registry = _wired()
    for i in range(5):
        profile_repo.create_profile(
            ProfileSpec(
                profile_id=f"p{i}",
                slug=f"s{i}",
                name=f"n{i}",
                hermes_home_path=f"/tmp/{i}",
            )
        )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.list",
            "params": {"limit": 2},
        },
        resolver=AllowAllResolver(),
    )
    assert len(resp["result"]["profiles"]) == 2


def test_agent_profile_list_rejects_non_integer_limit():
    conn, profile_repo, _, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.list",
            "params": {"limit": "not-a-number"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


# --- message.get ---


def test_message_get_returns_full_projection():
    conn, _, message_repo, registry = _wired()
    m = message_repo.append(
        "s1",
        MessageSpec(
            session_id="s1",
            role="user",
            content="hi",
            metadata={"origin": "test"},
        ),
    )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get",
            "params": {"sessionId": "s1", "messageId": m.id},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert result["id"] == m.id
    assert result["content"] == "hi"
    assert result["metadata"] == {"origin": "test"}


def test_message_get_missing_returns_5008():
    conn, _, _, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get",
            "params": {"sessionId": "s1", "messageId": 9999},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.MESSAGE_NOT_FOUND.value


def test_message_get_rejects_missing_ids():
    conn, _, _, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get",
            "params": {"sessionId": "s1"},  # missing message_id
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_message_get_rejects_non_integer_message_id():
    conn, _, _, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "message.get",
            "params": {"sessionId": "s1", "messageId": "not-a-number"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value
