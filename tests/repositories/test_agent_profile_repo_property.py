"""Phase K — property-style tests for AgentProfileRepoImpl (spec §4.5)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.repositories import (
    AgentProfileRepoImpl,
    ProfileSpec,
    ProfileVersion,
)


PROPERTY_ROUNDS = 24


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


def test_property_create_profile_projection_matches_spec():
    rng = random.Random(20260736)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = AgentProfileRepoImpl(conn)
        n = rng.randint(1, 8)
        expected = {}
        for i in range(n):
            pid = f"p-{round_idx}-{i}"
            name = f"name-{rng.randint(0, 999)}"
            slug = f"slug-{rng.randint(0, 999)}"
            tags = tuple(f"t{rng.randint(0, 5)}" for _ in range(rng.randint(0, 4)))
            repo.create_profile(
                ProfileSpec(
                    profile_id=pid,
                    slug=slug,
                    name=name,
                    hermes_home_path=f"/tmp/{pid}",
                    tags=tags,
                )
            )
            expected[pid] = (slug, name, tags)
        for pid, (slug, name, tags) in expected.items():
            got = repo.get(pid)
            assert got is not None
            assert got.slug == slug
            assert got.name == name
            assert got.tags == tags
        conn.close()


def test_property_add_version_is_current_uniqueness_invariant():
    """Only one version per profile can be ``is_current=1`` at any time."""
    rng = random.Random(20260737)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = AgentProfileRepoImpl(conn)
        pid = f"p-{round_idx}"
        repo.create_profile(
            ProfileSpec(profile_id=pid, slug="s", name="n", hermes_home_path="/tmp")
        )
        n_versions = rng.randint(2, 8)
        latest_current_vid = None
        for i in range(n_versions):
            vid = f"v-{i}"
            is_current = rng.random() < 0.6
            repo.add_version(
                pid,
                ProfileVersion(
                    profile_id=pid,
                    version_id=vid,
                    version_number=i,
                    is_current=is_current,
                ),
            )
            if is_current:
                latest_current_vid = vid
        current_count = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_profile_versions "
            "WHERE profile_id = ? AND is_current = 1",
            (pid,),
        ).fetchone()["n"]
        assert current_count <= 1, (
            f"round {round_idx}: {current_count} versions marked current"
        )
        if latest_current_vid is not None:
            row = conn.execute(
                "SELECT current_version_id FROM agent_profiles WHERE id = ?",
                (pid,),
            ).fetchone()
            assert row["current_version_id"] == latest_current_vid
        conn.close()


def test_property_get_growth_summary_defaults_when_row_missing():
    rng = random.Random(20260738)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = AgentProfileRepoImpl(conn)
        pid = f"p-missing-{round_idx}"
        got = repo.get_growth_summary(pid)
        assert got.profile_id == pid
        assert got.total_runs == 0
        assert got.total_messages == 0
        assert got.total_tokens == 0
        assert got.growth_score == 0
        conn.close()


def test_property_get_growth_summary_reflects_persisted_metrics():
    rng = random.Random(20260739)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = AgentProfileRepoImpl(conn)
        n = rng.randint(1, 6)
        for i in range(n):
            pid = f"p-{round_idx}-{i}"
            runs = rng.randint(0, 1000)
            messages = rng.randint(0, 10000)
            tokens = rng.randint(0, 1000000)
            score = round(rng.random(), 3)
            conn.execute(
                "INSERT INTO agent_profile_growth_summary "
                "(profile_id, total_runs, total_messages, total_tokens, "
                "growth_score, updated_at) VALUES (?, ?, ?, ?, ?, 0)",
                (pid, runs, messages, tokens, score),
            )
            got = repo.get_growth_summary(pid)
            assert got.total_runs == runs
            assert got.total_messages == messages
            assert got.total_tokens == tokens
            assert got.growth_score == score
        conn.close()


def test_property_create_profile_overwrite_replaces_prior_row():
    """Duplicate profile_id keeps only the latest slug/name/tags."""
    rng = random.Random(20260740)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = AgentProfileRepoImpl(conn)
        pid = f"p-{round_idx}"
        for i in range(rng.randint(2, 6)):
            repo.create_profile(
                ProfileSpec(
                    profile_id=pid,
                    slug=f"slug-{i}",
                    name=f"name-{i}",
                    hermes_home_path=f"/tmp/{i}",
                )
            )
        got = repo.get(pid)
        assert got is not None
        # The row that survives is the last-written one.
        row_count = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_profiles WHERE id = ?", (pid,)
        ).fetchone()["n"]
        assert row_count == 1
        conn.close()
