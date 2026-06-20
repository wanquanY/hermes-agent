# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import uuid

from hermes_team_mission_profile_tools import team_mission_control_db as _team_registry_control_db
from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _get_db():
    return _team_registry_control_db()


def _text(value) -> str:
    return str(value or "").strip()


def _object(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _array(value) -> list:
    return list(value) if isinstance(value, list) else []


def _bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _projection(params: dict | None, *, default: str = "summary") -> str:
    value = _text((params or {}).get("projection") or (params or {}).get("view") or default).lower()
    return value if value in {"summary", "detail", "raw"} else default


def _member_summary(member: dict) -> dict:
    if not isinstance(member, dict):
        return {}
    profile_name = _text(
        member.get("profile_name")
        or member.get("profileName")
        or member.get("agent_profile_name")
        or member.get("agentProfileName")
        or member.get("name")
    )
    profile_avatar = _text(
        member.get("profile_avatar")
        or member.get("profileAvatar")
        or member.get("agent_profile_avatar")
        or member.get("agentProfileAvatar")
        or member.get("avatar")
    )
    summary = {
        "id": _text(member.get("id") or member.get("member_id")),
        "member_id": _text(member.get("member_id") or member.get("id")),
        "team_id": _text(member.get("team_id")),
        "agent_profile_id": _text(member.get("agent_profile_id")),
        "agent_profile_version_id": _text(member.get("agent_profile_version_id")),
        "role": _text(member.get("role")) or "member",
        "status": _text(member.get("status")) or "active",
    }
    if profile_name:
        summary.update({
            "name": profile_name,
            "profile_name": profile_name,
            "profileName": profile_name,
            "agent_profile_name": profile_name,
            "agentProfileName": profile_name,
        })
    if profile_avatar:
        summary.update({
            "avatar": profile_avatar,
            "profile_avatar": profile_avatar,
            "profileAvatar": profile_avatar,
            "agent_profile_avatar": profile_avatar,
            "agentProfileAvatar": profile_avatar,
        })
    return summary


def _team_summary(team: dict, members: list[dict] | None = None) -> dict:
    if not isinstance(team, dict):
        return {}
    summary = {**team}
    summary.pop("members", None)
    loaded_members = members if isinstance(members, list) else None
    if loaded_members is None:
        member_count = int(team.get("member_count") or team.get("memberCount") or 0)
        leader = team.get("leader_member") if isinstance(team.get("leader_member"), dict) else team.get("leaderMember")
        leader = leader if isinstance(leader, dict) else {}
        display_members = team.get("display_members") if isinstance(team.get("display_members"), list) else team.get("displayMembers")
        display_members = display_members if isinstance(display_members, list) else []
    else:
        member_count = len(loaded_members)
        leader = next(
            (
                member for member in loaded_members
                if _text(member.get("role")).lower() in {"lead", "leader"}
            ),
            loaded_members[0] if loaded_members else {},
        )
        display_members = loaded_members[:2]
    display_member_summaries = [_member_summary(member) for member in display_members if isinstance(member, dict)]
    return {
        **summary,
        "member_count": member_count,
        "memberCount": member_count,
        "leader_member": _member_summary(leader),
        "leaderMember": _member_summary(leader),
        "display_members": display_member_summaries,
        "displayMembers": display_member_summaries,
        "projection": "summary",
    }


def _team_for_projection(team: dict, *, members: list[dict] | None = None, projection: str = "summary", include_members: bool = False) -> dict:
    if projection == "raw":
        return {**team, **({"members": members or []} if include_members else {})}
    if projection == "detail" or include_members:
        return {
            **_team_summary(team, members),
            "projection": "detail",
            "members": members or [],
        }
    return _team_summary(team, members)


def _team_payload(params: dict) -> dict:
    raw = params.get("team") if isinstance(params.get("team"), dict) else params
    return {
        "team_id": _text(raw.get("team_id") or raw.get("teamId") or raw.get("id")) or uuid.uuid4().hex,
        "name": _text(raw.get("name")),
        "avatar": raw.get("avatar"),
        "description": _text(raw.get("description")),
        "lead_agent_profile_id": _text(raw.get("lead_agent_profile_id") or raw.get("leadAgentProfileId")),
        "default_mode": _text(raw.get("default_mode") or raw.get("defaultMode")) or "supervised_mission",
        "policy": _object(raw.get("policy")),
        "status": _text(raw.get("status")) or "active",
        "created_at": raw.get("created_at") or raw.get("createdAt"),
        "updated_at": raw.get("updated_at") or raw.get("updatedAt"),
    }


def _member_payload(params: dict, *, fallback_team_id: str = "") -> dict:
    raw = params.get("member") if isinstance(params.get("member"), dict) else params
    profile_name = _text(
        raw.get("profile_name")
        or raw.get("profileName")
        or raw.get("agent_profile_name")
        or raw.get("agentProfileName")
        or raw.get("name")
    )
    profile_avatar = _text(
        raw.get("profile_avatar")
        or raw.get("profileAvatar")
        or raw.get("agent_profile_avatar")
        or raw.get("agentProfileAvatar")
        or raw.get("avatar")
    )
    return {
        "member_id": _text(raw.get("member_id") or raw.get("memberId") or raw.get("id")) or uuid.uuid4().hex,
        "team_id": _text(raw.get("team_id") or raw.get("teamId") or fallback_team_id),
        "agent_profile_id": _text(raw.get("agent_profile_id") or raw.get("agentProfileId")),
        "agent_profile_version_id": _text(raw.get("agent_profile_version_id") or raw.get("agentProfileVersionId")),
        "profile_name": profile_name,
        "profile_avatar": profile_avatar,
        "role": _text(raw.get("role")) or "member",
        "capability_tags": _array(raw.get("capability_tags") or raw.get("capabilityTags")),
        "auto_assignable": raw.get("auto_assignable", raw.get("autoAssignable", True)) is not False,
        "max_concurrent_nodes": int(raw.get("max_concurrent_nodes") or raw.get("maxConcurrentNodes") or 1),
        "permission_mode": _text(raw.get("permission_mode") or raw.get("permissionMode")) or "inherit_profile",
        "status": _text(raw.get("status")) or "active",
        "created_at": raw.get("created_at") or raw.get("createdAt"),
        "updated_at": raw.get("updated_at") or raw.get("updatedAt"),
    }


@method("team_registry.team.upsert")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes team registry db unavailable")
    try:
        team = db.upsert_agent_team(**_team_payload(params or {}))
        members = []
        if isinstance((params or {}).get("members"), list):
            for raw_member in (params or {}).get("members") or []:
                member = db.upsert_agent_team_member(**_member_payload(
                    {"member": raw_member},
                    fallback_team_id=team["id"],
                ))
                members.append(member)
        else:
            members = db.list_agent_team_members(team["id"])
        return _ok(rid, {"team": {**team, "members": members}})
    except ValueError as exc:
        return _err(rid, 4006, str(exc))


@method("team_registry.team.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes team registry db unavailable")
    team_id = _text((params or {}).get("team_id") or (params or {}).get("teamId") or (params or {}).get("id"))
    team = db.get_agent_team_with_members(team_id) if hasattr(db, "get_agent_team_with_members") else db.get_agent_team(team_id)
    if not team:
        return _err(rid, 4040, "team not found")
    members = team.get("members") if isinstance(team.get("members"), list) else db.list_agent_team_members(team["id"])
    include_members = _bool((params or {}).get("include_members", (params or {}).get("includeMembers")), default=True)
    return _ok(rid, {
        "team": _team_for_projection(
            team,
            members=members,
            projection=_projection(params, default="detail"),
            include_members=include_members,
        ),
    })


@method("team_registry.team.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"teams": []})
    include_archived = _bool((params or {}).get("include_archived", (params or {}).get("includeArchived")))
    projection = _projection(params)
    include_members = _bool((params or {}).get("include_members", (params or {}).get("includeMembers")), default=False)
    if projection == "summary" and not include_members and hasattr(db, "list_agent_team_summaries"):
        return _ok(rid, {"teams": [
            _team_for_projection(team, projection="summary", include_members=False)
            for team in db.list_agent_team_summaries(include_archived=include_archived)
        ]})
    teams = []
    for team in db.list_agent_teams(include_archived=include_archived):
        members = db.list_agent_team_members(team.get("id", ""))
        teams.append(_team_for_projection(
            team,
            members=members,
            projection=projection,
            include_members=include_members,
        ))
    return _ok(rid, {"teams": teams})


@method("team_registry.team.archive")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes team registry db unavailable")
    team_id = _text((params or {}).get("team_id") or (params or {}).get("teamId") or (params or {}).get("id"))
    team = db.archive_agent_team(team_id)
    if not team:
        return _err(rid, 4040, "team not found")
    return _ok(rid, {"team": {**team, "members": db.list_agent_team_members(team["id"])}})


@method("team_registry.member.upsert")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes team registry db unavailable")
    try:
        member = db.upsert_agent_team_member(**_member_payload(params or {}))
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    return _ok(rid, {"member": member})


@method("team_registry.member.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes team registry db unavailable")
    member_id = _text((params or {}).get("member_id") or (params or {}).get("memberId") or (params or {}).get("id"))
    member = db.get_agent_team_member(member_id)
    if not member:
        return _err(rid, 4040, "team member not found")
    return _ok(rid, {"member": member})


@method("team_registry.member.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"members": []})
    team_id = _text((params or {}).get("team_id") or (params or {}).get("teamId") or (params or {}).get("id"))
    if not team_id:
        return _err(rid, 4006, "team_id required")
    if not db.get_agent_team(team_id):
        return _err(rid, 4040, "team not found")
    return _ok(rid, {"members": db.list_agent_team_members(team_id)})


@method("team_registry.member.delete")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _err(rid, 5008, "Hermes team registry db unavailable")
    member_id = _text((params or {}).get("member_id") or (params or {}).get("memberId") or (params or {}).get("id"))
    try:
        member = db.delete_agent_team_member(member_id)
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    if not member:
        return _err(rid, 4040, "team member not found")
    return _ok(rid, {"removed": member})
