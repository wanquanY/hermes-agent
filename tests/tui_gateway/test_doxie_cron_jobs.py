from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture()
def cron_env(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    cron_dir = tmp_path / "cron"
    output_dir = cron_dir / "output"
    cron_dir.mkdir(parents=True)
    output_dir.mkdir()
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", output_dir)
    return cron_dir


def test_status_reports_actual_doxie_ticker_state(cron_env, monkeypatch):
    from tui_gateway.services import doxie_cron_runtime
    from tui_gateway.services.doxie_cron_jobs import cron_status

    monkeypatch.setattr(
        doxie_cron_runtime,
        "cron_ticker_status",
        lambda: {"source": "doxie-sidecar-ticker", "healthy": False, "running": False},
    )

    status = cron_status()

    assert status["scheduler"] == {
        "source": "doxie-sidecar-ticker",
        "healthy": False,
        "running": False,
    }


def test_add_job_with_wake_now_triggers_due_run_and_wakes_ticker(cron_env, monkeypatch):
    from cron.jobs import get_job
    from tui_gateway.services import doxie_cron_runtime
    from tui_gateway.services.doxie_cron_jobs import add_cron_job

    wakeups: list[bool] = []
    monkeypatch.setattr(doxie_cron_runtime, "request_cron_tick", lambda: wakeups.append(True))

    job = add_cron_job({
        "name": "Immediate job",
        "enabled": True,
        "wakeMode": "now",
        "schedule": {"kind": "every", "everyMs": 60 * 60 * 1000},
        "payload": {"kind": "agentTask", "prompt": "check status"},
    })

    stored = get_job(job["id"])
    assert stored is not None
    assert stored["enabled"] is True
    assert datetime.fromisoformat(stored["next_run_at"]) <= datetime.now().astimezone()
    assert wakeups == [True]


def test_run_job_triggers_due_run_and_wakes_ticker(cron_env, monkeypatch):
    from cron.jobs import create_job, get_job
    from tui_gateway.services import doxie_cron_runtime
    from tui_gateway.services.doxie_cron_jobs import run_cron_job

    wakeups: list[bool] = []
    monkeypatch.setattr(doxie_cron_runtime, "request_cron_tick", lambda: wakeups.append(True))
    created = create_job(prompt="check status", schedule="every 1h", name="Manual run job")

    result = run_cron_job({"id": created["id"]})
    stored = get_job(created["id"])

    assert result["ok"] is True
    assert result["ran"] is True
    assert stored is not None
    assert datetime.fromisoformat(stored["next_run_at"]) <= datetime.now().astimezone()
    assert wakeups == [True]


def test_run_entries_prefer_runtime_session_over_target_session(cron_env):
    from cron.jobs import create_job, mark_job_run, save_job_output, update_job
    from tui_gateway.services.doxie_cron_jobs import list_cron_runs

    job = create_job(prompt="check status", schedule="every 1h", name="Session run job")
    update_job(job["id"], {"doxie": {"session_id": "target-session"}})
    save_job_output(job["id"], "output")
    mark_job_run(job["id"], success=True, session_id="cron_session")

    runs = list_cron_runs({"id": job["id"]})["entries"]

    assert runs
    assert runs[0]["sessionId"] == "cron_session"
    assert runs[0]["targetSessionId"] == "target-session"
