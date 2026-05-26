from __future__ import annotations

import json
from typing import Any

from tools.registry import registry, tool_error, tool_result


DOXIE_AUTOMATION_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "doxie_automation_task_create",
        "description": (
            "Create a Doxie automation task bound to the current conversation's "
            "agent profile, runtime scope, and workspace. Use this instead of "
            "asking the user to choose an agent or workdir inside a Doxie chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short task name shown in the automation list."},
                "prompt": {"type": "string", "description": "Self-contained task instruction to run on schedule."},
                "schedule": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["at", "every", "cron"]},
                                "at": {"type": "string"},
                                "everyMs": {"type": "number"},
                                "expr": {"type": "string"},
                                "tz": {"type": "string"},
                            },
                            "required": ["kind"],
                            "additionalProperties": True,
                        },
                    ],
                    "description": "When to run. Accepts Doxie schedule objects or Hermes cron strings.",
                },
                "description": {"type": "string"},
                "result_binding": {
                    "type": "string",
                    "enum": ["current-session", "new-session", "run-log-only"],
                    "description": "Where future run results should appear. Default is current-session.",
                },
                "enabled": {"type": "boolean"},
                "run_immediately": {"type": "boolean"},
                "approval_policy": {"type": "string", "enum": ["default", "deny", "approve"]},
                "model": {"type": "string"},
            },
            "required": ["name", "prompt", "schedule"],
            "additionalProperties": False,
        },
    },
}

DOXIE_AUTOMATION_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "doxie_automation_task_list",
        "description": (
            "List Doxie automation tasks owned by the current conversation context. "
            "Defaults to tasks bound to the current conversation session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["current-session", "current-profile"],
                    "description": "Default current-session. Use current-profile only when the user asks for all tasks for this agent profile.",
                },
                "include_disabled": {"type": "boolean", "description": "Whether paused/disabled tasks should be included. Default true."},
            },
            "additionalProperties": False,
        },
    },
}

DOXIE_AUTOMATION_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "doxie_automation_task_update",
        "description": (
            "Edit a Doxie automation task owned by the current conversation. "
            "By default this only edits tasks bound to the current conversation session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID from doxie_automation_task_list."},
                "scope": {"type": "string", "enum": ["current-session", "current-profile"]},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "schedule": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "object", "additionalProperties": True},
                    ],
                },
                "enabled": {"type": "boolean"},
                "result_binding": {"type": "string", "enum": ["current-session", "new-session", "run-log-only"]},
                "approval_policy": {"type": "string", "enum": ["default", "deny", "approve"]},
                "model": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
}

DOXIE_AUTOMATION_REMOVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "doxie_automation_task_remove",
        "description": (
            "Remove a Doxie automation task owned by the current conversation. "
            "By default this only removes tasks bound to the current conversation session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID from doxie_automation_task_list."},
                "scope": {"type": "string", "enum": ["current-session", "current-profile"]},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
}


def _session_design_context() -> dict[str, Any]:
    try:
        from gateway.session_context import get_session_env

        raw = get_session_env("HERMES_DOXIE_PRODUCT_CONTEXT", "")
    except Exception:
        raw = ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _has_doxie_product_context() -> bool:
    return bool(_session_design_context())


def _doxie_automation_tool_available() -> bool:
    # Tool schema resolution happens before per-turn Doxie product context is
    # installed. Do not gate visibility on HERMES_DOXIE_PRODUCT_CONTEXT here;
    # handlers validate the active conversation context at execution time.
    return True


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_text(value: Any) -> str | None:
    text = _text(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _owner_from_context(context: dict[str, Any]) -> dict[str, Any]:
    agent_profile_id = _optional_text(context.get("sourceAgentProfileId") or context.get("targetAgentProfileId"))
    source_session_id = _optional_text(context.get("sourceSessionId"))
    workspace_path = _optional_text(context.get("workspacePath"))
    owner = {
        "agentProfileId": agent_profile_id,
        "agentProfileName": _optional_text(context.get("sourceAgentProfileName")),
        "agentProfileVersionId": _optional_text(context.get("sourceAgentProfileVersionId")),
        "runtimeScopeKey": _optional_text(context.get("runtimeScopeKey")),
        "workspaceId": _optional_text(context.get("workspaceId")),
        "workdir": workspace_path,
        "sourceSessionId": source_session_id,
        "sourceRunId": _optional_text(context.get("sourceRunId")),
        "sourceTurnId": _optional_text(context.get("sourceTurnId")),
        "sourceClientMessageId": _optional_text(context.get("sourceClientMessageId")),
        "createdBy": "conversation",
    }
    return {key: value for key, value in owner.items() if value is not None}


def _current_agent_profile_id(context: dict[str, Any]) -> str | None:
    return _optional_text(context.get("sourceAgentProfileId") or context.get("targetAgentProfileId"))


def _current_session_id(context: dict[str, Any]) -> str | None:
    return _optional_text(context.get("sourceSessionId"))


def _job_owner(job: dict[str, Any]) -> dict[str, Any]:
    owner = job.get("owner") if isinstance(job.get("owner"), dict) else {}
    raw = job.get("raw") if isinstance(job.get("raw"), dict) else {}
    doxie = raw.get("doxie") if isinstance(raw.get("doxie"), dict) else {}
    raw_owner = doxie.get("owner") if isinstance(doxie.get("owner"), dict) else {}
    return owner or raw_owner


def _job_result_binding(job: dict[str, Any]) -> dict[str, Any]:
    binding = job.get("resultBinding") if isinstance(job.get("resultBinding"), dict) else {}
    raw = job.get("raw") if isinstance(job.get("raw"), dict) else {}
    doxie = raw.get("doxie") if isinstance(raw.get("doxie"), dict) else {}
    raw_binding = doxie.get("result_binding") if isinstance(doxie.get("result_binding"), dict) else {}
    return binding or raw_binding


def _job_agent_profile_id(job: dict[str, Any]) -> str | None:
    owner = _job_owner(job)
    return _optional_text(owner.get("agentProfileId") or owner.get("agent_profile_id") or job.get("agentProfileId"))


def _job_source_session_id(job: dict[str, Any]) -> str | None:
    owner = _job_owner(job)
    return _optional_text(owner.get("sourceSessionId") or owner.get("source_session_id"))


def _job_bound_session_id(job: dict[str, Any]) -> str | None:
    binding = _job_result_binding(job)
    if binding.get("mode") == "current-session":
        return _optional_text(binding.get("sessionId") or binding.get("session_id"))
    return None


def _is_current_profile_job(job: dict[str, Any], context: dict[str, Any]) -> bool:
    current_profile_id = _current_agent_profile_id(context)
    job_profile_id = _job_agent_profile_id(job)
    return bool(current_profile_id and job_profile_id and job_profile_id == current_profile_id)


def _is_current_session_job(job: dict[str, Any], context: dict[str, Any]) -> bool:
    current_session_id = _current_session_id(context)
    if not current_session_id:
        return False
    return _job_source_session_id(job) == current_session_id or _job_bound_session_id(job) == current_session_id


def _scope_value(scope: str | None) -> str:
    return scope if scope in {"current-session", "current-profile"} else "current-session"


def _list_visible_jobs(context: dict[str, Any], *, scope: str = "current-session", include_disabled: bool = True) -> list[dict[str, Any]]:
    from tui_gateway.services.doxie_cron_jobs import list_cron_jobs

    jobs = list_cron_jobs({"includeDisabled": include_disabled})["jobs"]
    profile_jobs = [job for job in jobs if _is_current_profile_job(job, context)]
    if _scope_value(scope) == "current-profile":
        return profile_jobs
    return [job for job in profile_jobs if _is_current_session_job(job, context)]


def _job_matches_ref(job: dict[str, Any], task_id: str) -> bool:
    ref = _text(task_id).strip()
    if not ref:
        return False
    return ref in {
        _text(job.get("id")).strip(),
        _text(job.get("jobId")).strip(),
        _text(job.get("name")).strip(),
    }


def _resolve_visible_job(context: dict[str, Any], task_id: str, *, scope: str) -> dict[str, Any] | str:
    visible = _list_visible_jobs(context, scope=scope, include_disabled=True)
    matches = [job for job in visible if _job_matches_ref(job, task_id)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return f"Multiple tasks matched '{task_id}'. Use the exact task ID."
    profile_matches = [
        job for job in _list_visible_jobs(context, scope="current-profile", include_disabled=True)
        if _job_matches_ref(job, task_id)
    ]
    if profile_matches and _scope_value(scope) == "current-session":
        return f"Task '{task_id}' belongs to this agent profile but is not bound to the current conversation. Use scope='current-profile' only if the user explicitly asked to manage profile-level tasks."
    return f"Task '{task_id}' was not found in the current {_scope_value(scope)} automation scope."


def _result_binding_payload(mode: str | None, context: dict[str, Any]) -> dict[str, Any] | None:
    if not mode:
        return None
    normalized = mode if mode in {"current-session", "new-session", "run-log-only"} else "current-session"
    if normalized == "current-session":
        session_id = _current_session_id(context)
        if not session_id:
            raise ValueError("current-session result binding requires the active Doxie conversation session.")
        return {"mode": "current-session", "sessionId": session_id}
    return {"mode": normalized}


def _summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    state = job.get("state") if isinstance(job.get("state"), dict) else {}
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "description": job.get("description"),
        "enabled": job.get("enabled"),
        "sessionTarget": job.get("sessionTarget"),
        "owner": _job_owner(job),
        "resultBinding": _job_result_binding(job),
        "schedule": job.get("schedule"),
        "scheduleDisplay": job.get("scheduleDisplay"),
        "nextRunAtMs": state.get("nextRunAtMs"),
        "lastRunAtMs": state.get("lastRunAtMs"),
        "lastRunStatus": state.get("lastRunStatus") or state.get("lastStatus"),
        "prompt": payload.get("prompt") or payload.get("text"),
        "model": payload.get("model"),
        "capabilitySource": job.get("capabilitySource"),
        "capabilityOverride": job.get("capabilityOverride"),
    }


def doxie_automation_task_create(
    *,
    name: str,
    prompt: str,
    schedule: Any,
    description: str = "",
    result_binding: str = "current-session",
    enabled: bool = True,
    run_immediately: bool = False,
    approval_policy: str = "default",
    model: str = "",
) -> str:
    context = _session_design_context()
    owner = _owner_from_context(context)
    if not owner.get("agentProfileId"):
        return tool_error("Doxie automation tasks require an active Doxie agent profile context.")

    binding_mode = result_binding if result_binding in {"current-session", "new-session", "run-log-only"} else "current-session"
    if binding_mode == "current-session":
        session_id = _optional_text(owner.get("sourceSessionId"))
        if not session_id:
            return tool_error("current-session result binding requires the active Doxie conversation session.")
        result_binding_payload: dict[str, Any] = {"mode": "current-session", "sessionId": session_id}
    else:
        result_binding_payload = {"mode": binding_mode}

    from tui_gateway.services.doxie_cron_jobs import add_cron_job

    job = add_cron_job({
        "name": name,
        "description": description,
        "enabled": enabled,
        "wakeMode": "now" if run_immediately else "next-heartbeat",
        "schedule": schedule,
        "approvalPolicy": approval_policy,
        "owner": owner,
        "resultBinding": result_binding_payload,
        "workdir": owner.get("workdir"),
        "payload": {
            "kind": "agentTask",
            "prompt": prompt,
            **({"model": model} if _optional_text(model) else {}),
        },
    })
    return tool_result({
        "doxie_event": "automation_job_created",
        "job": job,
    })


def doxie_automation_task_list(
    *,
    scope: str = "current-session",
    include_disabled: bool = True,
) -> str:
    context = _session_design_context()
    if not _current_agent_profile_id(context):
        return tool_error("Doxie automation task list requires an active Doxie agent profile context.")
    jobs = _list_visible_jobs(context, scope=scope, include_disabled=include_disabled)
    return tool_result({
        "doxie_event": "automation_job_listed",
        "scope": _scope_value(scope),
        "count": len(jobs),
        "jobs": [_summarize_job(job) for job in jobs],
    })


def doxie_automation_task_update(
    *,
    task_id: str,
    scope: str = "current-session",
    name: str | None = None,
    description: str | None = None,
    prompt: str | None = None,
    schedule: Any = None,
    enabled: bool | None = None,
    result_binding: str | None = None,
    approval_policy: str | None = None,
    model: str | None = None,
) -> str:
    context = _session_design_context()
    if not _current_agent_profile_id(context):
        return tool_error("Doxie automation task update requires an active Doxie agent profile context.")
    resolved = _resolve_visible_job(context, task_id, scope=scope)
    if isinstance(resolved, str):
        return tool_error(resolved)
    patch: dict[str, Any] = {}
    if name is not None:
        patch["name"] = name
    if description is not None:
        patch["description"] = description
    if schedule is not None:
        patch["schedule"] = schedule
    if enabled is not None:
        patch["enabled"] = enabled
    if approval_policy is not None:
        patch["approvalPolicy"] = approval_policy
    payload_patch: dict[str, Any] = {"kind": "agentTask"}
    if prompt is not None:
        payload_patch["prompt"] = prompt
    if model is not None:
        patch["model"] = model
    if len(payload_patch) > 1:
        patch["payload"] = payload_patch
    try:
        binding = _result_binding_payload(result_binding, context)
    except ValueError as exc:
        return tool_error(str(exc))
    if binding is not None:
        patch["resultBinding"] = binding
    if not patch:
        return tool_error("No task updates were provided.")

    from tui_gateway.services.doxie_cron_jobs import update_cron_job

    job = update_cron_job({"id": resolved["id"], "patch": patch})
    return tool_result({
        "doxie_event": "automation_job_updated",
        "job": job,
    })


def doxie_automation_task_remove(
    *,
    task_id: str,
    scope: str = "current-session",
) -> str:
    context = _session_design_context()
    if not _current_agent_profile_id(context):
        return tool_error("Doxie automation task remove requires an active Doxie agent profile context.")
    resolved = _resolve_visible_job(context, task_id, scope=scope)
    if isinstance(resolved, str):
        return tool_error(resolved)

    from tui_gateway.services.doxie_cron_jobs import remove_cron_job

    result = remove_cron_job({"id": resolved["id"]})
    return tool_result({
        "doxie_event": "automation_job_removed",
        "job": _summarize_job(resolved),
        "removed": bool(result.get("removed")),
    })


registry.register(
    name="doxie_automation_task_create",
    toolset="cronjob",
    schema=DOXIE_AUTOMATION_CREATE_SCHEMA,
    handler=lambda args, **kw: doxie_automation_task_create(**args),
    check_fn=_doxie_automation_tool_available,
    emoji="clock",
    max_result_size_chars=32_000,
)

registry.register(
    name="doxie_automation_task_list",
    toolset="cronjob",
    schema=DOXIE_AUTOMATION_LIST_SCHEMA,
    handler=lambda args, **kw: doxie_automation_task_list(**args),
    check_fn=_doxie_automation_tool_available,
    emoji="clock",
    max_result_size_chars=32_000,
)

registry.register(
    name="doxie_automation_task_update",
    toolset="cronjob",
    schema=DOXIE_AUTOMATION_UPDATE_SCHEMA,
    handler=lambda args, **kw: doxie_automation_task_update(**args),
    check_fn=_doxie_automation_tool_available,
    emoji="clock",
    max_result_size_chars=32_000,
)

registry.register(
    name="doxie_automation_task_remove",
    toolset="cronjob",
    schema=DOXIE_AUTOMATION_REMOVE_SCHEMA,
    handler=lambda args, **kw: doxie_automation_task_remove(**args),
    check_fn=_doxie_automation_tool_available,
    emoji="clock",
    max_result_size_chars=32_000,
)
