"""Phase F — TeamMissionOrchestrator graph execution (spec §3, §4.4)."""

from __future__ import annotations

import sqlite3

import pytest

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


def _make_orch() -> tuple[TeamMissionOrchestrator, TeamMissionRepoImpl, sqlite3.Connection]:
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    return TeamMissionOrchestrator(repo), repo, conn


def _linear_mission(orch, repo, ids):
    """Build a mission whose nodes form a straight chain node[0] → node[1] → …"""
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    for nid in ids:
        repo.add_node(NodeSpec(mission_id="m1", node_id=nid))
    for a, b in zip(ids, ids[1:]):
        repo.add_edge(EdgeSpec(mission_id="m1", from_node_id=a, to_node_id=b))


def test_ready_nodes_returns_only_pending_with_terminal_predecessors():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a", "b", "c"])

    # Only 'a' is ready — 'b' and 'c' are gated by their predecessors.
    ready = orch.ready_nodes("m1")
    assert [n.node_id for n in ready] == ["a"]


def test_ready_nodes_missing_mission_returns_empty():
    orch, _, _ = _make_orch()
    assert orch.ready_nodes("no-such") == []


def test_mark_node_running_binds_run_and_flips_status():
    orch, repo, conn = _make_orch()
    _linear_mission(orch, repo, ["a"])
    orch.mark_node_running("m1", "a", "run-1")
    node = repo.get_node("m1", "a")
    assert node.status == "running"
    assert node.bound_run_id == "run-1"


def test_advance_after_terminal_opens_next_node():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a", "b", "c"])
    orch.mark_node_running("m1", "a", "run-a")

    outcome = orch.advance_after_terminal_node(
        "m1", "a", "completed", session_id="s1"
    )
    assert isinstance(outcome, MissionAdvanceOutcome)
    assert outcome.terminated_node_id == "a"
    assert outcome.newly_ready_nodes == ("b",)
    assert len(outcome.activity_ids_recorded) == 1

    # 'a' persisted as completed; 'b' is now on deck; 'c' still pending.
    assert repo.get_node("m1", "a").status == "completed"


def test_advance_records_dispatch_completion_activity_with_shared_seq():
    orch, repo, conn = _make_orch()
    _linear_mission(orch, repo, ["a"])
    orch.mark_node_running("m1", "a", "run-a")

    outcome = orch.advance_after_terminal_node(
        "m1", "a", "completed", session_id="s1"
    )
    assert len(outcome.activity_ids_recorded) == 1

    row = conn.execute(
        "SELECT kind, activity_seq FROM v3_activities WHERE session_id='s1'"
    ).fetchone()
    assert row["kind"] == "dispatch_completion"
    # activity_seq shares the run_events SeqAllocator domain — starts at 1
    # for a fresh session with no prior events.
    assert row["activity_seq"] == 1


def test_advance_rejects_non_terminal_status():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a"])
    with pytest.raises(ValueError):
        orch.advance_after_terminal_node("m1", "a", "running", session_id="s1")


def test_mission_terminal_status_none_when_still_running():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a", "b"])
    assert orch.mission_terminal_status("m1") is None


def test_mission_terminal_status_completed_when_all_terminal_and_any_completed():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a", "b"])
    orch.mark_node_running("m1", "a", "run-a")
    orch.advance_after_terminal_node("m1", "a", "completed", session_id="s1")
    orch.mark_node_running("m1", "b", "run-b")
    orch.advance_after_terminal_node("m1", "b", "completed", session_id="s1")
    assert orch.mission_terminal_status("m1") == "completed"


def test_mission_terminal_status_failed_when_any_node_failed():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a", "b"])
    orch.mark_node_running("m1", "a", "run-a")
    orch.advance_after_terminal_node("m1", "a", "completed", session_id="s1")
    orch.mark_node_running("m1", "b", "run-b")
    orch.advance_after_terminal_node("m1", "b", "failed", session_id="s1")
    assert orch.mission_terminal_status("m1") == "failed"


def test_mission_terminal_status_cancelled_when_all_cancelled():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a", "b"])
    orch.mark_node_running("m1", "a", "run-a")
    orch.advance_after_terminal_node("m1", "a", "cancelled", session_id="s1")
    orch.mark_node_running("m1", "b", "run-b")
    orch.advance_after_terminal_node("m1", "b", "cancelled", session_id="s1")
    assert orch.mission_terminal_status("m1") == "cancelled"


def test_mark_node_running_rejects_missing_ids():
    orch, repo, _ = _make_orch()
    _linear_mission(orch, repo, ["a"])
    with pytest.raises(ValueError):
        orch.mark_node_running("", "a", "run-1")
    with pytest.raises(ValueError):
        orch.mark_node_running("m1", "", "run-1")
    with pytest.raises(ValueError):
        orch.mark_node_running("m1", "a", "")


def test_diamond_topology_ready_after_both_predecessors_terminal():
    """Graph:  a → b, a → c, b → d, c → d  — d is ready only after b AND c."""
    orch, repo, _ = _make_orch()
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    for nid in ("a", "b", "c", "d"):
        repo.add_node(NodeSpec(mission_id="m1", node_id=nid))
    for a, b in (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")):
        repo.add_edge(EdgeSpec(mission_id="m1", from_node_id=a, to_node_id=b))

    orch.mark_node_running("m1", "a", "run-a")
    orch.advance_after_terminal_node("m1", "a", "completed", session_id="s1")

    ready_after_a = {n.node_id for n in orch.ready_nodes("m1")}
    assert ready_after_a == {"b", "c"}, "b and c both wait only on a"

    orch.mark_node_running("m1", "b", "run-b")
    orch.advance_after_terminal_node("m1", "b", "completed", session_id="s1")

    ready_after_b = {n.node_id for n in orch.ready_nodes("m1")}
    assert ready_after_b == {"c"}, "d still needs c"

    orch.mark_node_running("m1", "c", "run-c")
    orch.advance_after_terminal_node("m1", "c", "completed", session_id="s1")

    ready_after_c = {n.node_id for n in orch.ready_nodes("m1")}
    assert ready_after_c == {"d"}
