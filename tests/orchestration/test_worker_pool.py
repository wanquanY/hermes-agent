"""Phase F — WorkerPool single-replica in-flight state (spec §8.1)."""

from __future__ import annotations

import pytest

from hermes_agent.domain.run_identity import CrossWiredRunError
from hermes_agent.orchestration import InflightRun, WorkerPool


def test_record_run_start_creates_inflight_entry():
    pool = WorkerPool()
    record = pool.record_run_start(
        worker_id="w1",
        run_id="r1",
        session_id="s1",
        turn_id="t1",
    )
    assert isinstance(record, InflightRun)
    assert pool.size() == 1
    assert pool.get("r1") == record
    assert pool.runs_for_worker("w1") == [record]


def test_record_run_terminal_removes_entry():
    pool = WorkerPool()
    pool.record_run_start(worker_id="w1", run_id="r1", session_id="s1")
    reaped = pool.record_run_terminal("r1")
    assert reaped is not None and reaped.run_id == "r1"
    assert pool.size() == 0
    assert pool.get("r1") is None
    assert pool.runs_for_worker("w1") == []


def test_record_run_terminal_unknown_run_returns_none():
    pool = WorkerPool()
    assert pool.record_run_terminal("unknown") is None


def test_worker_swap_is_rejected_as_cross_wired_identity():
    pool = WorkerPool()
    original = pool.record_run_start(
        worker_id="w1", run_id="r1", session_id="s1", now=10
    )

    with pytest.raises(CrossWiredRunError) as raised:
        pool.record_run_start(worker_id="w2", run_id="r1", session_id="s1")

    assert raised.value.mismatch_fields == ("worker_id",)
    assert pool.get("r1") is original
    assert pool.runs_for_worker("w1") == [original]
    assert pool.runs_for_worker("w2") == []


def test_same_identity_retry_preserves_original_allocation():
    pool = WorkerPool()
    original = pool.record_run_start(
        worker_id="w1",
        run_id="r1",
        session_id="s1",
        runtime_scope_key="scope-1",
        agent_profile_id="profile-1",
        now=10,
    )

    retried = pool.record_run_start(
        worker_id="w1",
        run_id="r1",
        session_id="s1",
        runtime_scope_key="scope-1",
        agent_profile_id="profile-1",
        now=99,
    )

    assert retried is original
    assert retried.allocated_at == 10


def test_runs_for_worker_lists_sorted():
    pool = WorkerPool()
    pool.record_run_start(worker_id="w1", run_id="rc", session_id="s1")
    pool.record_run_start(worker_id="w1", run_id="ra", session_id="s1")
    pool.record_run_start(worker_id="w1", run_id="rb", session_id="s1")
    got = [r.run_id for r in pool.runs_for_worker("w1")]
    assert got == ["ra", "rb", "rc"]


def test_workers_lists_active_worker_ids():
    pool = WorkerPool()
    pool.record_run_start(worker_id="w1", run_id="r1", session_id="s1")
    pool.record_run_start(worker_id="w2", run_id="r2", session_id="s2")
    assert pool.workers() == ["w1", "w2"]
    pool.record_run_terminal("r1")
    assert pool.workers() == ["w2"]


def test_record_run_start_rejects_missing_ids():
    pool = WorkerPool()
    with pytest.raises(ValueError):
        pool.record_run_start(worker_id="", run_id="r1", session_id="s1")
    with pytest.raises(ValueError):
        pool.record_run_start(worker_id="w1", run_id="", session_id="s1")
    with pytest.raises(ValueError):
        pool.record_run_start(worker_id="w1", run_id="r1", session_id="")
