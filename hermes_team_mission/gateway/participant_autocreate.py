from __future__ import annotations

import logging
from typing import Any, Iterable

_log = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _member_id(member: dict[str, Any]) -> str:
    return _text(member.get("member_id") or member.get("memberId") or member.get("id"))


def _profile_id(member: dict[str, Any]) -> str:
    return _text(member.get("agent_profile_id") or member.get("agentProfileId"))


def _display_name(member: dict[str, Any]) -> str:
    profile = member.get("profile") if isinstance(member.get("profile"), dict) else {}
    return _text(
        member.get("display_name")
        or member.get("displayName")
        or member.get("name")
        or member.get("profile_name")
        or member.get("profileName")
        or member.get("agent_profile_name")
        or member.get("agentProfileName")
        or profile.get("name")
    )


def _avatar(member: dict[str, Any]) -> str:
    profile = member.get("profile") if isinstance(member.get("profile"), dict) else {}
    return _text(
        member.get("avatar")
        or member.get("profile_avatar")
        or member.get("profileAvatar")
        or member.get("agent_profile_avatar")
        or member.get("agentProfileAvatar")
        or profile.get("avatar")
    )


def _profile(db: Any, profile_id: str) -> dict[str, Any]:
    if not profile_id:
        return {}
    try:
        profile = db.profiles.get_agent_profile(profile_id) or {}
    except Exception:
        return {}
    return profile if isinstance(profile, dict) else {}


def _display_name_with_profile(member: dict[str, Any], profile: dict[str, Any]) -> str:
    return _display_name(member) or _text(profile.get("name"))


def _avatar_with_profile(member: dict[str, Any], profile: dict[str, Any]) -> str:
    return _avatar(member) or _text(profile.get("avatar"))


def _role(member: dict[str, Any]) -> str:
    return _text(member.get("role")).lower()


def _leader_member(members: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = [item for item in members if isinstance(item, dict)]
    return next(
        (item for item in materialized if _role(item) in {"lead", "leader"}),
        materialized[0] if materialized else {},
    )


def _team_members(db: Any, team_id: str, members: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if members is not None:
        return [item for item in members if isinstance(item, dict)]
    if team_id:
        loaded = db.teams.list_agent_team_members(team_id) or []
        return [item for item in loaded if isinstance(item, dict)]
    return []


def _warn(source: str, conversation_session_id: str, participant: str, exc: Exception) -> None:
    _log.warning(
        "%s participant auto-create skipped conversation_session_id=%s participant=%s: %s",
        source,
        conversation_session_id,
        participant,
        exc,
    )


def ensure_team_conversation_participants(
    db: Any,
    *,
    conversation_session_id: str,
    team_id: str,
    members: Iterable[dict[str, Any]] | None = None,
    leader_profile_params: dict[str, Any] | None = None,
    source: str,
) -> None:
    conversation_session_id = _text(conversation_session_id)
    team_id = _text(team_id)
    if not conversation_session_id:
        return
    try:
        resolved_members = _team_members(db, team_id, members)
    except Exception as exc:
        _warn(source, conversation_session_id, "team-registry", exc)
        resolved_members = []

    try:
        db.participants.ensure_user_participant(
            conversation_session_id,
            user_id="default",
        )
    except Exception as exc:
        _warn(source, conversation_session_id, "user", exc)

    leader = _leader_member(resolved_members)
    leader_profile_params = leader_profile_params if isinstance(leader_profile_params, dict) else {}
    if team_id:
        try:
            leader_profile_id = _text(leader_profile_params.get("agent_profile_id")) or _profile_id(leader)
            leader_profile = _profile(db, leader_profile_id)
            db.participants.ensure_leader_participant(
                conversation_session_id,
                team_id=team_id,
                leader_profile_id=leader_profile_id,
                display_name=_display_name_with_profile(leader, leader_profile),
                avatar=_avatar_with_profile(leader, leader_profile),
            )
        except Exception as exc:
            _warn(source, conversation_session_id, f"leader:{team_id}", exc)

    for member in resolved_members:
        if _role(member) in {"lead", "leader"}:
            continue
        member_id = _member_id(member)
        if not member_id:
            continue
        try:
            profile_id = _profile_id(member)
            profile = _profile(db, profile_id)
            db.participants.ensure_member_participant(
                conversation_session_id,
                member_id=member_id,
                agent_profile_id=profile_id,
                display_name=_display_name_with_profile(member, profile),
                avatar=_avatar_with_profile(member, profile),
            )
        except Exception as exc:
            _warn(source, conversation_session_id, f"member:{member_id}", exc)


def ensure_member_chat_participant(
    db: Any,
    *,
    conversation_session_id: str,
    member: dict[str, Any],
    member_id: str,
    source: str,
) -> None:
    try:
        profile_id = _profile_id(member)
        profile = _profile(db, profile_id)
        db.participants.ensure_member_participant(
            _text(conversation_session_id),
            member_id=_text(member_id),
            agent_profile_id=profile_id,
            display_name=_display_name_with_profile(member, profile),
            avatar=_avatar_with_profile(member, profile),
        )
    except Exception as exc:
        _warn(source, _text(conversation_session_id), f"member:{member_id}", exc)
