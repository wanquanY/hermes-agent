"""Profile isolation contract for cron persistence."""

from __future__ import annotations

import threading

from cron import jobs as cron_jobs
from hermes_gateway.profile_runtime import profile_runtime_scope


def _record(job_id: str) -> dict:
    return {"id": job_id, "name": job_id, "prompt": "test"}


def test_context_local_home_routes_jobs_without_global_mutation(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"

    with profile_runtime_scope(first):
        cron_jobs.save_jobs([_record("first-job")])
    with profile_runtime_scope(second):
        cron_jobs.save_jobs([_record("second-job")])

    assert "first-job" in (first / "cron" / "jobs.json").read_text()
    assert "second-job" not in (first / "cron" / "jobs.json").read_text()
    assert "second-job" in (second / "cron" / "jobs.json").read_text()


def test_explicit_store_override_is_nested_and_restored(tmp_path):
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"

    with cron_jobs.use_cron_store(outer):
        cron_jobs.save_jobs([_record("outer-before")])
        with cron_jobs.use_cron_store(inner):
            cron_jobs.save_jobs([_record("inner")])
        cron_jobs.save_jobs([_record("outer-after")])

    assert "outer-after" in (outer / "cron" / "jobs.json").read_text()
    assert "inner" not in (outer / "cron" / "jobs.json").read_text()
    assert "inner" in (inner / "cron" / "jobs.json").read_text()


def test_concurrent_profile_writes_do_not_cross_stores(tmp_path):
    homes = [tmp_path / "alpha", tmp_path / "beta"]
    barrier = threading.Barrier(2)

    def _write(index: int) -> None:
        with profile_runtime_scope(homes[index]):
            barrier.wait()
            cron_jobs.save_jobs([_record(f"job-{index}")])

    threads = [threading.Thread(target=_write, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert "job-0" in (homes[0] / "cron" / "jobs.json").read_text()
    assert "job-1" not in (homes[0] / "cron" / "jobs.json").read_text()
    assert "job-1" in (homes[1] / "cron" / "jobs.json").read_text()


def test_patched_compatibility_paths_still_take_precedence(tmp_path, monkeypatch):
    patched = tmp_path / "patched" / "cron"
    monkeypatch.setattr(cron_jobs, "CRON_DIR", patched)
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", patched / "jobs.json")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", patched / "output")

    with profile_runtime_scope(tmp_path / "context-home"):
        cron_jobs.save_jobs([_record("compat")])

    assert "compat" in (patched / "jobs.json").read_text()
    assert not (tmp_path / "context-home" / "cron" / "jobs.json").exists()
