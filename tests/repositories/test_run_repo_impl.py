"""Phase D3 — RunRepoImpl concrete behavior (spec §4.2)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.domain.run_terminator import TerminateCause, TerminateOutcome
from hermes_agent.repositories import (
    CanonicalEventSpec,
    Run,
    RunRepo,
    RunRepoImpl,
    RunSpec,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            runtime_scope_key TEXT,
            turn_id TEXT,
            runtime_session_id TEXT,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            last_seq INTEGER DEFAULT 0,
            terminal_seq INTEGER NOT NULL DEFAULT 0,
            terminal_degraded INTEGER NOT NULL DEFAULT 0,
            terminal_cause TEXT NOT NULL DEFAULT '',
            error TEXT,
            metadata_json TEXT
        );
        CREATE TABLE run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            turn_id TEXT,
            timestamp REAL NOT NULL,
            payload_json TEXT,
            event_json TEXT NOT NULL,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE seq_counter (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO sessions (id, source) VALUES ('s1', 'test')")
    conn.commit()
    return conn


def test_impl_is_structural_run_repo():
    repo = RunRepoImpl(_make_conn())
    assert isinstance(repo, RunRepo)


def test_create_and_get_run_roundtrip():
    conn = _make_conn()
    repo = RunRepoImpl(conn)
    created = repo.create_run("s1", RunSpec(run_id="r1", session_id="s1", turn_id="t1"))
    assert isinstance(created, Run)
    assert created.run_id == "r1"
    assert created.status == "running"
    got = repo.get_run("r1")
    assert got == created


def test_get_run_returns_none_missing():
    repo = RunRepoImpl(_make_conn())
    assert repo.get_run("no-such") is None


def test_append_event_allocates_monotonic_seq():
    conn = _make_conn()
    repo = RunRepoImpl(conn)
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    first = repo.append_event(
        "s1",
        CanonicalEventSpec(
            event_type="message.start",
            payload={"role": "assistant"},
            run_id="r1",
        ),
    )
    second = repo.append_event(
        "s1",
        CanonicalEventSpec(
            event_type="message.delta",
            payload={"text": "hi"},
            run_id="r1",
        ),
    )
    assert first == 1
    assert second == 2


def test_list_events_filters_internal_by_default():
    conn = _make_conn()
    repo = RunRepoImpl(conn)
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    repo.append_event(
        "s1",
        CanonicalEventSpec(event_type="message.start", payload={}, run_id="r1"),
    )
    repo.append_event(
        "s1",
        CanonicalEventSpec(
            event_type="_internal.interaction.requested",
            payload={"request_id": "req-1"},
            run_id="r1",
        ),
    )
    default = repo.list_events("s1")
    assert [e.event_type for e in default] == ["message.start"]
    with_internal = repo.list_events("s1", include_internal=True)
    assert [e.event_type for e in with_internal] == [
        "message.start",
        "_internal.interaction.requested",
    ]


def test_list_tool_events_only_tool_types():
    conn = _make_conn()
    repo = RunRepoImpl(conn)
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    for etype in (
        "message.start",
        "tool.start",
        "tool.progress",
        "tool.complete",
        "message.complete",
    ):
        repo.append_event(
            "s1",
            CanonicalEventSpec(event_type=etype, payload={}, run_id="r1"),
        )
    got = repo.list_tool_events("s1")
    assert {e.event_type for e in got} == {"tool.start", "tool.progress", "tool.complete"}


def test_set_terminal_uses_domain_terminate_run():
    conn = _make_conn()
    repo = RunRepoImpl(conn)
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    result = repo.set_terminal(
        run_id="r1",
        session_id="s1",
        target_status="completed",
        cause=TerminateCause.WORKER_EMITTED,
    )
    assert result.outcome is TerminateOutcome.APPLIED
    assert result.terminal_seq == 1
    got = repo.get_run("r1")
    assert got.status == "completed"
    assert got.terminal_seq == 1
    assert got.terminal_cause == "worker_emitted"


def test_set_terminal_idempotent_on_second_call():
    conn = _make_conn()
    repo = RunRepoImpl(conn)
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    repo.set_terminal(run_id="r1", session_id="s1", target_status="completed")
    result2 = repo.set_terminal(run_id="r1", session_id="s1", target_status="failed")
    assert result2.outcome is TerminateOutcome.IDEMPOTENT_SKIP
    assert result2.terminal_status == "completed"


def test_create_run_rejects_missing_ids():
    repo = RunRepoImpl(_make_conn())
    with pytest.raises(ValueError):
        repo.create_run("", RunSpec(run_id="r1", session_id="s1"))
    with pytest.raises(ValueError):
        repo.create_run("s1", RunSpec(run_id="", session_id="s1"))
