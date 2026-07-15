"""Phase K — property-style tests for TeamMissionRepoImpl (spec §4.4)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.repositories import (
    ActivitySpec,
    MissionSpec,
    NodeSpec,
    TeamMissionRepoImpl,
)


PROPERTY_ROUNDS = 24

_ACTIVITY_KINDS = (
    "async_agent_dispatch",
    "async_team_dispatch",
    "team_mission_activity",
    "dispatch_completion",
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE team_missions (
            mission_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            plan_json TEXT,
            metadata_json TEXT
        );
        CREATE TABLE team_mission_nodes (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL,
            bound_run_id TEXT NOT NULL DEFAULT '',
            plan_json TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (mission_id, node_id)
        );
        CREATE TABLE team_mission_edges (
            mission_id TEXT NOT NULL,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'sequence',
            PRIMARY KEY (mission_id, from_node_id, to_node_id)
        );
        CREATE TABLE team_mission_run_bindings (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            bound_at REAL NOT NULL,
            PRIMARY KEY (mission_id, node_id)
        );
        CREATE TABLE v3_activities (
            activity_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            activity_seq INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            target_id TEXT NOT NULL DEFAULT '',
            prompt_summary TEXT,
            result_summary TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL DEFAULT 0,
            completed_at REAL,
            metadata_json TEXT
        );
        CREATE TABLE run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL DEFAULT '',
            timestamp REAL NOT NULL DEFAULT 0,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE seq_counter (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    conn.commit()
    return conn


def test_property_activity_seq_strict_monotonic_regardless_of_kind_mix():
    rng = random.Random(20260731)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        n = rng.randint(2, 20)
        seqs = []
        for i in range(n):
            act = repo.append_activity(
                "s1",
                ActivitySpec(
                    activity_id=f"a-{round_idx}-{i}",
                    session_id="s1",
                    kind=rng.choice(_ACTIVITY_KINDS),
                ),
            )
            seqs.append(act.activity_seq)
        # All strictly increasing.
        assert seqs == sorted(set(seqs))
        conn.close()


def test_property_list_activities_after_seq_cursor_walks_every_activity():
    rng = random.Random(20260732)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        n = rng.randint(2, 20)
        added: list[str] = []
        for i in range(n):
            act = repo.append_activity(
                "s1",
                ActivitySpec(
                    activity_id=f"a-{round_idx}-{i}",
                    session_id="s1",
                    kind=rng.choice(_ACTIVITY_KINDS),
                ),
            )
            added.append(act.activity_id)

        visited: list[str] = []
        after = 0
        while True:
            page = repo.list_activities("s1", after_seq=after, limit=max(1, n // 3))
            if not page:
                break
            visited.extend(a.activity_id for a in page)
            after = page[-1].activity_seq
        assert visited == added
        conn.close()


def test_property_list_activities_kind_filter():
    rng = random.Random(20260733)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        expected_by_kind: dict[str, int] = {k: 0 for k in _ACTIVITY_KINDS}
        for i in range(rng.randint(5, 25)):
            kind = rng.choice(_ACTIVITY_KINDS)
            expected_by_kind[kind] += 1
            repo.append_activity(
                "s1",
                ActivitySpec(
                    activity_id=f"a-{round_idx}-{i}",
                    session_id="s1",
                    kind=kind,
                ),
            )
        # Pick a random subset of kinds to filter on.
        target_kinds = set(rng.sample(_ACTIVITY_KINDS, rng.randint(1, 4)))
        got = repo.list_activities("s1", kinds=target_kinds, limit=1000)
        expected_total = sum(expected_by_kind[k] for k in target_kinds)
        assert len(got) == expected_total
        assert {a.kind for a in got} <= target_kinds
        conn.close()


def test_property_update_activity_status_sets_completed_at_on_terminal_only():
    rng = random.Random(20260734)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        act = repo.append_activity(
            "s1",
            ActivitySpec(
                activity_id=f"a-{round_idx}",
                session_id="s1",
                kind=rng.choice(_ACTIVITY_KINDS),
            ),
        )
        # First set to 'running' — completed_at stays None.
        updated = repo.update_activity_status(act.activity_id, "running")
        assert updated.status == "running"
        assert updated.completed_at is None
        # Terminal transitions must populate completed_at.
        terminal = rng.choice(("completed", "failed", "cancelled"))
        final = repo.update_activity_status(act.activity_id, terminal)
        assert final.status == terminal
        assert final.completed_at is not None
        conn.close()


def test_property_bind_run_sets_bound_run_id_on_node():
    rng = random.Random(20260735)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        mission_id = f"m-{round_idx}"
        repo.create_mission("s1", MissionSpec(mission_id=mission_id, session_id="s1"))
        n_nodes = rng.randint(1, 6)
        for i in range(n_nodes):
            repo.add_node(NodeSpec(mission_id=mission_id, node_id=f"n{i}"))
        # Bind runs to random subset of nodes.
        binds = {}
        for i in range(n_nodes):
            if rng.random() < 0.7:
                run_id = f"run-{round_idx}-{i}"
                repo.bind_run(mission_id, f"n{i}", run_id)
                binds[f"n{i}"] = run_id
        for node_id, expected_run in binds.items():
            got = repo.get_node(mission_id, node_id)
            assert got.bound_run_id == expected_run
        conn.close()
