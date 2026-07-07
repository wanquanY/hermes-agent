"""Phase G — ``agent_profile.*`` gateway methods E2E."""

from __future__ import annotations

import sqlite3

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods import agent_profile_methods
from hermes_agent.repositories import AgentProfileRepoImpl, ProfileSpec


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
        """
    )
    conn.commit()
    return conn


def _wired():
    conn = _make_conn()
    repo = AgentProfileRepoImpl(conn)
    registry = MethodRegistry()
    agent_profile_methods.register(registry, repo)
    return conn, repo, registry


def test_agent_profile_create_persists_and_returns_projection():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.create",
            "params": {
                "profileId": "p1",
                "slug": "assistant",
                "name": "Assistant",
                "hermesHomePath": "/tmp/p1",
                "tags": ["beta", "cn"],
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["profile_id"] == "p1"
    assert resp["result"]["tags"] == ["beta", "cn"]


def test_agent_profile_create_rejects_missing_required_fields():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.create",
            "params": {"profileId": "p1", "slug": "s"},  # missing name + home
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_agent_profile_create_rejects_non_list_tags():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.create",
            "params": {
                "profileId": "p1",
                "slug": "s",
                "name": "n",
                "hermesHomePath": "/tmp",
                "tags": "not-a-list",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_agent_profile_get_returns_stored_projection():
    conn, repo, registry = _wired()
    repo.create_profile(
        ProfileSpec(
            profile_id="p1", slug="s", name="Name", hermes_home_path="/tmp"
        )
    )
    resp = dispatch(
        registry,
        {"id": "req", "method": "agent_profile.get", "params": {"profileId": "p1"}},
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["profile_id"] == "p1"


def test_agent_profile_get_missing_returns_not_found():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.get",
            "params": {"profileId": "missing"},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" in resp


def test_agent_profile_add_version_returns_summary():
    conn, repo, registry = _wired()
    repo.create_profile(
        ProfileSpec(profile_id="p1", slug="s", name="n", hermes_home_path="/tmp")
    )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.add_version",
            "params": {
                "profileId": "p1",
                "versionId": "v1",
                "versionNumber": 1,
                "isCurrent": True,
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["version_id"] == "v1"
    assert resp["result"]["version_number"] == 1
    assert resp["result"]["is_current"] is True


def test_agent_profile_add_version_rejects_missing_ids():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.add_version",
            "params": {"profileId": "p1", "versionNumber": 1},  # missing version_id
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_agent_profile_add_version_rejects_non_int_version_number():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.add_version",
            "params": {
                "profileId": "p1",
                "versionId": "v1",
                "versionNumber": "not-a-number",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_agent_profile_growth_summary_defaults_when_missing():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.growth_summary",
            "params": {"profileId": "p-missing"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["total_runs"] == 0


def test_agent_profile_write_denied_when_read_only_resolver():
    conn, repo, registry = _wired()

    class _RO:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "agent_profile.create",
            "params": {
                "profileId": "p1",
                "slug": "s",
                "name": "n",
                "hermesHomePath": "/tmp",
            },
        },
        resolver=_RO(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value
