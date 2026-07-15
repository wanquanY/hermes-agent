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


def test_status_reports_actual_dovie_ticker_state(cron_env, monkeypatch):
    from tui_gateway.services import dovie_cron_runtime
    from tui_gateway.services.dovie_cron_jobs import cron_status

    monkeypatch.setattr(
        dovie_cron_runtime,
        "cron_ticker_status",
        lambda: {"source": "dovie-sidecar-ticker", "healthy": False, "running": False},
    )

    status = cron_status()

    assert status["scheduler"] == {
        "source": "dovie-sidecar-ticker",
        "healthy": False,
        "running": False,
    }


def test_manage_cron_uses_active_profile_home_without_runtime_worker(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tui_gateway.services.dovie_cron_jobs import manage_cron

    default_home = tmp_path / "default"
    profile_home = tmp_path / "profile-a"
    default_home.mkdir()
    profile_home.mkdir()

    default_token = set_hermes_home_override(default_home)
    try:
        manage_cron({
            "action": "add",
            "name": "Default task",
            "schedule": {"kind": "every", "everyMs": 3_600_000},
            "payload": {"kind": "agentTask", "prompt": "default"},
        })
    finally:
        reset_hermes_home_override(default_token)

    profile_token = set_hermes_home_override(profile_home)
    try:
        manage_cron({
            "action": "add",
            "name": "Profile task",
            "schedule": {"kind": "every", "everyMs": 3_600_000},
            "payload": {"kind": "agentTask", "prompt": "profile"},
        })
        listed = manage_cron({"action": "list"})
    finally:
        reset_hermes_home_override(profile_token)

    assert [job["name"] for job in listed["jobs"]] == ["Profile task"]
    assert (default_home / "cron" / "jobs.json").exists()
    assert (profile_home / "cron" / "jobs.json").exists()


def test_add_job_with_wake_now_triggers_due_run_and_wakes_ticker(cron_env, monkeypatch):
    from cron.jobs import get_job
    from tui_gateway.services import dovie_cron_runtime
    from tui_gateway.services.dovie_cron_jobs import add_cron_job

    wakeups: list[bool] = []
    monkeypatch.setattr(dovie_cron_runtime, "request_cron_tick", lambda: wakeups.append(True))

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
    from tui_gateway.services import dovie_cron_runtime
    from tui_gateway.services.dovie_cron_jobs import run_cron_job

    wakeups: list[bool] = []
    monkeypatch.setattr(dovie_cron_runtime, "request_cron_tick", lambda: wakeups.append(True))
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
    from tui_gateway.services.dovie_cron_jobs import list_cron_runs

    job = create_job(prompt="check status", schedule="every 1h", name="Session run job")
    update_job(job["id"], {"dovie": {"session_id": "target-session"}})
    save_job_output(job["id"], "output")
    mark_job_run(job["id"], success=True, session_id="cron_session")

    runs = list_cron_runs({"id": job["id"]})["entries"]

    assert runs
    assert runs[0]["sessionId"] == "cron_session"
    assert runs[0]["targetSessionId"] == "target-session"


def test_add_job_persists_dovie_owner_and_result_binding(cron_env):
    from cron.jobs import get_job
    from tui_gateway.services.dovie_cron_jobs import add_cron_job

    job = add_cron_job({
        "name": "Conversation task",
        "schedule": {"kind": "every", "everyMs": 3_600_000},
        "owner": {
            "agentProfileId": "agent-research",
            "agentProfileName": "Research",
            "runtimeScopeKey": "profile:agent-research@v1",
            "workspaceId": "workspace-1",
            "workdir": "/tmp/workspace",
            "sourceSessionId": "session-1",
            "createdBy": "conversation",
        },
        "resultBinding": {"mode": "current-session", "sessionId": "session-1"},
        "payload": {"kind": "agentTask", "prompt": "check status"},
    })

    stored = get_job(job["id"])

    assert stored is not None
    assert stored["dovie"]["schema_version"] == 2
    assert stored["dovie"]["owner"]["agentProfileId"] == "agent-research"
    assert stored["dovie"]["result_binding"] == {"mode": "current-session", "sessionId": "session-1"}
    assert job["origin"] == "conversation"
    assert job["sessionTarget"] == "main"
    assert job["sessionId"] == "session-1"
    assert job["owner"]["workdir"] == "/tmp/workspace"


def test_add_job_defaults_owner_session_to_current_session_binding(cron_env):
    from cron.jobs import get_job
    from tui_gateway.services.dovie_cron_jobs import add_cron_job

    job = add_cron_job({
        "name": "Conversation default task",
        "schedule": {"kind": "every", "everyMs": 3_600_000},
        "owner": {
            "agentProfileId": "agent-research",
            "sourceSessionId": "session-1",
            "createdBy": "conversation",
        },
        "payload": {"kind": "agentTask", "prompt": "check status"},
    })

    stored = get_job(job["id"])

    assert stored is not None
    assert stored["dovie"]["result_binding"] == {"mode": "current-session", "sessionId": "session-1"}
    assert stored["dovie"]["session_target"] == "main"
    assert job["resultBinding"] == {"mode": "current-session", "sessionId": "session-1"}
    assert job["sessionTarget"] == "main"
    assert job["sessionId"] == "session-1"
    assert job["payload"]["kind"] == "agentTask"
    assert job["payload"]["prompt"] == "check status"


def test_add_job_prefers_owner_session_over_legacy_isolated_target_default(cron_env):
    from tui_gateway.services.dovie_cron_jobs import add_cron_job

    job = add_cron_job({
        "name": "Legacy modal default",
        "schedule": {"kind": "every", "everyMs": 3_600_000},
        "sessionTarget": "isolated",
        "owner": {
            "agentProfileId": "agent-research",
            "sourceSessionId": "session-1",
            "createdBy": "conversation",
        },
        "payload": {"kind": "agentTask", "prompt": "check status"},
    })

    assert job["resultBinding"] == {"mode": "current-session", "sessionId": "session-1"}
    assert job["sessionTarget"] == "main"


def test_add_job_honors_explicit_new_session_result_binding(cron_env):
    from tui_gateway.services.dovie_cron_jobs import add_cron_job

    job = add_cron_job({
        "name": "Explicit isolated task",
        "schedule": {"kind": "every", "everyMs": 3_600_000},
        "sessionTarget": "isolated",
        "owner": {
            "agentProfileId": "agent-research",
            "sourceSessionId": "session-1",
            "createdBy": "conversation",
        },
        "resultBinding": {"mode": "new-session"},
        "payload": {"kind": "agentTask", "prompt": "check status"},
    })

    assert job["resultBinding"] == {"mode": "new-session"}
    assert job["sessionTarget"] == "isolated"
    assert job["sessionId"] is None


def test_dovie_automation_tool_inherits_current_context(cron_env):
    from channels import session_context
    from tools.dovie_automation_task_tool import dovie_automation_task_create
    from tui_gateway.services.dovie_cron_jobs import list_cron_jobs

    workspace = cron_env / "workspace"
    workspace.mkdir()
    context = {
        "sourceSessionId": "session-chat",
        "sourceAgentProfileId": "agent-writer",
        "sourceAgentProfileName": "Writer",
        "sourceAgentProfileVersionId": "version-1",
        "runtimeScopeKey": "profile:agent-writer@version-1",
        "workspaceId": "workspace-2",
        "workspacePath": str(workspace),
        "sourceRunId": "run-1",
        "sourceTurnId": "turn-1",
        "sourceClientMessageId": "message-1",
    }
    tokens = session_context.set_session_vars(dovie_product_context=__import__("json").dumps(context))
    try:
        result = dovie_automation_task_create(
            name="Follow up",
            prompt="Summarize updates",
            schedule={"kind": "every", "everyMs": 900_000},
            result_binding="current-session",
        )
    finally:
        session_context.clear_session_vars(tokens)

    assert "automation_job_created" in result
    jobs = list_cron_jobs({"includeDisabled": True})["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["agentProfileId"] == "agent-writer"
    assert jobs[0]["workspaceId"] == "workspace-2"
    assert jobs[0]["workdir"] == str(workspace)
    assert jobs[0]["resultBinding"] == {"mode": "current-session", "sessionId": "session-chat"}


def test_dovie_automation_tool_defaults_to_current_session_binding(cron_env):
    import json

    from channels import session_context
    from tools.dovie_automation_task_tool import dovie_automation_task_create

    workspace = cron_env / "workspace"
    workspace.mkdir()
    context = {
        "sourceSessionId": "session-chat",
        "sourceAgentProfileId": "agent-writer",
        "sourceAgentProfileName": "Writer",
        "workspacePath": str(workspace),
    }
    tokens = session_context.set_session_vars(dovie_product_context=json.dumps(context))
    try:
        created = json.loads(dovie_automation_task_create(
            name="Default bound follow up",
            prompt="Summarize updates",
            schedule={"kind": "every", "everyMs": 900_000},
        ))["job"]
    finally:
        session_context.clear_session_vars(tokens)

    assert created["resultBinding"] == {"mode": "current-session", "sessionId": "session-chat"}
    assert created["sessionTarget"] == "main"


def test_add_job_inherits_runtime_capabilities_without_persisting_skills(cron_env, monkeypatch):
    from tui_gateway.services.dovie_cron_jobs import add_cron_job

    monkeypatch.setenv("HERMES_TUI_SKILLS", "research, writing, research")

    job = add_cron_job({
        "name": "Skill bound task",
        "schedule": {"kind": "every", "everyMs": 900_000},
        "payload": {"kind": "agentTask", "prompt": "Summarize updates"},
    })

    assert "skills" not in job["payload"]
    assert job["capabilitySource"] == "agent-runtime"
    assert job["capabilityOverride"] is None
    assert job["raw"]["skills"] == []


def test_add_job_persists_explicit_capability_override(cron_env):
    from tui_gateway.services.dovie_cron_jobs import add_cron_job

    job = add_cron_job({
        "name": "Pinned capability task",
        "schedule": {"kind": "every", "everyMs": 900_000},
        "payload": {"kind": "agentTask", "prompt": "Summarize updates"},
        "capabilityOverride": {
            "skills": ["research", "writing", "research"],
            "enabledToolsets": ["web", "skills"],
            "reason": "User explicitly limited this task",
            "createdBy": "user-explicit-advanced-setting",
        },
    })

    assert job["capabilitySource"] == "task-override"
    assert job["capabilityOverride"] == {
        "skills": ["research", "writing"],
        "enabledToolsets": ["web", "skills"],
        "createdBy": "user-explicit-advanced-setting",
        "reason": "User explicitly limited this task",
    }
    assert job["payload"]["capabilityOverride"] == job["capabilityOverride"]
    assert job["raw"]["skills"] == ["research", "writing"]
    assert job["raw"]["enabled_toolsets"] == ["web", "skills"]


def test_update_job_clears_capability_override_to_runtime_inheritance(cron_env):
    from tui_gateway.services.dovie_cron_jobs import add_cron_job, update_cron_job

    created = add_cron_job({
        "name": "Pinned capability task",
        "schedule": {"kind": "every", "everyMs": 900_000},
        "payload": {"kind": "agentTask", "prompt": "Summarize updates"},
        "capabilityOverride": {
            "skills": ["research"],
            "enabledToolsets": ["web"],
            "reason": "User explicitly limited this task",
            "createdBy": "user-explicit-advanced-setting",
        },
    })

    updated = update_cron_job({
        "id": created["id"],
        "patch": {"capabilityOverride": {}},
    })

    assert updated["capabilitySource"] == "agent-runtime"
    assert updated["capabilityOverride"] is None
    assert updated["raw"]["skills"] == []
    assert updated["raw"]["enabled_toolsets"] is None
    assert updated["raw"]["dovie"]["capability_override"] is None


def test_dovie_automation_tools_manage_current_session_bound_tasks(cron_env):
    import json

    from channels import session_context
    from tools.dovie_automation_task_tool import (
        dovie_automation_task_create,
        dovie_automation_task_list,
        dovie_automation_task_remove,
        dovie_automation_task_update,
    )
    from tui_gateway.services.dovie_cron_jobs import add_cron_job, list_cron_jobs

    workspace = cron_env / "workspace"
    workspace.mkdir()
    context = {
        "sourceSessionId": "session-chat",
        "sourceAgentProfileId": "agent-writer",
        "sourceAgentProfileName": "Writer",
        "sourceAgentProfileVersionId": "version-1",
        "runtimeScopeKey": "profile:agent-writer@version-1",
        "workspaceId": "workspace-2",
        "workspacePath": str(workspace),
        "sourceRunId": "run-1",
        "sourceTurnId": "turn-1",
        "sourceClientMessageId": "message-1",
    }
    tokens = session_context.set_session_vars(dovie_product_context=json.dumps(context))
    try:
        created = json.loads(dovie_automation_task_create(
            name="Bound follow up",
            prompt="Summarize updates",
            schedule={"kind": "every", "everyMs": 900_000},
            result_binding="current-session",
        ))["job"]
        other = add_cron_job({
            "name": "Other session task",
            "schedule": {"kind": "every", "everyMs": 3_600_000},
            "owner": {
                "agentProfileId": "agent-writer",
                "agentProfileName": "Writer",
                "workspaceId": "workspace-2",
                "workdir": str(workspace),
                "sourceSessionId": "other-session",
                "createdBy": "conversation",
            },
            "resultBinding": {"mode": "current-session", "sessionId": "other-session"},
            "payload": {"kind": "agentTask", "prompt": "check other"},
        })

        current_session_jobs = json.loads(dovie_automation_task_list())["jobs"]
        current_profile_jobs = json.loads(dovie_automation_task_list(scope="current-profile"))["jobs"]
        blocked_update = json.loads(dovie_automation_task_update(
            task_id=other["id"],
            name="Should not update",
        ))
        updated = json.loads(dovie_automation_task_update(
            task_id=created["id"],
            name="Updated follow up",
            prompt="Updated prompt",
            enabled=False,
        ))["job"]
        removed = json.loads(dovie_automation_task_remove(task_id=created["id"]))
    finally:
        session_context.clear_session_vars(tokens)

    assert [job["id"] for job in current_session_jobs] == [created["id"]]
    assert {job["id"] for job in current_profile_jobs} == {created["id"], other["id"]}
    assert "not bound to the current conversation" in blocked_update["error"]
    assert updated["name"] == "Updated follow up"
    assert updated["enabled"] is False
    assert removed["removed"] is True
    remaining = list_cron_jobs({"includeDisabled": True})["jobs"]
    assert [job["id"] for job in remaining] == [other["id"]]
