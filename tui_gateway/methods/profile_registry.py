# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import uuid
import sqlite3

from agent.dovie_diagnostics import emit_dovie_diagnostic
from hermes_team_mission_profile_tools import team_mission_control_db as _profile_registry_control_db
from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _get_db():
    return _profile_registry_control_db()


def _text(value) -> str:
    return str(value or "").strip()


def _object(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _array(value) -> list:
    return list(value) if isinstance(value, list) else []


def _string_array(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def _bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _raw(params: dict, key: str) -> dict:
    return params.get(key) if isinstance(params.get(key), dict) else params


def _projection(params: dict | None, *, default: str = "summary") -> str:
    value = _text((params or {}).get("projection") or (params or {}).get("view") or default).lower()
    return value if value in {"summary", "detail", "raw"} else default


def _profile_summary(profile: dict) -> dict:
    if not isinstance(profile, dict):
        return {}
    keys = (
        "id",
        "slug",
        "name",
        "avatar",
        "description",
        "category",
        "tags",
        "status",
        "is_system_default",
        "isSystemDefault",
        "hermes_profile_name",
        "hermesProfileName",
        "hermes_home_path",
        "hermesHomePath",
        "default_model",
        "defaultModel",
        "default_provider",
        "defaultProvider",
        "default_permission_mode",
        "defaultPermissionMode",
        "default_toolsets",
        "defaultToolsets",
        "recommended_skills",
        "recommendedSkills",
        "current_version_id",
        "currentVersionId",
        "current_version_number",
        "currentVersionNumber",
        "agent_profile_version_id",
        "agentProfileVersionId",
        "runtime_home_path",
        "runtimeHomePath",
        "runtime_scope_key",
        "runtimeScopeKey",
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
        "last_used_at",
        "lastUsedAt",
    )
    return {
        **{key: profile.get(key) for key in keys if key in profile},
        "projection": "summary",
    }


def _profile_for_projection(profile: dict, projection: str) -> dict:
    if projection in {"detail", "raw"}:
        return profile if isinstance(profile, dict) else {}
    return _profile_summary(profile)


def _profile_payload(params: dict) -> dict:
    raw = _raw(params, "profile")
    return {
        "profile_id": _text(
            raw.get("profile_id")
            or raw.get("profileId")
            or raw.get("agent_profile_id")
            or raw.get("agentProfileId")
            or raw.get("id")
        ) or uuid.uuid4().hex,
        "slug": _text(raw.get("slug") or raw.get("hermesProfileName") or raw.get("hermes_profile_name")),
        "name": _text(raw.get("name")),
        "avatar": _text(raw.get("avatar")),
        "description": _text(raw.get("description")),
        "category": _text(raw.get("category")),
        "tags": _string_array(raw.get("tags")),
        "status": _text(raw.get("status")) or "active",
        "is_system_default": _bool(raw.get("is_system_default", raw.get("isSystemDefault"))),
        "hermes_profile_name": _text(raw.get("hermes_profile_name") or raw.get("hermesProfileName")),
        "hermes_home_path": _text(raw.get("hermes_home_path") or raw.get("hermesHomePath")),
        "default_model": _text(raw.get("default_model") or raw.get("defaultModel")),
        "default_provider": _text(raw.get("default_provider") or raw.get("defaultProvider")),
        "default_permission_mode": _text(raw.get("default_permission_mode") or raw.get("defaultPermissionMode")) or "default",
        "default_toolsets": _string_array(raw.get("default_toolsets") or raw.get("defaultToolsets")),
        "recommended_skills": _string_array(raw.get("recommended_skills") or raw.get("recommendedSkills") or raw.get("skills")),
        "platform_base_toolsets_initialized": _bool(
            raw.get("platform_base_toolsets_initialized", raw.get("platformBaseToolsetsInitialized")),
        ),
        "current_version_id": _text(raw.get("current_version_id") or raw.get("currentVersionId")),
        "current_version_number": _int(raw.get("current_version_number") or raw.get("currentVersionNumber"), 0),
        "source_kind": _text(raw.get("source_kind") or raw.get("sourceKind")),
        "public_profile_id": _text(raw.get("public_profile_id") or raw.get("publicProfileId")),
        "public_version_id": _text(raw.get("public_version_id") or raw.get("publicVersionId")),
        "public_content_hash": _text(raw.get("public_content_hash") or raw.get("publicContentHash")),
        "metadata": _object(raw.get("metadata")),
        "created_at": raw.get("created_at") or raw.get("createdAt"),
        "updated_at": raw.get("updated_at") or raw.get("updatedAt"),
        "last_used_at": raw.get("last_used_at") or raw.get("lastUsedAt"),
    }


def _profile_id_from_params(params: dict | None) -> str:
    params = params or {}
    return _text(
        params.get("profile_id")
        or params.get("profileId")
        or params.get("agent_profile_id")
        or params.get("agentProfileId")
    )


def _version_id_from_params(params: dict | None) -> str:
    params = params or {}
    return _text(
        params.get("version_id")
        or params.get("versionId")
        or params.get("agent_profile_version_id")
        or params.get("agentProfileVersionId")
    )


def _growth_diagnostic(stage: str, **fields) -> None:
    emit_dovie_diagnostic("[profile-growth-summary]", {"stage": stage, **fields})


def _draft_payload(params: dict) -> dict:
    raw = _raw(params, "draft")
    return {
        "draft_id": _text(raw.get("draft_id") or raw.get("draftId") or raw.get("id")) or uuid.uuid4().hex,
        "status": _text(raw.get("status")) or "draft",
        "draft_kind": _text(raw.get("draft_kind") or raw.get("draftKind")) or "create",
        "base_agent_profile_id": _text(raw.get("base_agent_profile_id") or raw.get("baseAgentProfileId")),
        "base_version_id": _text(raw.get("base_version_id") or raw.get("baseVersionId")),
        "target_agent_profile_id": _text(raw.get("target_agent_profile_id") or raw.get("targetAgentProfileId")),
        "source_session_id": _text(raw.get("source_session_id") or raw.get("sourceSessionId")),
        "source_agent_profile_id": _text(raw.get("source_agent_profile_id") or raw.get("sourceAgentProfileId")),
        "source_run_id": _text(raw.get("source_run_id") or raw.get("sourceRunId")),
        "source_turn_id": _text(raw.get("source_turn_id") or raw.get("sourceTurnId")),
        "source_client_message_id": _text(raw.get("source_client_message_id") or raw.get("sourceClientMessageId")),
        "workspace_id": _text(raw.get("workspace_id") or raw.get("workspaceId")),
        "name": _text(raw.get("name")),
        "avatar": _text(raw.get("avatar")),
        "description": _text(raw.get("description")),
        "category": _text(raw.get("category")),
        "tags": _string_array(raw.get("tags")),
        "architecture_template_id": _text(raw.get("architecture_template_id") or raw.get("architectureTemplateId")),
        "recommended_toolsets": _string_array(raw.get("recommended_toolsets") or raw.get("recommendedToolsets")),
        "recommended_skills": _string_array(raw.get("recommended_skills") or raw.get("recommendedSkills")),
        "skill_creation_plans": [_object(item) for item in _array(raw.get("skill_creation_plans") or raw.get("skillCreationPlans"))],
        "missing_capabilities": _string_array(raw.get("missing_capabilities") or raw.get("missingCapabilities")),
        "default_model": _text(raw.get("default_model") or raw.get("defaultModel")),
        "default_provider": _text(raw.get("default_provider") or raw.get("defaultProvider")),
        "default_permission_mode": _text(raw.get("default_permission_mode") or raw.get("defaultPermissionMode")) or "default",
        "files": _object(raw.get("files")),
        "runtime_prepared_at": raw.get("runtime_prepared_at") or raw.get("runtimePreparedAt"),
        "published_agent_profile_id": _text(raw.get("published_agent_profile_id") or raw.get("publishedAgentProfileId")),
        "published_version_id": _text(raw.get("published_version_id") or raw.get("publishedVersionId")),
        "metadata": _object(raw.get("metadata")),
        "created_at": raw.get("created_at") or raw.get("createdAt"),
        "updated_at": raw.get("updated_at") or raw.get("updatedAt"),
        "published_at": raw.get("published_at") or raw.get("publishedAt"),
    }


@method("profile.upsert")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes profile registry db unavailable")
    try:
        profile = db.upsert_agent_profile(**_profile_payload(params or {}))
        return _ok(rid, {"profile": profile})
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except sqlite3.Error as exc:
        return _err(rid, 5009, f"profile registry sqlite error: {exc}")


@method("profile.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes profile registry db unavailable")
    profile_id = _text(
        (params or {}).get("profile_id")
        or (params or {}).get("profileId")
        or (params or {}).get("agent_profile_id")
        or (params or {}).get("agentProfileId")
        or (params or {}).get("id")
    )
    slug = _text((params or {}).get("slug") or (params or {}).get("agentProfileSlug") or (params or {}).get("agent_profile_slug"))
    profile = db.get_agent_profile(profile_id) if profile_id else {}
    if not profile and slug and callable(getattr(db, "get_agent_profile_by_slug", None)):
        profile = db.get_agent_profile_by_slug(slug)
    if not profile:
        return _err(rid, 4040, "profile not found")
    return _ok(rid, {"profile": _profile_for_projection(profile, _projection(params, default="detail"))})


@method("profile.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"profiles": []})
    include_archived = _bool((params or {}).get("include_archived", (params or {}).get("includeArchived")))
    projection = _projection(params)
    profiles = db.list_agent_profiles(include_archived=include_archived)
    return _ok(rid, {"profiles": [_profile_for_projection(profile, projection) for profile in profiles]})


@method("profile.archive")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes profile registry db unavailable")
    profile_id = _text(
        (params or {}).get("profile_id")
        or (params or {}).get("profileId")
        or (params or {}).get("agent_profile_id")
        or (params or {}).get("agentProfileId")
        or (params or {}).get("id")
    )
    profile = db.archive_agent_profile(profile_id)
    if not profile:
        return _err(rid, 4040, "profile not found")
    return _ok(rid, {"profile": profile})


@method("profile.growth.summary")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes profile registry db unavailable")
    profile_id = _profile_id_from_params(params)
    version_id = _version_id_from_params(params)
    range_preset = _text((params or {}).get("range_preset") or (params or {}).get("rangePreset"))
    start_date = _text((params or {}).get("start_date") or (params or {}).get("startDate"))
    end_date = _text((params or {}).get("end_date") or (params or {}).get("endDate"))
    _growth_diagnostic(
        "rpc_request_started",
        rid=rid,
        profile_id=profile_id,
        version_id=version_id,
        range_preset=range_preset,
        start_date=start_date,
        end_date=end_date,
    )
    if not profile_id:
        _growth_diagnostic("rpc_missing_profile_id", rid=rid)
        return _err(rid, 4006, "agent profile id required")
    summary = db.agent_profile_growth_summary(
        profile_id,
        agent_profile_version_id=version_id,
        range_preset=range_preset,
        start_date=start_date,
        end_date=end_date,
    )
    if not summary:
        _growth_diagnostic("rpc_profile_not_found", rid=rid, profile_id=profile_id, version_id=version_id)
        return _err(rid, 4040, "profile not found")
    _growth_diagnostic(
        "rpc_request_finished",
        rid=rid,
        profile_id=profile_id,
        version_id=version_id,
        memory_items=summary.get("memoryItems", 0),
        project_memory_items=summary.get("projectMemoryItems", 0),
        user_memory_items=summary.get("userMemoryItems", 0),
        skill_count=summary.get("skillCount", 0),
        session_count=summary.get("sessionCount", 0),
        daily_point_count=len(summary.get("dailyGrowth") or []),
        recent_event_count=len(summary.get("recentEvents") or []),
        latest_activity_at=summary.get("latestActivityAt", ""),
    )
    return _ok(rid, {"growth": summary, "summary": summary})


@method("profile.draft.upsert")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes profile registry db unavailable")
    try:
        draft = db.upsert_agent_profile_draft(**_draft_payload(params or {}))
        return _ok(rid, {"draft": draft})
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except sqlite3.Error as exc:
        return _err(rid, 5009, f"profile registry sqlite error: {exc}")


@method("profile.draft.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes profile registry db unavailable")
    draft_id = _text((params or {}).get("draft_id") or (params or {}).get("draftId") or (params or {}).get("id"))
    draft = db.get_agent_profile_draft(draft_id)
    if not draft:
        return _err(rid, 4040, "profile draft not found")
    return _ok(rid, {"draft": draft})


@method("profile.draft.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"drafts": []})
    raw_statuses = (params or {}).get("statuses", (params or {}).get("status"))
    if isinstance(raw_statuses, str):
        statuses = [item.strip() for item in raw_statuses.split(",") if item.strip()]
    else:
        statuses = _string_array(raw_statuses)
    drafts = db.list_agent_profile_drafts(
        include_published=_bool((params or {}).get("include_published", (params or {}).get("includePublished"))),
        include_discarded=_bool((params or {}).get("include_discarded", (params or {}).get("includeDiscarded"))),
        statuses=statuses,
        source_session_id=_text((params or {}).get("source_session_id") or (params or {}).get("sourceSessionId")),
        source_agent_profile_id=_text((params or {}).get("source_agent_profile_id") or (params or {}).get("sourceAgentProfileId")),
        workspace_id=_text((params or {}).get("workspace_id") or (params or {}).get("workspaceId")),
    )
    return _ok(rid, {"drafts": drafts})


@method("profile.draft.discard")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes profile registry db unavailable")
    draft_id = _text((params or {}).get("draft_id") or (params or {}).get("draftId") or (params or {}).get("id"))
    draft = db.discard_agent_profile_draft(draft_id)
    if not draft:
        return _err(rid, 4040, "profile draft not found")
    return _ok(rid, {"draft": draft})
