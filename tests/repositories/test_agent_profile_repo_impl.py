"""Phase D2 — AgentProfileRepoImpl concrete behavior (spec §4.5)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_agent.repositories import (
    AgentProfileRepo,
    AgentProfileRepoImpl,
    GrowthSummary,
    Profile,
    ProfileSpec,
    ProfileVersion,
    ensure_agent_profile_repository_schema,
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
        """
    )
    conn.commit()
    return conn


def test_impl_is_structural_agent_profile_repo():
    repo = AgentProfileRepoImpl(_make_conn())
    assert isinstance(repo, AgentProfileRepo)


def test_create_profile_persists_row_and_returns_projection():
    conn = _make_conn()
    repo = AgentProfileRepoImpl(conn)
    got = repo.create_profile(
        ProfileSpec(
            profile_id="p1",
            slug="assistant",
            name="Assistant",
            hermes_home_path="/tmp/p1",
            category="general",
            tags=("beta", "cn"),
            description="a description",
        )
    )
    assert isinstance(got, Profile)
    assert got.profile_id == "p1"
    assert got.slug == "assistant"
    assert got.tags == ("beta", "cn")
    row = conn.execute(
        "SELECT id, slug, name, tags_json FROM agent_profiles WHERE id='p1'"
    ).fetchone()
    assert row["id"] == "p1"
    assert row["slug"] == "assistant"
    assert json.loads(row["tags_json"]) == ["beta", "cn"]


def test_create_profile_requires_id_slug_name():
    repo = AgentProfileRepoImpl(_make_conn())
    with pytest.raises(ValueError):
        repo.create_profile(ProfileSpec(profile_id="", slug="s", name="n", hermes_home_path="/tmp"))
    with pytest.raises(ValueError):
        repo.create_profile(ProfileSpec(profile_id="p", slug="", name="n", hermes_home_path="/tmp"))
    with pytest.raises(ValueError):
        repo.create_profile(ProfileSpec(profile_id="p", slug="s", name="", hermes_home_path="/tmp"))


def test_get_returns_none_when_missing():
    repo = AgentProfileRepoImpl(_make_conn())
    assert repo.get("missing") is None


def test_add_version_marks_only_one_current():
    conn = _make_conn()
    repo = AgentProfileRepoImpl(conn)
    repo.create_profile(ProfileSpec(profile_id="p1", slug="s", name="n", hermes_home_path="/tmp"))
    repo.add_version(
        "p1",
        ProfileVersion(profile_id="p1", version_id="v1", version_number=1, is_current=True),
    )
    repo.add_version(
        "p1",
        ProfileVersion(profile_id="p1", version_id="v2", version_number=2, is_current=True),
    )
    row = conn.execute(
        "SELECT current_version_id, current_version_number FROM agent_profiles WHERE id='p1'"
    ).fetchone()
    assert row["current_version_id"] == "v2"
    assert row["current_version_number"] == 2
    # Only v2 is_current, v1 flipped to 0.
    v1 = conn.execute(
        "SELECT is_current FROM agent_profile_versions WHERE profile_id='p1' AND version_id='v1'"
    ).fetchone()
    v2 = conn.execute(
        "SELECT is_current FROM agent_profile_versions WHERE profile_id='p1' AND version_id='v2'"
    ).fetchone()
    assert v1["is_current"] == 0
    assert v2["is_current"] == 1


def test_get_growth_summary_returns_default_when_missing():
    repo = AgentProfileRepoImpl(_make_conn())
    got = repo.get_growth_summary("p-missing")
    assert isinstance(got, GrowthSummary)
    assert got.profile_id == "p-missing"
    assert got.total_runs == 0


def test_get_growth_summary_reads_persisted_row():
    conn = _make_conn()
    repo = AgentProfileRepoImpl(conn)
    conn.execute(
        """
        INSERT INTO agent_profile_growth_summary (
            profile_id, total_runs, total_messages, total_tokens,
            growth_score, updated_at
        ) VALUES ('p1', 10, 42, 1234, 0.75, 1700000000)
        """
    )
    got = repo.get_growth_summary("p1")
    assert got.total_runs == 10
    assert got.total_messages == 42
    assert got.total_tokens == 1234
    assert got.growth_score == 0.75


def test_add_version_requires_ids():
    repo = AgentProfileRepoImpl(_make_conn())
    with pytest.raises(ValueError):
        repo.add_version(
            "",
            ProfileVersion(profile_id="", version_id="v", version_number=1),
        )
    with pytest.raises(ValueError):
        repo.add_version(
            "p",
            ProfileVersion(profile_id="p", version_id="", version_number=1),
        )


def _make_full_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_agent_profile_repository_schema(conn)
    return conn


def test_full_registry_profile_crud_and_drafts_are_repo_owned():
    conn = _make_full_conn()
    repo = AgentProfileRepoImpl(conn)

    profile = repo.upsert_agent_profile(
        profile_id="agent-1",
        slug="research-agent",
        name="Research Agent",
        hermes_home_path="/tmp/profiles/research-agent",
        default_model="gpt-5",
        default_provider="openai",
        default_toolsets=["file", "terminal"],
        recommended_skills=["search"],
        current_version_id="snapshot-2",
        current_version_number=2,
        source_kind="dovie-public-market",
        public_profile_id="public-profile-1",
        public_version_id="public-version-2",
        public_content_hash="sha256:abc",
        metadata={"marketInstall": {"installedAt": "2026-06-14T04:00:00Z"}},
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T03:00:00Z",
    )
    assert profile["id"] == "agent-1"
    assert profile["runtimeScopeKey"] == "profile:agent-1"
    assert profile["agentProfileVersionId"] == "snapshot-2"
    assert repo.get_agent_profile_by_slug("research-agent")["id"] == "agent-1"
    assert repo.list_agent_profiles()[0]["recommendedSkills"] == ["search"]

    draft = repo.upsert_agent_profile_draft(
        draft_id="draft-1",
        draft_kind="revision",
        base_agent_profile_id="agent-1",
        target_agent_profile_id="agent-1",
        source_session_id="session-1",
        source_agent_profile_id="agent-default",
        name="Research Agent draft",
        recommended_toolsets=["file"],
        recommended_skills=["search"],
        files={"soulMarkdown": "# Research Agent\n"},
        created_at="2026-06-14T02:00:00Z",
        updated_at="2026-06-14T02:00:00Z",
    )
    assert draft["id"] == "draft-1"
    assert draft["draftKind"] == "revision"
    assert repo.list_agent_profile_drafts(source_session_id="session-1")[0]["id"] == "draft-1"
    assert repo.discard_agent_profile_draft("draft-1")["status"] == "discarded"
    assert repo.list_agent_profile_drafts(source_session_id="session-1") == []
    assert repo.archive_agent_profile("agent-1")["status"] == "archived"
    assert repo.list_agent_profiles() == []
    assert repo.list_agent_profiles(include_archived=True)[0]["id"] == "agent-1"
