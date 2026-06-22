from __future__ import annotations

import sys
import types


def test_session_automation_counts_collects_owner_and_binding_sessions(monkeypatch):
    import hermes_cli.web_server as web_server

    fake_jobs = types.ModuleType("cron.jobs")
    fake_jobs.list_jobs = lambda include_disabled=True: [
        {
            "id": "job-owner",
            "dovie": {
                "owner": {"sourceSessionId": "session-a"},
                "result_binding": {"mode": "new-session"},
            },
        },
        {
            "id": "job-binding",
            "dovie": {
                "owner": {"sourceSessionId": "session-a"},
                "result_binding": {"mode": "current-session", "sessionId": "session-b"},
            },
        },
        {
            "id": "job-legacy",
            "dovie": {
                "session_id": "session-c",
                "session_target": "main",
            },
        },
    ]
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_jobs)

    assert web_server._session_automation_counts() == {
        "session-a": 2,
        "session-b": 1,
        "session-c": 1,
    }
