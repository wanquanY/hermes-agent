"""Phase K — property-style tests for TeamMissionOrchestrator (spec §3, §4.4)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.orchestration import (
    MissionAdvanceOutcome,
    TeamMissionOrchestrator,
)
from hermes_agent.repositories import (
    EdgeSpec,
    MissionSpec,
    NodeSpec,
    TeamMissionRepoImpl,
)


PROPERTY_ROUNDS = 24


def _make_conn(session_id: str = "s1") -> sqlite3.Connection:
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
    conn.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
    conn.commit()
    return conn


def _random_dag(rng: random.Random, n_nodes: int) -> tuple[list[str], list[tuple[str, str]]]:
    """Generate a random DAG of ``n_nodes`` nodes.

    Node ids are ``"n0" .. "n{N-1}"``; edges only go from lower-index to
    higher-index nodes so the graph is always acyclic, then we sprinkle a
    few extra forward edges to model diamonds and fan-out/fan-in.
    """
    nodes = [f"n{i}" for i in range(n_nodes)]
    edges: list[tuple[str, str]] = []
    # A spanning line to guarantee reachability of every node from the root.
    for i in range(1, n_nodes):
        edges.append((nodes[i - 1], nodes[i]))
    # Optional extra forward edges (diamonds / fan-in).
    extra = rng.randint(0, n_nodes)
    for _ in range(extra):
        i = rng.randint(0, n_nodes - 2)
        j = rng.randint(i + 1, n_nodes - 1)
        if (nodes[i], nodes[j]) not in edges:
            edges.append((nodes[i], nodes[j]))
    return nodes, edges


def _seed_mission(
    repo: TeamMissionRepoImpl,
    mission_id: str,
    nodes: list[str],
    edges: list[tuple[str, str]],
) -> None:
    repo.create_mission("s1", MissionSpec(mission_id=mission_id, session_id="s1"))
    for nid in nodes:
        repo.add_node(NodeSpec(mission_id=mission_id, node_id=nid))
    for a, b in edges:
        repo.add_edge(EdgeSpec(mission_id=mission_id, from_node_id=a, to_node_id=b))


def _drive_to_completion(
    orch: TeamMissionOrchestrator,
    mission_id: str,
    rng: random.Random,
    status_of_node,
) -> int:
    """Advance every ready node in random order until no more are ready."""
    step_count = 0
    while True:
        ready = orch.ready_nodes(mission_id)
        if not ready:
            return step_count
        # Pick a random ready node.
        node = rng.choice(ready)
        orch.mark_node_running(mission_id, node.node_id, f"run-{node.node_id}")
        outcome = orch.advance_after_terminal_node(
            mission_id,
            node.node_id,
            status_of_node(node.node_id),
            session_id="s1",
        )
        assert isinstance(outcome, MissionAdvanceOutcome)
        step_count += 1


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


def test_property_random_dag_advance_terminates_every_node():
    rng = random.Random(20260711)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        orch = TeamMissionOrchestrator(repo)
        n_nodes = rng.randint(2, 8)
        nodes, edges = _random_dag(rng, n_nodes)
        _seed_mission(repo, f"m{round_idx}", nodes, edges)

        steps = _drive_to_completion(
            orch, f"m{round_idx}", rng, status_of_node=lambda _: "completed"
        )
        assert steps == n_nodes, (
            f"round {round_idx}: DAG size {n_nodes} vs advance steps {steps}"
        )
        assert orch.mission_terminal_status(f"m{round_idx}") == "completed"


def test_property_any_failed_node_promotes_mission_to_failed():
    rng = random.Random(20260712)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        orch = TeamMissionOrchestrator(repo)
        n_nodes = rng.randint(2, 8)
        nodes, edges = _random_dag(rng, n_nodes)
        mission_id = f"m{round_idx}"
        _seed_mission(repo, mission_id, nodes, edges)

        # Randomly designate exactly one node to fail; rest complete.
        failing = rng.choice(nodes)

        _drive_to_completion(
            orch,
            mission_id,
            rng,
            status_of_node=lambda nid, f=failing: (
                "failed" if nid == f else "completed"
            ),
        )
        assert orch.mission_terminal_status(mission_id) == "failed"


def test_property_all_cancelled_yields_cancelled_status():
    rng = random.Random(20260713)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        orch = TeamMissionOrchestrator(repo)
        n_nodes = rng.randint(2, 6)
        nodes, edges = _random_dag(rng, n_nodes)
        mission_id = f"m{round_idx}"
        _seed_mission(repo, mission_id, nodes, edges)

        _drive_to_completion(
            orch, mission_id, rng, status_of_node=lambda _: "cancelled"
        )
        assert orch.mission_terminal_status(mission_id) == "cancelled"


def test_property_activity_seq_strict_monotonic_across_advance_calls():
    """Every advance_after_terminal_node records exactly one
    dispatch_completion activity, and their activity_seq values are strictly
    increasing (spec §6.5 shared SeqAllocator domain).
    """
    rng = random.Random(20260714)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        orch = TeamMissionOrchestrator(repo)
        n_nodes = rng.randint(2, 8)
        nodes, edges = _random_dag(rng, n_nodes)
        mission_id = f"m{round_idx}"
        _seed_mission(repo, mission_id, nodes, edges)

        _drive_to_completion(
            orch, mission_id, rng, status_of_node=lambda _: "completed"
        )
        rows = conn.execute(
            "SELECT activity_seq FROM v3_activities "
            "WHERE session_id = 's1' ORDER BY activity_seq ASC"
        ).fetchall()
        seqs = [r["activity_seq"] for r in rows]
        assert len(seqs) == n_nodes
        assert seqs == sorted(set(seqs)), (
            f"round {round_idx}: activity_seq not strictly monotonic: {seqs}"
        )


def test_property_no_node_starts_before_predecessors_terminal():
    """For every ready-set snapshot, no returned node has a pending predecessor."""
    rng = random.Random(20260715)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = TeamMissionRepoImpl(conn)
        orch = TeamMissionOrchestrator(repo)
        n_nodes = rng.randint(2, 8)
        nodes, edges = _random_dag(rng, n_nodes)
        incoming: dict[str, list[str]] = {n: [] for n in nodes}
        for a, b in edges:
            incoming.setdefault(b, []).append(a)

        mission_id = f"m{round_idx}"
        _seed_mission(repo, mission_id, nodes, edges)
        terminal: set[str] = set()

        while True:
            ready = orch.ready_nodes(mission_id)
            if not ready:
                assert terminal == set(nodes), (
                    f"round {round_idx}: mission stalled with {set(nodes) - terminal} "
                    f"still pending"
                )
                break
            for r in ready:
                for pred in incoming[r.node_id]:
                    assert pred in terminal, (
                        f"round {round_idx}: node {r.node_id} became ready "
                        f"while predecessor {pred} was not terminal"
                    )
            picked = rng.choice(ready)
            orch.mark_node_running(mission_id, picked.node_id, "run-x")
            orch.advance_after_terminal_node(
                mission_id, picked.node_id, "completed", session_id="s1"
            )
            terminal.add(picked.node_id)
