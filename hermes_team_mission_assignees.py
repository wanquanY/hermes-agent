from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hermes_team_mission_memory_utils import text as _text
from hermes_team_mission_modes import TeamMissionMember
from hermes_team_mission_node_kinds import is_team_mission_control_node_kind
from hermes_team_mission_node_kinds import normalize_team_mission_node_kind


_LEADER_ROLES = {"leader", "lead", "root"}


@dataclass(frozen=True)
class TeamMissionAssignee:
    member_id: str = ""
    profile_id: str = ""
    profile_version_id: str = ""
    display_name: str = ""
    role: str = ""
    resolved_by: str = ""


def normalized_member_dicts(members: Sequence[Mapping[str, Any] | TeamMissionMember] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
        return result
    for raw in members:
        if not isinstance(raw, (Mapping, TeamMissionMember)):
            continue
        member = TeamMissionMember.from_raw(raw)
        if not member.member_id or member.status in {"disabled", "removed"}:
            continue
        result.append(
            {
                "member_id": member.member_id,
                "profile_id": member.profile_id,
                "profile_version_id": member.profile_version_id,
                "runtime_scope_key": member.runtime_scope_key,
                "hermes_home_path": member.hermes_home_path,
                "doxie_profile": dict(member.doxie_profile or {}),
                "display_name": member.display_name,
                "role": member.role,
                "status": member.status,
                "capability_tags": list(member.capability_tags),
                "profile_summary": member.profile_summary,
                "best_for_tasks": list(member.best_for_tasks),
                "avoid_tasks": list(member.avoid_tasks),
                "strengths": list(member.strengths),
                "limitations": list(member.limitations),
                "default_toolsets": list(member.default_toolsets),
                "recommended_skills": list(member.recommended_skills),
                "radar_scores": list(member.radar_scores),
                "metadata": dict(member.metadata or {}),
            }
        )
    return result


def mission_metadata_with_members(
    metadata: Mapping[str, Any] | None,
    members: Sequence[Mapping[str, Any] | TeamMissionMember] | None,
) -> dict[str, Any]:
    normalized = dict(metadata or {})
    normalized_members = normalized_member_dicts(members)
    if normalized_members:
        normalized["members"] = normalized_members
    return normalized


def node_metadata_with_assignee(metadata: Mapping[str, Any] | None, assignee: TeamMissionAssignee) -> dict[str, Any]:
    normalized = dict(metadata or {})
    if assignee.member_id:
        normalized["assignee_member_id"] = assignee.member_id
        normalized["assigneeMemberId"] = assignee.member_id
    else:
        normalized.pop("assignee_member_id", None)
        normalized.pop("assigneeMemberId", None)
    if assignee.display_name:
        normalized["assignee_display_name"] = assignee.display_name
        normalized["assigneeDisplayName"] = assignee.display_name
    else:
        normalized.pop("assignee_display_name", None)
        normalized.pop("assigneeDisplayName", None)
    if assignee.role:
        normalized["assignee_role"] = assignee.role
        normalized["assigneeRole"] = assignee.role
    else:
        normalized.pop("assignee_role", None)
        normalized.pop("assigneeRole", None)
    if assignee.resolved_by:
        normalized["assignee_resolved_by"] = assignee.resolved_by
    return normalized


def assignee_public_fields(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return {
        "assignee_member_id": _text(metadata.get("assignee_member_id") or metadata.get("assigneeMemberId")),
        "assignee_display_name": _text(metadata.get("assignee_display_name") or metadata.get("assigneeDisplayName")),
        "assignee_role": _text(metadata.get("assignee_role") or metadata.get("assigneeRole")),
    }


def resolve_node_assignee(
    *,
    mission_id: str,
    node_id: str,
    kind: str,
    incoming_profile_id: str = "",
    incoming_profile_version_id: str = "",
    incoming_runtime_scope_key: str = "",
    metadata: Mapping[str, Any] | None = None,
    mission_metadata: Mapping[str, Any] | None = None,
    existing_node: Mapping[str, Any] | None = None,
    leader_node: Mapping[str, Any] | None = None,
) -> tuple[str, str, str, dict[str, Any]]:
    metadata = dict(metadata or {})
    existing_node = existing_node if isinstance(existing_node, Mapping) else {}
    leader_node = leader_node if isinstance(leader_node, Mapping) else {}
    mission_metadata = mission_metadata if isinstance(mission_metadata, Mapping) else {}

    members = _members_from_metadata(mission_metadata)
    profile_id = _text(incoming_profile_id) or _text(existing_node.get("assignee_profile_id"))
    profile_version_id = _text(incoming_profile_version_id) or _text(existing_node.get("assignee_profile_version_id"))
    runtime_scope_key = _text(incoming_runtime_scope_key) or _text(existing_node.get("runtime_scope_key"))

    assignee = _explicit_assignee(metadata=metadata, profile_id=profile_id, profile_version_id=profile_version_id, members=members)
    if not assignee.member_id and not assignee.profile_id:
        assignee = _default_assignee(
            mission_id=mission_id,
            node_id=node_id,
            kind=kind,
            metadata=metadata,
            members=members,
            leader_node=leader_node,
        )

    if assignee.profile_id and not profile_id:
        profile_id = assignee.profile_id
    if assignee.profile_version_id and not profile_version_id:
        profile_version_id = assignee.profile_version_id
    if not runtime_scope_key:
        runtime_scope_key = _default_runtime_scope(
            mission_id=mission_id,
            node_id=node_id,
            kind=kind,
            assignee=assignee,
        )

    return (
        profile_id,
        profile_version_id,
        runtime_scope_key,
        node_metadata_with_assignee(metadata, assignee),
    )


def _members_from_metadata(mission_metadata: Mapping[str, Any]) -> tuple[TeamMissionAssignee, ...]:
    raw_members = mission_metadata.get("members") if isinstance(mission_metadata.get("members"), Sequence) else []
    members: list[TeamMissionAssignee] = []
    for raw in raw_members if isinstance(raw_members, Sequence) else []:
        if not isinstance(raw, Mapping):
            continue
        member = TeamMissionMember.from_raw(raw)
        if not member.member_id or member.status in {"disabled", "removed"}:
            continue
        members.append(
            TeamMissionAssignee(
                member_id=member.member_id,
                profile_id=member.profile_id,
                profile_version_id=member.profile_version_id,
                display_name=member.display_name,
                role=member.role,
                resolved_by="mission_member",
            )
        )
    return tuple(members)


def _explicit_assignee(
    *,
    metadata: Mapping[str, Any],
    profile_id: str,
    profile_version_id: str,
    members: Sequence[TeamMissionAssignee],
) -> TeamMissionAssignee:
    member_id = _text(
        metadata.get("assignee_member_id")
        or metadata.get("assigneeMemberId")
        or metadata.get("member_id")
        or metadata.get("memberId")
    )
    if member_id:
        member = _find_member(members, member_id=member_id)
        if member:
            return TeamMissionAssignee(**{**member.__dict__, "resolved_by": "explicit_member"})
        return TeamMissionAssignee()
    if profile_id:
        member = _find_member(members, profile_id=profile_id, profile_version_id=profile_version_id)
        if member:
            return TeamMissionAssignee(**{**member.__dict__, "resolved_by": "explicit_profile"})
        return TeamMissionAssignee(profile_id=profile_id, profile_version_id=profile_version_id, resolved_by="explicit_profile")
    role = _text(metadata.get("assignee_role") or metadata.get("assigneeRole"))
    if role:
        member = _find_member(members, role=role)
        if member:
            return TeamMissionAssignee(**{**member.__dict__, "resolved_by": "explicit_role"})
    return TeamMissionAssignee()


def _default_assignee(
    *,
    mission_id: str,
    node_id: str,
    kind: str,
    metadata: Mapping[str, Any],
    members: Sequence[TeamMissionAssignee],
    leader_node: Mapping[str, Any],
) -> TeamMissionAssignee:
    leader = _leader_member(members) or _assignee_from_node(leader_node)
    if _is_leader_owned_node(kind=kind, metadata=metadata):
        if leader.member_id or leader.profile_id:
            return TeamMissionAssignee(**{**leader.__dict__, "resolved_by": "default_leader"})
        return TeamMissionAssignee(member_id="leader", display_name="Leader", role="leader", resolved_by="default_leader")
    worker = _first_worker_member(members)
    if worker:
        return TeamMissionAssignee(**{**worker.__dict__, "resolved_by": "default_worker"})
    if leader.member_id or leader.profile_id:
        return TeamMissionAssignee(**{**leader.__dict__, "resolved_by": "fallback_leader"})
    return TeamMissionAssignee(member_id="leader", display_name="Leader", role="leader", resolved_by="fallback_leader")


def _find_member(
    members: Sequence[TeamMissionAssignee],
    *,
    member_id: str = "",
    profile_id: str = "",
    profile_version_id: str = "",
    role: str = "",
) -> TeamMissionAssignee | None:
    normalized_member_id = _text(member_id)
    normalized_profile_id = _text(profile_id)
    normalized_version_id = _text(profile_version_id)
    normalized_role = _text(role)
    for member in members:
        if normalized_member_id and member.member_id == normalized_member_id:
            return member
        if normalized_profile_id and member.profile_id == normalized_profile_id:
            if not normalized_version_id or not member.profile_version_id or member.profile_version_id == normalized_version_id:
                return member
        if normalized_role and member.role == normalized_role:
            return member
    return None


def _leader_member(members: Sequence[TeamMissionAssignee]) -> TeamMissionAssignee | None:
    for member in members:
        if member.role in _LEADER_ROLES:
            return member
    return members[0] if members else None


def _first_worker_member(members: Sequence[TeamMissionAssignee]) -> TeamMissionAssignee | None:
    for member in members:
        if member.role not in _LEADER_ROLES:
            return member
    return None


def _assignee_from_node(node: Mapping[str, Any]) -> TeamMissionAssignee:
    if not node:
        return TeamMissionAssignee()
    metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
    return TeamMissionAssignee(
        member_id=_text(metadata.get("assignee_member_id") or metadata.get("assigneeMemberId")),
        profile_id=_text(node.get("assignee_profile_id")),
        profile_version_id=_text(node.get("assignee_profile_version_id")),
        display_name=_text(metadata.get("assignee_display_name") or metadata.get("assigneeDisplayName")),
        role=_text(metadata.get("assignee_role") or metadata.get("assigneeRole") or "leader"),
        resolved_by="leader_node",
    )


def _is_leader_owned_node(*, kind: str, metadata: Mapping[str, Any]) -> bool:
    if is_team_mission_control_node_kind(kind):
        return True
    role = _text(metadata.get("role"))
    phase = _text(metadata.get("phase"))
    return role in _LEADER_ROLES or phase in {"planning", "change_request", "synthesis", "verifying", "approval"}


def _default_runtime_scope(*, mission_id: str, node_id: str, kind: str, assignee: TeamMissionAssignee) -> str:
    del assignee
    if normalize_team_mission_node_kind(kind) in {"root"}:
        return f"team:{mission_id}:leader"
    suffix = (_text(node_id) or kind or "node").replace(":", "_")
    return f"team:{mission_id}:node:{suffix}"
