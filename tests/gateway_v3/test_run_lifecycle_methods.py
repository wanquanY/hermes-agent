"""Phase F/G — ``run.launch`` + ``run.terminate`` end-to-end dispatch."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods.run_methods import register_lifecycle
from hermes_agent.orchestration import RunOrchestrator, WorkerPool
from hermes_agent.storage.state_schema import SCHEMA_SQL


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, updated_at) "
        "VALUES ('s1', 'test', 0, 0)"
    )
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
        "VALUES ('r1', 's1', 'running', 0, 0)"
    )
    conn.commit()
    return conn


def _wired():
    conn = _make_conn()
    orch = RunOrchestrator(WorkerPool())
    registry = MethodRegistry()

    def provider(session_id: str) -> sqlite3.Connection:
        assert session_id == "s1"
        return conn

    register_lifecycle(registry, orch, provider)
    return conn, orch, registry


def test_run_launch_returns_start_seq_and_inflight():
    conn, orch, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "workerId": "w1",
                "turnId": "t1",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert result["run_id"] == "r1"
    assert result["start_seq"] == 1
    assert orch.pool.size() == 1


def test_run_launch_cross_wire_returns_5003_without_second_event():
    conn, orch, registry = _wired()
    first = dispatch(
        registry,
        {
            "id": "first",
            "method": "run.launch",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "workerId": "w1",
                "agentProfileId": "profile-1",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in first

    conflict = dispatch(
        registry,
        {
            "id": "second",
            "method": "run.launch",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "workerId": "w2",
                "agentProfileId": "profile-2",
            },
        },
        resolver=AllowAllResolver(),
    )

    assert conflict["error"]["code"] == ErrorCode.RUN_STATE_CONFLICT.value
    assert set(conflict["error"]["details"]["mismatch_fields"]) == {
        "worker_id",
        "agent_profile_id",
    }
    assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1
    assert orch.pool.get("r1").worker_id == "w1"


def test_run_launch_rejects_missing_ids():
    conn, orch, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {"runId": "r1", "sessionId": "s1"},  # missing worker_id
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_run_terminate_applies_and_clears_inflight():
    conn, orch, registry = _wired()
    dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "workerId": "w1",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert orch.pool.size() == 1

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.terminate",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "targetStatus": "completed",
                "cause": "worker_emitted",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert result["outcome"] == "applied"
    assert result["terminal_status"] == "completed"
    assert result["terminal_seq"] > 0
    assert result["degraded"] is False
    assert orch.pool.size() == 0


def test_run_terminate_second_call_is_idempotent_skip():
    conn, orch, registry = _wired()
    dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {"runId": "r1", "sessionId": "s1", "workerId": "w1"},
        },
        resolver=AllowAllResolver(),
    )
    dispatch(
        registry,
        {
            "id": "req",
            "method": "run.terminate",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "targetStatus": "completed",
            },
        },
        resolver=AllowAllResolver(),
    )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.terminate",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "targetStatus": "failed",
                "cause": "worker_crashed",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["outcome"] == "idempotent_skip"
    assert resp["result"]["terminal_status"] == "completed"  # unchanged


def test_run_terminate_rejects_unknown_cause():
    conn, orch, registry = _wired()
    dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {"runId": "r1", "sessionId": "s1", "workerId": "w1"},
        },
        resolver=AllowAllResolver(),
    )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.terminate",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "targetStatus": "completed",
                "cause": "not-a-real-cause",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value
    assert "not-a-real-cause" in resp["error"]["message"]


def test_run_terminate_rejects_non_terminal_target():
    conn, orch, registry = _wired()
    dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {"runId": "r1", "sessionId": "s1", "workerId": "w1"},
        },
        resolver=AllowAllResolver(),
    )
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.terminate",
            "params": {
                "runId": "r1",
                "sessionId": "s1",
                "targetStatus": "running",  # not terminal
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_run_launch_denied_when_permission_write_missing():
    conn, orch, registry = _wired()

    class _ReadOnly:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {"runId": "r1", "sessionId": "s1", "workerId": "w1"},
        },
        resolver=_ReadOnly(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


def test_run_launch_identity_alias_fold_at_dispatch_boundary():
    """`conversationSessionId` still normalises to `session_id` for the write path."""
    conn, orch, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.launch",
            "params": {
                "runId": "r1",
                "conversationSessionId": "s1",  # legacy alias
                "workerId": "w1",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["session_id"] == "s1"
