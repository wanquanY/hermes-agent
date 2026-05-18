from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def manage_cron(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "list").strip().lower()
    if action == "status":
        return cron_status()
    if action == "list":
        return list_cron_jobs(params)
    if action == "add":
        return add_cron_job(params)
    if action == "update":
        return update_cron_job(params)
    if action == "remove":
        return remove_cron_job(params)
    if action in {"run", "run_now", "trigger"}:
        return run_cron_job(params)
    if action == "runs":
        return list_cron_runs(params)
    if action in {"pause", "resume"}:
        return toggle_cron_job(params, enabled=action == "resume")
    raise ValueError(f"unknown cron action: {action}")


def cron_status() -> dict[str, Any]:
    from cron.jobs import JOBS_FILE, list_jobs

    jobs = list_jobs(include_disabled=True)
    return {
        "enabled": True,
        "storePath": str(JOBS_FILE),
        "jobs": len(jobs),
        "nextWakeAtMs": _next_wake_at_ms(jobs),
        "scheduler": {"source": "gateway-ticker", "healthy": True},
    }


def list_cron_jobs(params: dict[str, Any]) -> dict[str, Any]:
    from cron.jobs import list_jobs

    jobs = [_normalize_job(job) for job in list_jobs(include_disabled=_bool(params.get("includeDisabled"), default=True))]
    jobs = _sort_jobs(_filter_jobs(jobs, params), params)
    total = len(jobs)
    offset = max(0, _int(params.get("offset"), 0))
    limit = max(1, min(500, _int(params.get("limit"), 200)))
    page = jobs[offset:offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < total
    return {
        "jobs": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": has_more,
        "nextOffset": next_offset if has_more else None,
    }


def add_cron_job(params: dict[str, Any]) -> dict[str, Any]:
    from cron.jobs import create_job, pause_job, update_job

    payload = _payload_from_params(params)
    job = create_job(
        prompt=payload["prompt"],
        schedule=_schedule_to_hermes_string(params.get("schedule") or params.get("scheduleText")),
        name=_text(params.get("name") or "自动化任务"),
        repeat=1 if _bool(params.get("deleteAfterRun"), default=False) else params.get("repeat"),
        deliver=_optional_text(params.get("deliver")) or payload.get("deliver"),
        skills=_string_list(params.get("skills") or payload.get("skills")),
        model=_optional_text(params.get("model") or payload.get("model")),
        provider=_optional_text(params.get("provider")),
        base_url=_optional_text(params.get("baseUrl") or params.get("base_url")),
        script=_optional_text(params.get("script")),
        context_from=params.get("contextFrom") or params.get("context_from"),
        enabled_toolsets=_string_list(params.get("enabledToolsets") or params.get("enabled_toolsets") or payload.get("enabledToolsets") or payload.get("enabled_toolsets")),
        workdir=_optional_text(params.get("workdir")),
        no_agent=_bool(params.get("noAgent") or params.get("no_agent"), default=False),
    )
    updated = update_job(job["id"], {
        "description": _optional_text(params.get("description")),
        "doxie": _doxie_metadata_from_params(params),
    }) or job
    if not _bool(params.get("enabled"), default=True):
        updated = pause_job(job["id"], reason="disabled from Doxie") or updated
    return _normalize_job(updated)


def update_cron_job(params: dict[str, Any]) -> dict[str, Any]:
    from cron.jobs import get_job, pause_job, resume_job, update_job

    job_id = _job_id(params)
    current = get_job(job_id)
    if not current:
        raise ValueError(f"job not found: {job_id}")

    patch = params.get("patch") if isinstance(params.get("patch"), dict) else params
    updates: dict[str, Any] = {}
    if "name" in patch:
        updates["name"] = _text(patch.get("name"))
    if "description" in patch:
        updates["description"] = _optional_text(patch.get("description"))
    if "schedule" in patch:
        updates["schedule"] = _schedule_to_hermes_string(patch.get("schedule"))

    payload_patch = patch.get("payload") if isinstance(patch.get("payload"), dict) else patch
    if any(key in payload_patch for key in ("prompt", "message", "text", "payloadText")):
        updates["prompt"] = _payload_from_params(payload_patch)["prompt"]

    direct_fields = {
        "model": "model",
        "provider": "provider",
        "baseUrl": "base_url",
        "base_url": "base_url",
        "deliver": "deliver",
        "workdir": "workdir",
        "script": "script",
        "contextFrom": "context_from",
        "context_from": "context_from",
        "noAgent": "no_agent",
        "no_agent": "no_agent",
    }
    for source_key, target_key in direct_fields.items():
        if source_key in patch:
            updates[target_key] = patch.get(source_key)
    if "skills" in patch:
        updates["skills"] = _string_list(patch.get("skills"))
    if "enabledToolsets" in patch:
        updates["enabled_toolsets"] = _string_list(patch.get("enabledToolsets"))
    if "enabled_toolsets" in patch:
        updates["enabled_toolsets"] = _string_list(patch.get("enabled_toolsets"))
    if "enabledToolsets" in payload_patch:
        updates["enabled_toolsets"] = _string_list(payload_patch.get("enabledToolsets"))
    if "enabled_toolsets" in payload_patch:
        updates["enabled_toolsets"] = _string_list(payload_patch.get("enabled_toolsets"))

    existing_doxie = current.get("doxie") if isinstance(current.get("doxie"), dict) else {}
    doxie_patch = {k: v for k, v in _doxie_metadata_from_params(patch).items() if v is not None}
    updates["doxie"] = {**existing_doxie, **doxie_patch}

    updated = update_job(job_id, updates) if updates else current
    if "enabled" in patch:
        updated = (resume_job(job_id) if _bool(patch.get("enabled"), default=True) else pause_job(job_id, reason="disabled from Doxie")) or updated
    return _normalize_job(updated)


def remove_cron_job(params: dict[str, Any]) -> dict[str, Any]:
    from cron.jobs import remove_job

    return {"ok": True, "removed": bool(remove_job(_job_id(params)))}


def run_cron_job(params: dict[str, Any]) -> dict[str, Any]:
    from cron.jobs import trigger_job

    job = trigger_job(_job_id(params))
    if not job:
        return {"ok": False, "ran": False}
    return {"ok": True, "ran": True, "job": _normalize_job(job)}


def toggle_cron_job(params: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    from cron.jobs import pause_job, resume_job

    job = resume_job(_job_id(params)) if enabled else pause_job(_job_id(params), reason="disabled from Doxie")
    if not job:
        raise ValueError(f"job not found: {_job_id(params)}")
    return {"success": True, "job": _normalize_job(job)}


def list_cron_runs(params: dict[str, Any]) -> dict[str, Any]:
    from cron.jobs import get_job, list_jobs

    job_id = _optional_text(params.get("id") or params.get("jobId") or params.get("job_id"))
    jobs = [get_job(job_id)] if job_id else list_jobs(include_disabled=True)
    entries: list[dict[str, Any]] = []
    for job in jobs:
        if job:
            entries.extend(_run_entries_for_job(job))

    query = (_optional_text(params.get("query")) or "").lower()
    if query:
        entries = [
            entry for entry in entries
            if query in _text(entry.get("jobName")).lower()
            or query in _text(entry.get("summary")).lower()
            or query in _text(entry.get("error")).lower()
        ]

    statuses = set(_string_list(params.get("statuses")))
    status = _optional_text(params.get("status"))
    if status and status != "all":
        statuses.add(status)
    if statuses:
        entries = [entry for entry in entries if _text(entry.get("status")) in statuses]

    entries.sort(key=lambda item: _int(item.get("runAtMs") or item.get("ts"), 0), reverse=_text(params.get("sortDir") or "desc") != "asc")
    total = len(entries)
    offset = max(0, _int(params.get("offset"), 0))
    limit = max(1, min(500, _int(params.get("limit"), 20)))
    page = entries[offset:offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < total
    return {
        "entries": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": has_more,
        "nextOffset": next_offset if has_more else None,
    }


def _normalize_job(job: dict[str, Any]) -> dict[str, Any]:
    doxie = job.get("doxie") if isinstance(job.get("doxie"), dict) else {}
    job_id = _text(job.get("id") or job.get("job_id"))
    last_status = _optional_text(job.get("last_status"))
    payload_kind = "sessionMessage" if doxie.get("session_target") == "main" else "agentTask"
    return {
        "id": job_id,
        "jobId": job_id,
        "name": _text(job.get("name") or job_id or "自动化任务"),
        "description": _optional_text(job.get("description") or doxie.get("description")),
        "enabled": _bool(job.get("enabled"), default=True),
        "deleteAfterRun": _bool(doxie.get("delete_after_run"), default=False),
        "agentProfileId": _optional_text(doxie.get("agent_profile_id")),
        "agentProfileName": _optional_text(doxie.get("agent_profile_name")),
        "approvalPolicy": _normalize_approval_policy(doxie.get("approval_policy")),
        "createdAtMs": _iso_to_ms(job.get("created_at")) or 0,
        "updatedAtMs": _iso_to_ms(job.get("updated_at") or job.get("created_at")) or 0,
        "workspaceId": _optional_text(doxie.get("workspace_id")),
        "workdir": _optional_text(job.get("workdir")),
        "sessionId": _optional_text(doxie.get("session_id")),
        "sessionKey": _optional_text(doxie.get("session_id")),
        "sessionTarget": _optional_text(doxie.get("session_target")) or "isolated",
        "wakeMode": _optional_text(doxie.get("wake_mode")) or "next-heartbeat",
        "schedule": _normalize_schedule(job.get("schedule"), job.get("schedule_display")),
        "scheduleDisplay": _optional_text(job.get("schedule_display")),
        "payload": {
            "kind": payload_kind,
            "text": _text(job.get("prompt")) if payload_kind == "sessionMessage" else None,
            "prompt": _text(job.get("prompt")) if payload_kind != "sessionMessage" else None,
            "model": _optional_text(job.get("model")),
            "provider": _optional_text(job.get("provider")),
            "skills": _string_list(job.get("skills")),
            "enabledToolsets": _string_list(job.get("enabled_toolsets")),
            "deliver": _optional_text(job.get("deliver")),
        },
        "state": {
            "nextRunAtMs": _iso_to_ms(job.get("next_run_at")),
            "lastRunAtMs": _iso_to_ms(job.get("last_run_at")),
            "lastRunStatus": last_status,
            "lastStatus": last_status,
            "lastError": _optional_text(job.get("last_error")),
        },
        "raw": job,
    }


def _filter_jobs(jobs: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    query = (_optional_text(params.get("query")) or "").lower()
    enabled_filter = _optional_text(params.get("enabled")) or ""
    target_filter = _optional_text(params.get("sessionTarget") or params.get("session_target")) or ""
    workspace_filter = _optional_text(params.get("workspaceId") or params.get("agentId") or params.get("workdir")) or ""
    result = jobs
    if query:
        result = [job for job in result if query in _text(job.get("name")).lower() or query in _text(job.get("description")).lower() or query in _text(job.get("workdir")).lower() or query in _payload_text(job).lower()]
    if enabled_filter == "enabled":
        result = [job for job in result if _bool(job.get("enabled"), default=True)]
    elif enabled_filter == "disabled":
        result = [job for job in result if not _bool(job.get("enabled"), default=True)]
    if target_filter and target_filter != "all":
        result = [job for job in result if _text(job.get("sessionTarget")) == target_filter]
    if workspace_filter and workspace_filter != "all":
        result = [job for job in result if _text(job.get("workspaceId")) == workspace_filter or _text(job.get("workdir")) == workspace_filter]
    return result


def _sort_jobs(jobs: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    sort_by = _text(params.get("sortBy") or "createdAtMs")
    reverse = _text(params.get("sortDir") or "desc").lower() != "asc"

    def sort_key(job: dict[str, Any]) -> Any:
        if sort_by == "name":
            return _text(job.get("name")).lower()
        if sort_by == "nextRunAtMs":
            return _int((job.get("state") or {}).get("nextRunAtMs"), 0)
        if sort_by == "updatedAtMs":
            return _int(job.get("updatedAtMs"), 0)
        return _int(job.get("createdAtMs"), 0)

    return sorted(jobs, key=sort_key, reverse=reverse)


def _run_entries_for_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    from cron.jobs import OUTPUT_DIR

    job_id = _text(job.get("id") or job.get("job_id"))
    entries: list[dict[str, Any]] = []
    output_dir = Path(OUTPUT_DIR) / job_id
    if output_dir.exists():
        for file_path in output_dir.glob("*.md"):
            ts_ms = int(file_path.stat().st_mtime * 1000)
            preview = _read_preview(file_path)
            entries.append({
                "ts": ts_ms,
                "jobId": job_id,
                "action": "finished",
                "status": "ok",
                "summary": preview,
                "outputPath": str(file_path),
                "outputPreview": preview,
                "sessionId": _doxie_session_id(job),
                "sessionKey": _doxie_session_id(job),
                "runAtMs": ts_ms,
                "nextRunAtMs": _iso_to_ms(job.get("next_run_at")),
                "model": _optional_text(job.get("model")),
                "provider": _optional_text(job.get("provider")),
                "jobName": _text(job.get("name") or job_id),
            })
    last_run_ms = _iso_to_ms(job.get("last_run_at"))
    if last_run_ms and not any(abs(_int(entry["runAtMs"], 0) - last_run_ms) < 1000 for entry in entries):
        entries.append({
            "ts": last_run_ms,
            "jobId": job_id,
            "action": "finished",
            "status": _optional_text(job.get("last_status")) or "unknown",
            "error": _optional_text(job.get("last_error")),
            "summary": _optional_text(job.get("last_error")) or "",
            "sessionId": _doxie_session_id(job),
            "sessionKey": _doxie_session_id(job),
            "runAtMs": last_run_ms,
            "nextRunAtMs": _iso_to_ms(job.get("next_run_at")),
            "model": _optional_text(job.get("model")),
            "provider": _optional_text(job.get("provider")),
            "jobName": _text(job.get("name") or job_id),
        })
    return entries


def _payload_from_params(params: dict[str, Any]) -> dict[str, Any]:
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else params
    return {
        "prompt": _optional_text(payload.get("prompt")) or _optional_text(payload.get("message")) or _optional_text(payload.get("text")) or _optional_text(params.get("prompt")) or _optional_text(params.get("payloadText")) or "",
        "model": _optional_text(payload.get("model") or params.get("model")),
        "skills": _string_list(payload.get("skills") or params.get("skills")),
        "enabledToolsets": _string_list(payload.get("enabledToolsets") or payload.get("enabled_toolsets") or params.get("enabledToolsets") or params.get("enabled_toolsets")),
        "deliver": _optional_text(payload.get("deliver") or params.get("deliver")),
    }


def _doxie_metadata_from_params(params: dict[str, Any]) -> dict[str, Any]:
    delete_after_run = params.get("deleteAfterRun", params.get("delete_after_run"))
    approval_policy = None
    if "approvalPolicy" in params or "approval_policy" in params:
        approval_policy = _normalize_approval_policy(params.get("approvalPolicy") or params.get("approval_policy"))
    return {
        "description": _optional_text(params.get("description")),
        "agent_profile_id": _optional_text(params.get("agentProfileId") or params.get("agent_profile_id")),
        "agent_profile_name": _optional_text(params.get("agentProfileName") or params.get("agent_profile_name")),
        "approval_policy": approval_policy,
        "session_target": _optional_text(params.get("sessionTarget") or params.get("session_target")),
        "wake_mode": _optional_text(params.get("wakeMode") or params.get("wake_mode")),
        "delete_after_run": _bool(delete_after_run, default=False) if delete_after_run is not None else None,
        "session_id": _optional_text(params.get("sessionId") or params.get("sessionKey") or params.get("session_id")),
        "workspace_id": _optional_text(params.get("workspaceId") or params.get("workspace_id")),
        "created_by": "doxie-desktop",
    }


def _normalize_approval_policy(value: Any) -> str:
    text = _text(value).strip().lower()
    if text in {"approve", "allow", "allow_all", "full_access", "off"}:
        return "approve"
    if text in {"deny", "block", "manual"}:
        return "deny"
    return "default"


def _normalize_schedule(schedule: Any, display: Any = None) -> dict[str, Any]:
    if isinstance(schedule, dict):
        kind = _text(schedule.get("kind"))
        if kind == "once":
            return {"kind": "at", "at": _text(schedule.get("run_at"))}
        if kind == "interval":
            return {"kind": "every", "everyMs": max(1, _int(schedule.get("minutes"), 1)) * 60_000}
        if kind == "cron":
            return {"kind": "cron", "expr": _text(schedule.get("expr"))}
    return {"kind": "cron", "expr": _text(display or schedule)}


def _schedule_to_hermes_string(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        kind = _text(value.get("kind"))
        if kind in {"at", "once"}:
            return _text(value.get("at") or value.get("run_at"))
        if kind in {"every", "interval"}:
            every_ms = _int(value.get("everyMs"), 0)
            minutes = _int(value.get("minutes"), 0) or max(1, every_ms // 60_000)
            return f"every {minutes}m"
        if kind == "cron":
            return _text(value.get("expr"))
    raise ValueError("schedule is required")


def _next_wake_at_ms(jobs: list[dict[str, Any]]) -> int | None:
    values = [_iso_to_ms(job.get("next_run_at")) for job in jobs if _bool(job.get("enabled"), default=True)]
    values = [value for value in values if value and value > 0]
    return min(values) if values else None


def _read_preview(path: Path, limit: int = 240) -> str:
    try:
        compact = " ".join(path.read_text(encoding="utf-8", errors="replace").strip().split())
    except OSError:
        return ""
    return compact if len(compact) <= limit else f"{compact[:limit - 1]}..."


def _payload_text(job: dict[str, Any]) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    return _text(payload.get("text") or payload.get("prompt"))


def _doxie_session_id(job: dict[str, Any]) -> str | None:
    doxie = job.get("doxie") if isinstance(job.get("doxie"), dict) else {}
    return _optional_text(doxie.get("session_id"))


def _job_id(params: dict[str, Any]) -> str:
    value = _optional_text(params.get("id") or params.get("jobId") or params.get("job_id") or params.get("name"))
    if not value:
        raise ValueError("job id is required")
    return value


def _iso_to_ms(value: Any) -> int | None:
    text = _optional_text(value)
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


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
