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
