from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

import cron.jobs as jobs
import cron.scheduler as scheduler


@pytest.fixture
def claim_store(tmp_path, monkeypatch):
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    return cron_dir


def _job(*, claim=None):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    record = {
        "id": "job-1",
        "name": "one shot",
        "prompt": "run once",
        "schedule": {"kind": "once", "run_at": past},
        "schedule_display": past,
        "repeat": {"times": 1, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "next_run_at": past,
    }
    if claim is not None:
        record["run_claim"] = claim
    return record


def test_claim_heartbeat_and_completion_are_owner_bound(claim_store):
    jobs.save_jobs([_job()])

    assert jobs.claim_job_for_fire("job-1", owner="owner-a") is True
    assert jobs.claim_job_for_fire("job-1", owner="owner-b") is False
    assert jobs.heartbeat_run_claim("job-1", expected_owner="owner-b") is False
    assert jobs.heartbeat_run_claim("job-1", expected_owner="owner-a") is True
    assert jobs.mark_job_run("job-1", True, expected_owner="owner-b") is False
    assert jobs.get_job("job-1")["run_claim"]["by"] == "owner-a"
    assert jobs.mark_job_run("job-1", True, expected_owner="owner-a") is True
    assert jobs.get_job("job-1") is None


def test_drain_timeout_preserves_execution_owner_until_thread_finishes(claim_store):
    jobs.save_jobs([_job()])
    assert jobs.claim_job_for_fire("job-1", owner="owner-a") is True

    assert jobs.record_job_drain_timeout("job-1", expected_owner="owner-b") is False
    assert jobs.record_job_drain_timeout("job-1", expected_owner="owner-a") is True

    stored = jobs.get_job("job-1")
    assert stored["run_claim"]["by"] == "owner-a"
    assert stored["state"] == "running"
    assert stored["last_status"] == "error"
    assert stored["last_error"] == "runtime drain timeout"
    assert stored["repeat"]["completed"] == 0

    assert jobs.mark_job_run("job-1", True, expected_owner="owner-a") is True
    assert jobs.get_job("job-1") is None


def test_expired_dead_owner_can_be_replaced_but_stale_runner_cannot_finish(
    claim_store,
    monkeypatch,
):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    jobs.save_jobs([
        _job(claim={"by": "owner-a", "at": old, "ttl_seconds": 1}),
    ])
    monkeypatch.setattr(jobs, "_job_running_in_this_process", lambda _job_id: False)

    assert jobs.claim_job_for_fire("job-1", owner="owner-b", claim_ttl_seconds=10) is True
    assert jobs.heartbeat_run_claim("job-1", expected_owner="owner-a") is False
    assert jobs.mark_job_run("job-1", False, "late", expected_owner="owner-a") is False
    assert jobs.get_job("job-1")["run_claim"]["by"] == "owner-b"


def test_running_set_failure_keeps_expired_one_shot(claim_store, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    jobs.save_jobs([
        _job(claim={"by": "owner-a", "at": old, "ttl_seconds": 1}),
    ])

    def broken_running_set():
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(scheduler, "get_running_job_ids", broken_running_set)

    assert jobs.get_due_jobs() == []
    assert jobs.get_job("job-1")["run_claim"]["by"] == "owner-a"


def test_two_processes_competing_for_same_job_have_one_winner(tmp_path):
    home = tmp_path / "hermes-home"
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True)
    payload = {"jobs": [_job()], "updated_at": datetime.now(timezone.utc).isoformat()}
    (cron_dir / "jobs.json").write_text(json.dumps(payload), encoding="utf-8")
    command = (
        "from cron.jobs import claim_job_for_fire; "
        "import sys; "
        "print(claim_job_for_fire('job-1', owner=sys.argv[1]))"
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", command, owner],
            cwd=str(os.getcwd()),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for owner in ("owner-a", "owner-b")
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        results.append(stdout.strip())

    assert sorted(results) == ["False", "True"]


def test_long_script_refreshes_stable_owner(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(
        scheduler,
        "heartbeat_run_claim",
        lambda job_id, *, expected_owner: calls.append((job_id, expected_owner)) or True,
    )

    def slow_script(_path):
        time.sleep(0.04)
        return True, "ok"

    monkeypatch.setattr(scheduler, "_run_job_script", slow_script)

    result = scheduler._run_job_script_with_claim_heartbeat(
        {"id": "job-1", "_run_claim_owner": "owner-a"},
        "/tmp/fake.sh",
    )

    assert result == (True, "ok")
    assert calls
    assert set(calls) == {("job-1", "owner-a")}
