"""Tests for cron scheduler persistent pools and running-job guards."""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def isolated_scheduler(tmp_path, monkeypatch):
    import cron.scheduler as sched

    sched._shutdown_parallel_pool()
    sched._running_job_ids.clear()
    monkeypatch.setattr(sched, "_hermes_home", tmp_path / "hermes")
    yield sched
    sched._running_job_ids.clear()
    sched._shutdown_parallel_pool()


def _patch_job_io(monkeypatch, sched) -> None:
    monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
    monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "_deliver_dovie_bound_result", lambda *_a, **_kw: None)


class TestPersistentPool:
    def test_pool_is_reused(self, isolated_scheduler):
        sched = isolated_scheduler

        pool1 = sched._get_parallel_pool(4)
        pool2 = sched._get_parallel_pool(4)

        assert pool1 is pool2

    def test_pool_is_recreated_on_worker_change(self, isolated_scheduler):
        sched = isolated_scheduler

        pool1 = sched._get_parallel_pool(2)
        pool2 = sched._get_parallel_pool(4)

        assert pool1 is not pool2

    def test_shutdown_clears_pools(self, isolated_scheduler):
        sched = isolated_scheduler

        sched._get_parallel_pool(2)
        sched._get_sequential_pool()
        sched._shutdown_parallel_pool()

        assert sched._parallel_pool is None
        assert sched._parallel_pool_max_workers is None
        assert sched._sequential_pool is None


class TestRunningJobGuard:
    def test_running_set_prevents_double_dispatch(self, monkeypatch, isolated_scheduler):
        sched = isolated_scheduler
        job = {
            "id": "guard-job",
            "name": "guard-test",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        sched._running_job_ids.add("guard-job")
        dispatched = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        _patch_job_io(monkeypatch, sched)
        monkeypatch.setattr(
            sched,
            "run_job",
            lambda j: dispatched.append(j["id"]) or (True, "out", "resp", None),
        )

        n = sched.tick(verbose=False)

        assert n == 0
        assert dispatched == []


class TestSyncMode:
    def test_sync_true_blocks_and_returns_correct_count(self, monkeypatch, isolated_scheduler):
        sched = isolated_scheduler
        jobs = [
            {
                "id": f"job-{i}",
                "name": f"Job {i}",
                "prompt": "test",
                "schedule": "every 5m",
                "enabled": True,
                "next_run_at": "2020-01-01T00:00:00",
                "deliver": "local",
            }
            for i in range(3)
        ]

        monkeypatch.setattr(sched, "get_due_jobs", lambda: jobs)
        _patch_job_io(monkeypatch, sched)
        monkeypatch.setattr(sched, "run_job", lambda _j: (True, "out", "resp", None))

        assert sched.tick(verbose=False) == 3

    def test_sync_false_returns_immediately(self, monkeypatch, isolated_scheduler):
        sched = isolated_scheduler
        job = {
            "id": "slow-job",
            "name": "slow",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }
        barrier = threading.Barrier(2, timeout=5)

        def slow_run(_job):
            barrier.wait()
            return True, "out", "resp", None

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        _patch_job_io(monkeypatch, sched)
        monkeypatch.setattr(sched, "run_job", slow_run)

        start = time.monotonic()
        n = sched.tick(verbose=False, sync=False)
        elapsed = time.monotonic() - start

        assert n == 1
        assert elapsed < 1.0

        barrier.wait()
        time.sleep(0.1)


class TestSequentialPool:
    def test_sequential_job_does_not_block_ticker(
        self,
        tmp_path,
        monkeypatch,
        isolated_scheduler,
    ):
        sched = isolated_scheduler
        job = {
            "id": "slow-workdir",
            "name": "slow-workdir",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
            "workdir": str(tmp_path),
        }
        barrier = threading.Barrier(2, timeout=5)

        def slow_run(_job):
            barrier.wait()
            return True, "out", "resp", None

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        _patch_job_io(monkeypatch, sched)
        monkeypatch.setattr(sched, "run_job", slow_run)

        start = time.monotonic()
        n = sched.tick(verbose=False, sync=False)
        elapsed = time.monotonic() - start

        assert n == 1
        assert elapsed < 1.0

        barrier.wait()
        time.sleep(0.1)

    def test_sequential_running_guard_prevents_double_dispatch(
        self,
        tmp_path,
        monkeypatch,
        isolated_scheduler,
    ):
        sched = isolated_scheduler
        job = {
            "id": "guard-seq",
            "name": "guard-seq",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
            "workdir": str(tmp_path),
        }

        sched._running_job_ids.add("guard-seq")
        dispatched = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        _patch_job_io(monkeypatch, sched)
        monkeypatch.setattr(
            sched,
            "run_job",
            lambda j: dispatched.append(j["id"]) or (True, "out", "resp", None),
        )

        n = sched.tick(verbose=False)

        assert n == 0
        assert dispatched == []

    def test_get_sequential_pool_is_persistent(self, isolated_scheduler):
        sched = isolated_scheduler

        pool1 = sched._get_sequential_pool()
        pool2 = sched._get_sequential_pool()

        assert pool1 is pool2
