"""Resolve team-owned leader runtime context from Hermes state.

Team conversation submit must not depend on Dovie passing profile runtime
details.  This module is the single ingress normalizer for requests that need
the team leader runtime worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [text for text in (_text(item) for item in value) if text]
    return []


def _team_id_from_params(params: dict[str, Any]) -> str:
    team = _object(params.get("team"))
    return _text(
        params.get("team_id")
        or params.get("teamId")
        or team.get("team_id")
        or team.get("teamId")
        or team.get("id")
    )


def _mission_id_from_params(params: dict[str, Any]) -> str:
    return _text(params.get("mission_id") or params.get("missionId"))


def _conversation_id_from_params(params: dict[str, Any]) -> str:
    return _text(
        params.get("conversation_id")
        or params.get("conversationId")
        or params.get("team_conversation_id")
        or params.get("teamConversationId")
    )


def _conversation_session_id_from_params(params: dict[str, Any]) -> str:
    return _text(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("conversation_team_session_id")
        or params.get("conversationTeamSessionId")
        or params.get("team_session_id")
        or params.get("teamSessionId")
        or params.get("conversationSessionId")
        or params.get("conversation_session_id")
        or params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or params.get("sessionId")
    )


def _runtime_subject_from_params(params: dict[str, Any]) -> str:
    return _text(
        _conversation_id_from_params(params)
        or _mission_id_from_params(params)
        or _conversation_session_id_from_params(params)
    )


def _team_leader_scope_key(params: dict[str, Any]) -> str:
    requested = _text(params.get("runtime_scope_key") or params.get("runtimeScopeKey"))
    if requested.startswith("team:"):
        return requested
    subject = _runtime_subject_from_params(params)
    return f"team:{subject}:leader-conversation" if subject else ""


def _db_from_arg(db: Any | None) -> Any:
    if db is not None:
        return db
    try:
        from hermes_team_mission.runtime.profile_scope import team_mission_control_db

        return team_mission_control_db()
    except Exception:
        return None


def _mission_from_db(db: Any, mission_id: str) -> dict[str, Any]:
    if not db or not mission_id:
        return {}
    try:
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    except Exception:
        return {}
    if not isinstance(graph, dict):
        return {}
    return _object(graph.get("mission"))


def _conversation_bundle_from_db(db: Any, identifier: str) -> dict[str, Any]:
    if not db or not identifier or not hasattr(db, "resolve_team_mission_conversation"):
        return {}
    try:
        resolved = db.resolve_team_mission_conversation(identifier)
    except Exception:
        return {}
    return resolved if isinstance(resolved, dict) else {}


def _team_id_from_state(db: Any, params: dict[str, Any]) -> str:
    team_id = _team_id_from_params(params)
    if team_id:
        return team_id
    mission = _mission_from_db(db, _mission_id_from_params(params))
    team_id = _text(mission.get("team_id") or mission.get("teamId"))
    if team_id:
        return team_id
    for identifier in (_conversation_id_from_params(params), _conversation_session_id_from_params(params)):
        bundle = _conversation_bundle_from_db(db, identifier)
        conversation = _object(bundle.get("conversation"))
        mission = _object(bundle.get("mission"))
        team_id = _text(
            conversation.get("team_id")
            or conversation.get("teamId")
            or mission.get("team_id")
            or mission.get("teamId")
        )
        if team_id:
            return team_id
    return ""


def _active_members(team: dict[str, Any]) -> list[dict[str, Any]]:
    members = [dict(item) for item in _list(team.get("members")) if isinstance(item, dict)]
    active = [
        item
        for item in members
        if _text(item.get("status") or "active") not in {"archived", "deleted", "inactive"}
    ]
    return active or members


def _leader_member(team: dict[str, Any]) -> dict[str, Any]:
    members = _active_members(team)
    lead_profile_id = _text(team.get("lead_agent_profile_id") or team.get("leadAgentProfileId"))
    leader = next(
        (item for item in members if _text(item.get("role")).lower() in {"lead", "leader"}),
        None,
    )
    if not leader and lead_profile_id:
        leader = next(
            (
                item
                for item in members
                if _text(item.get("agent_profile_id") or item.get("agentProfileId") or item.get("profile_id"))
                == lead_profile_id
            ),
            None,
        )
    if leader:
        return dict(leader)
    if lead_profile_id:
        return {
            "id": f"{_text(team.get('id') or team.get('team_id') or team.get('teamId'))}:leader",
            "member_id": f"{_text(team.get('id') or team.get('team_id') or team.get('teamId'))}:leader",
            "team_id": _text(team.get("id") or team.get("team_id") or team.get("teamId")),
            "agent_profile_id": lead_profile_id,
            "role": "lead",
            "status": "active",
        }
    return dict(members[0]) if members else {}


def _profile_id_from_member(member: dict[str, Any]) -> str:
    return _text(
        member.get("agent_profile_id")
        or member.get("agentProfileId")
        or member.get("profile_id")
        or member.get("profileId")
    )


def _profile_version_id_from_member(member: dict[str, Any]) -> str:
    return _text(
        member.get("agent_profile_version_id")
        or member.get("agentProfileVersionId")
        or member.get("profile_version_id")
        or member.get("profileVersionId")
        or member.get("version_id")
        or member.get("versionId")
    )


def _profile_version_id(profile: dict[str, Any]) -> str:
    return _text(
        profile.get("agent_profile_version_id")
        or profile.get("agentProfileVersionId")
        or profile.get("current_version_id")
        or profile.get("currentVersionId")
    )


def _profile_runtime_home(profile: dict[str, Any]) -> str:
    return _text(
        profile.get("runtime_home_path")
        or profile.get("runtimeHomePath")
        or profile.get("hermes_home_path")
        or profile.get("hermesHomePath")
    )


def _effective_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile = dict(profile)
    profile_id = _text(profile.get("id"))
    runtime_home = _profile_runtime_home(profile)
    if profile_id:
        profile.setdefault("runtime_scope_key", f"profile:{profile_id}")
        profile.setdefault("runtimeScopeKey", f"profile:{profile_id}")
    if runtime_home:
        profile.setdefault("runtime_home_path", runtime_home)
        profile.setdefault("runtimeHomePath", runtime_home)
    return profile


def _resolve_profile(db: Any, member: dict[str, Any]) -> dict[str, Any]:
    profile_id = _profile_id_from_member(member)
    if not profile_id:
        raise ValueError("team leader profile required")
    if not hasattr(db, "profiles"):
        raise ValueError("team leader profile registry unavailable")
    profile = db.profiles.get_agent_profile(profile_id)
    if not isinstance(profile, dict) or not profile:
        raise ValueError(f"team leader profile not found: {profile_id}")

    return _effective_profile(profile)


def _profile_runtime_scope_key(profile: dict[str, Any]) -> str:
    profile_id = _text(profile.get("id"))
    return _text(
        profile.get("runtime_scope_key")
        or profile.get("runtimeScopeKey")
        or (f"profile:{profile_id}" if profile_id else "")
    )


def _dovie_profile_payload(profile: dict[str, Any], *, runtime_home: str, profile_scope_key: str) -> dict[str, Any]:
    profile_id = _text(profile.get("id"))
    version_id = _profile_version_id(profile)
    payload = {
        "id": profile_id,
        "agentProfileId": profile_id,
        "hermesHomePath": runtime_home,
        "runtimeScopeKey": profile_scope_key,
        "name": _text(profile.get("name")),
        "avatar": _text(profile.get("avatar")),
        "defaultModel": _text(profile.get("defaultModel") or profile.get("default_model")),
        "defaultProvider": _text(profile.get("defaultProvider") or profile.get("default_provider")),
        "defaultPermissionMode": _text(profile.get("defaultPermissionMode") or profile.get("default_permission_mode") or "default"),
        "defaultToolsets": _string_list(profile.get("defaultToolsets") or profile.get("default_toolsets")),
    }
    if version_id:
        payload["agentProfileVersionId"] = version_id
    env = _object(profile.get("env"))
    if env:
        payload["env"] = {str(key): str(value) for key, value in env.items()}
    return payload


def _member_roster_entry(member: dict[str, Any]) -> dict[str, Any]:
    profile_id = _profile_id_from_member(member)
    version_id = _profile_version_id_from_member(member)
    entry = {
        "id": _text(member.get("id") or member.get("member_id") or member.get("memberId")),
        "member_id": _text(member.get("member_id") or member.get("memberId") or member.get("id")),
        "team_id": _text(member.get("team_id") or member.get("teamId")),
        "agent_profile_id": profile_id,
        "agentProfileId": profile_id,
        "profile_id": profile_id,
        "profileId": profile_id,
        "role": _text(member.get("role") or "member"),
        "capability_tags": _string_list(member.get("capability_tags") or member.get("capabilityTags")),
        "capabilityTags": _string_list(member.get("capabilityTags") or member.get("capability_tags")),
        "auto_assignable": bool(member.get("auto_assignable", member.get("autoAssignable", True)) is not False),
        "autoAssignable": bool(member.get("autoAssignable", member.get("auto_assignable", True)) is not False),
        "max_concurrent_nodes": int(member.get("max_concurrent_nodes") or member.get("maxConcurrentNodes") or 1),
        "maxConcurrentNodes": int(member.get("maxConcurrentNodes") or member.get("max_concurrent_nodes") or 1),
        "permission_mode": _text(member.get("permission_mode") or member.get("permissionMode") or "inherit_profile"),
        "permissionMode": _text(member.get("permissionMode") or member.get("permission_mode") or "inherit_profile"),
        "status": _text(member.get("status") or "active"),
    }
    if version_id:
        entry["agent_profile_version_id"] = version_id
        entry["agentProfileVersionId"] = version_id
        entry["profile_version_id"] = version_id
        entry["profileVersionId"] = version_id
    return entry


def _runtime_member_roster_entry(db: Any, member: dict[str, Any]) -> dict[str, Any]:
    entry = _member_roster_entry(member)
    profile = _resolve_profile(db, member)
    runtime_home = _profile_runtime_home(profile)
    if not runtime_home:
        raise ValueError(f"team member profile runtime home required: {_text(profile.get('id'))}")
    profile_scope_key = _profile_runtime_scope_key(profile)
    if not profile_scope_key:
        raise ValueError(f"team member profile runtime scope required: {_text(profile.get('id'))}")
    dovie_profile = _dovie_profile_payload(profile, runtime_home=runtime_home, profile_scope_key=profile_scope_key)
    default_toolsets = _string_list(profile.get("defaultToolsets") or profile.get("default_toolsets"))
    recommended_skills = _string_list(profile.get("recommendedSkills") or profile.get("recommended_skills"))
    enriched = {
        **entry,
        "runtime_scope_key": profile_scope_key,
        "runtimeScopeKey": profile_scope_key,
        "profile_runtime_scope_key": profile_scope_key,
        "profileRuntimeScopeKey": profile_scope_key,
        "hermes_home_path": runtime_home,
        "hermesHomePath": runtime_home,
        "dovie_profile": dovie_profile,
        "dovieProfile": dovie_profile,
    }
    if default_toolsets:
        enriched["default_toolsets"] = default_toolsets
        enriched["defaultToolsets"] = default_toolsets
    if recommended_skills:
        enriched["recommended_skills"] = recommended_skills
        enriched["recommendedSkills"] = recommended_skills
    return enriched


def _leader_runtime_context(
    *,
    team_id: str,
    leader_runtime_scope_key: str,
    profile_scope_key: str,
    profile: dict[str, Any],
    runtime_home: str,
    leader_member: dict[str, Any],
) -> dict[str, Any]:
    profile_id = _text(profile.get("id"))
    version_id = _profile_version_id(profile)
    context = {
        "team_id": team_id,
        "teamId": team_id,
        "runtime_scope_key": leader_runtime_scope_key,
        "runtimeScopeKey": leader_runtime_scope_key,
        "profile_runtime_scope_key": profile_scope_key,
        "profileRuntimeScopeKey": profile_scope_key,
        "agent_profile_id": profile_id,
        "agentProfileId": profile_id,
        "hermes_home_path": runtime_home,
        "hermesHomePath": runtime_home,
        "member_id": _text(leader_member.get("member_id") or leader_member.get("id")),
        "memberId": _text(leader_member.get("member_id") or leader_member.get("id")),
    }
    if version_id:
        context["agent_profile_version_id"] = version_id
        context["agentProfileVersionId"] = version_id
    return context


def resolve_team_runtime_members(
    params: dict[str, Any],
    *,
    mission: dict[str, Any] | None = None,
    db: Any | None = None,
) -> list[dict[str, Any]]:
    """Hydrate active team members from Hermes registries for runtime execution."""

    seed = dict(params or {})
    mission = mission if isinstance(mission, dict) else {}
    if mission:
        if _text(mission.get("team_id") or mission.get("teamId")):
            seed.setdefault("team_id", _text(mission.get("team_id") or mission.get("teamId")))
            seed.setdefault("teamId", _text(mission.get("teamId") or mission.get("team_id")))
        if _text(mission.get("mission_id") or mission.get("missionId")):
            seed.setdefault("mission_id", _text(mission.get("mission_id") or mission.get("missionId")))
            seed.setdefault("missionId", _text(mission.get("missionId") or mission.get("mission_id")))
        if _text(mission.get("conversation_id") or mission.get("conversationId")):
            seed.setdefault("conversation_id", _text(mission.get("conversation_id") or mission.get("conversationId")))
            seed.setdefault("conversationId", _text(mission.get("conversationId") or mission.get("conversation_id")))

    resolved_db = _db_from_arg(db)
    team_id = _team_id_from_state(resolved_db, seed)
    if not team_id:
        raise ValueError("team_id required for team runtime members")
    if resolved_db is None or not hasattr(resolved_db, "teams"):
        raise ValueError("team registry unavailable for team runtime members")
    team = resolved_db.teams.get_agent_team_with_members(team_id)
    if not isinstance(team, dict) or not team:
        raise ValueError(f"team not found: {team_id}")
    members = _active_members(team)
    if not members:
        raise ValueError(f"team members not found: {team_id}")
    return [_runtime_member_roster_entry(resolved_db, member) for member in members]


@dataclass(frozen=True)
class TeamLeaderRuntimeResolution:
    params: dict[str, Any]
    leader_runtime_context: dict[str, Any]
    team: dict[str, Any]
    leader_member: dict[str, Any]
    profile: dict[str, Any]


def resolve_team_leader_runtime_params(
    params: dict[str, Any],
    *,
    db: Any | None = None,
) -> TeamLeaderRuntimeResolution:
    """Return params enriched with Hermes-owned team leader runtime context."""

    if not isinstance(params, dict):
        params = {}
    resolved_db = _db_from_arg(db)
    team_id = _team_id_from_state(resolved_db, params)
    if not team_id:
        raise ValueError("team_id required for team leader runtime context")
    if resolved_db is None or not hasattr(resolved_db, "teams"):
        raise ValueError("team registry unavailable for team leader runtime context")

    team = resolved_db.teams.get_agent_team_with_members(team_id)
    if not isinstance(team, dict) or not team:
        raise ValueError(f"team not found: {team_id}")
    leader_member = _leader_member(team)
    if not leader_member:
        raise ValueError(f"team leader member not found: {team_id}")

    profile = _resolve_profile(resolved_db, leader_member)
    runtime_home = _profile_runtime_home(profile)
    if not runtime_home:
        raise ValueError(f"team leader profile runtime home required: {_text(profile.get('id'))}")
    profile_scope_key = _profile_runtime_scope_key(profile)
    if not profile_scope_key:
        raise ValueError(f"team leader profile runtime scope required: {_text(profile.get('id'))}")
    leader_runtime_scope_key = _team_leader_scope_key(params)
    if not leader_runtime_scope_key:
        raise ValueError("conversation_id or mission_id required for team leader runtime context")

    dovie_profile = _dovie_profile_payload(profile, runtime_home=runtime_home, profile_scope_key=profile_scope_key)
    roster = [_member_roster_entry(member) for member in _active_members(team)]
    leader_member_id = _text(leader_member.get("member_id") or leader_member.get("id"))
    for index, member in enumerate(roster):
        if _text(member.get("member_id") or member.get("id")) == leader_member_id:
            roster[index] = {
                **member,
                "role": "lead" if _text(member.get("role")).lower() in {"lead", "leader"} else member.get("role", "lead"),
                "runtime_scope_key": profile_scope_key,
                "runtimeScopeKey": profile_scope_key,
                "profile_runtime_scope_key": profile_scope_key,
                "profileRuntimeScopeKey": profile_scope_key,
                "hermes_home_path": runtime_home,
                "hermesHomePath": runtime_home,
                "dovie_profile": dovie_profile,
                "dovieProfile": dovie_profile,
            }
            break
    else:
        roster.insert(
            0,
            {
                **_member_roster_entry(leader_member),
                "role": "lead",
                "runtime_scope_key": profile_scope_key,
                "runtimeScopeKey": profile_scope_key,
                "profile_runtime_scope_key": profile_scope_key,
                "profileRuntimeScopeKey": profile_scope_key,
                "hermes_home_path": runtime_home,
                "hermesHomePath": runtime_home,
                "dovie_profile": dovie_profile,
                "dovieProfile": dovie_profile,
            },
        )

    enriched = dict(params)
    enriched.update(
        {
            "team_id": team_id,
            "teamId": team_id,
            "runtime_scope_key": leader_runtime_scope_key,
            "runtimeScopeKey": leader_runtime_scope_key,
            "profile_runtime_scope_key": profile_scope_key,
            "profileRuntimeScopeKey": profile_scope_key,
            "agent_profile_id": _text(profile.get("id")),
            "agentProfileId": _text(profile.get("id")),
            "dovie_profile": dovie_profile,
            "dovieProfile": dovie_profile,
            "members": roster,
        }
    )
    version_id = _profile_version_id(profile)
    if version_id:
        enriched["agent_profile_version_id"] = version_id
        enriched["agentProfileVersionId"] = version_id
    if not _text(enriched.get("mode")):
        mode = _text(team.get("default_mode") or team.get("defaultMode"))
        if mode:
            enriched["mode"] = mode
    enriched["team_registry_snapshot"] = {
        "team_id": team_id,
        "teamId": team_id,
        "name": _text(team.get("name")),
        "default_mode": _text(team.get("default_mode") or team.get("defaultMode")),
        "defaultMode": _text(team.get("defaultMode") or team.get("default_mode")),
        "lead_agent_profile_id": _text(team.get("lead_agent_profile_id") or team.get("leadAgentProfileId")),
        "leadAgentProfileId": _text(team.get("leadAgentProfileId") or team.get("lead_agent_profile_id")),
        "member_count": len(roster),
        "memberCount": len(roster),
    }
    context = _leader_runtime_context(
        team_id=team_id,
        leader_runtime_scope_key=leader_runtime_scope_key,
        profile_scope_key=profile_scope_key,
        profile=profile,
        runtime_home=runtime_home,
        leader_member=leader_member,
    )
    enriched["leader_runtime_context"] = context
    enriched["leaderRuntimeContext"] = context
    return TeamLeaderRuntimeResolution(
        params=enriched,
        leader_runtime_context=context,
        team=team,
        leader_member=leader_member,
        profile=profile,
    )


def resolve_team_leader_runtime_request(
    req: Any,
    *,
    db: Any | None = None,
) -> Any:
    if not isinstance(req, dict):
        return req
    params = req.get("params")
    if not isinstance(params, dict):
        return req
    resolution = resolve_team_leader_runtime_params(params, db=db)
    return {**req, "params": resolution.params}
