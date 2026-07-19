from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind


READ_MODEL_SCHEMA_VERSION = 2
READ_MODEL_SOURCE = "team_mission.snapshot.get"

_MISSION_STATUS_ALIASES = {
    "active": "running",
    "canceled": "cancelled",
}
_NODE_STATUS_ALIASES = {
    "blocked_waiting_dependency": "waiting_dependency",
    "canceled": "cancelled",
    "done": "completed",
    "queued": "todo",
    "succeeded": "completed",
}


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _array(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(*values: Any) -> str:
    for value in values:
        normalized = _text(value)
        if normalized:
            return normalized
    return ""


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed == parsed else fallback


def _integer(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _iso_time(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        numeric = float(raw)
    except ValueError:
        return raw
    if numeric <= 0:
        return ""
    seconds = numeric / 1000 if numeric >= 10_000_000_000 else numeric
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_mission_status(value: Any, *, default: str = "draft") -> str:
    raw = _text(value).lower()
    if not raw:
        return default
    return _MISSION_STATUS_ALIASES.get(raw, raw)


def _normalize_node_status(value: Any, *, default: str = "todo") -> str:
    raw = _text(value).lower()
    if not raw:
        return default
    return _NODE_STATUS_ALIASES.get(raw, raw)


def _unique_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _message_page_info(value: Any) -> dict[str, Any]:
    raw = _object(value)
    return {
        "prev_cursor": _first_text(raw.get("prev_cursor"), raw.get("prevCursor"), raw.get("prev_cursor_id")),
        "next_cursor": _first_text(raw.get("next_cursor"), raw.get("nextCursor"), raw.get("next_cursor_id")),
        "has_more_before": bool(raw.get("has_more_before") or raw.get("hasMoreBefore")),
        "has_more_after": bool(raw.get("has_more_after") or raw.get("hasMoreAfter")),
        "total_count": _integer(raw.get("total_count") if "total_count" in raw else raw.get("totalCount"), 0),
    }


def _normalize_team_member(value: Any) -> dict[str, Any]:
    raw = _object(value)
    member_id = _first_text(raw.get("member_id"), raw.get("memberId"), raw.get("id"))
    if not member_id:
        return {}
    profile_name = _first_text(
        raw.get("profile_name"),
        raw.get("profileName"),
        raw.get("agent_profile_name"),
        raw.get("agentProfileName"),
        raw.get("name"),
    )
    profile_avatar = _first_text(
        raw.get("profile_avatar"),
        raw.get("profileAvatar"),
        raw.get("agent_profile_avatar"),
        raw.get("agentProfileAvatar"),
        raw.get("avatar"),
    )
    return {
        "id": member_id,
        "member_id": member_id,
        "team_id": _first_text(raw.get("team_id"), raw.get("teamId")),
        "agent_profile_id": _first_text(raw.get("agent_profile_id"), raw.get("agentProfileId"), raw.get("profile_id"), raw.get("profileId")),
        "agent_profile_version_id": _first_text(raw.get("agent_profile_version_id"), raw.get("agentProfileVersionId")),
        "name": profile_name,
        "avatar": profile_avatar,
        "profile_name": profile_name,
        "profile_avatar": profile_avatar,
        "role": _first_text(raw.get("role")) or "member",
        "capability_tags": [_text(item) for item in _array(raw.get("capability_tags") or raw.get("capabilityTags")) if _text(item)],
        "auto_assignable": raw.get("auto_assignable", raw.get("autoAssignable", True)) is not False,
        "max_concurrent_nodes": max(1, _integer(raw.get("max_concurrent_nodes") or raw.get("maxConcurrentNodes"), 1)),
        "permission_mode": _first_text(raw.get("permission_mode"), raw.get("permissionMode")) or "inherit_profile",
        "status": _first_text(raw.get("status")) or "active",
        "created_at": _iso_time(raw.get("created_at") or raw.get("createdAt")),
        "updated_at": _iso_time(raw.get("updated_at") or raw.get("updatedAt")),
    }


def _normalize_team(value: Any, fallback_members: Any = None) -> dict[str, Any]:
    raw = _object(value)
    team_id = _first_text(raw.get("team_id"), raw.get("teamId"), raw.get("id"))
    if not team_id:
        return {}
    explicit_members = [_member for _member in (_normalize_team_member(item) for item in _array(fallback_members)) if _member]
    projected_members = [_member for _member in (_normalize_team_member(item) for item in _array(raw.get("members"))) if _member]
    members = explicit_members or projected_members
    display_members = [
        _member
        for _member in (
            _normalize_team_member(item)
            for item in _array(raw.get("display_members") or raw.get("displayMembers"))
        )
        if _member
    ]
    leader_member = _normalize_team_member(raw.get("leader_member") or raw.get("leaderMember"))
    member_count = _integer(
        raw.get("member_count") if "member_count" in raw else raw.get("memberCount"),
        len(members) or len(display_members),
    )
    return {
        "id": team_id,
        "team_id": team_id,
        "name": _first_text(raw.get("name"), raw.get("title")) or "团队",
        "avatar": raw.get("avatar"),
        "description": _text(raw.get("description")),
        "lead_agent_profile_id": _first_text(raw.get("lead_agent_profile_id"), raw.get("leadAgentProfileId")),
        "default_mode": _first_text(raw.get("default_mode"), raw.get("defaultMode"), raw.get("mode")) or "supervised_mission",
        "policy": _object(raw.get("policy")),
        "status": _first_text(raw.get("status")) or "active",
        "created_at": _iso_time(raw.get("created_at") or raw.get("createdAt")),
        "updated_at": _iso_time(raw.get("updated_at") or raw.get("updatedAt")),
        "member_count": member_count,
        "projection": _first_text(raw.get("projection"), raw.get("member_projection"), raw.get("memberProjection")),
        "leader_member": leader_member or None,
        "display_members": display_members,
        "members": members,
    }


def _conversation_conversation_session_id(conversation: dict[str, Any], mission: dict[str, Any]) -> str:
    metadata = _object(mission.get("metadata"))
    return _first_text(
        conversation.get("conversation_session_id"),
        conversation.get("conversationSessionId"),
        metadata.get("conversation_session_id"),
        metadata.get("conversationSessionId"),
        metadata.get("conversation_team_session_id"),
        metadata.get("conversationTeamSessionId"),
    )


def _normalize_conversation(conversation: Any, mission: dict[str, Any]) -> dict[str, Any]:
    raw = _object(conversation)
    team_id = _first_text(raw.get("team_id"), raw.get("teamId"), mission.get("team_id"), mission.get("teamId"))
    conversation_session_id = _first_text(
        _conversation_conversation_session_id(raw, mission),
        # Compatibility input for snapshots persisted before schema v2. The
        # legacy aggregate key is never re-emitted by this public read model.
        raw.get("conversation_id"),
        raw.get("conversationId"),
        mission.get("conversation_id"),
        mission.get("conversationId"),
    )
    title = _first_text(raw.get("display_title"), raw.get("displayTitle"), raw.get("title"))
    return {
        "team_id": team_id,
        "conversation_session_id": conversation_session_id,
        "title": title,
        "objective": _first_text(raw.get("objective"), mission.get("objective")),
        "workspace_id": _first_text(raw.get("workspace_id"), raw.get("workspaceId"), mission.get("workspace_id"), mission.get("workspaceId")),
        "workspace_path": _first_text(raw.get("workspace_path"), raw.get("workspacePath"), mission.get("workspace_path"), mission.get("workspacePath")),
        "status": _first_text(raw.get("status")) or "active",
        "active_mission_id": _first_text(raw.get("active_mission_id"), raw.get("activeMissionId")),
        "leader_runtime_context": _object(raw.get("leader_runtime_context") or raw.get("leaderRuntimeContext")) or None,
        "created_by_user_id": _first_text(raw.get("created_by_user_id"), raw.get("createdByUserId")),
        "created_at": _iso_time(raw.get("created_at") or raw.get("createdAt") or mission.get("created_at") or mission.get("createdAt")),
        "updated_at": _iso_time(raw.get("updated_at") or raw.get("updatedAt") or mission.get("updated_at") or mission.get("updatedAt")),
    }


def _normalize_mission(
    mission: Any,
    conversation: Any,
    team: dict[str, Any],
    *,
    activity_id: str,
) -> dict[str, Any]:
    raw = _object(mission)
    normalized_conversation = _normalize_conversation(conversation, raw)
    explicit_mission_id = _first_text(raw.get("mission_id"), raw.get("missionId"), raw.get("id"))
    entity_kind = "mission" if explicit_mission_id else "conversation_shell"
    title = (
        normalized_conversation["title"]
        or _first_text(raw.get("title"))
        or ("团队任务" if explicit_mission_id else "团队会话")
    )
    objective = _first_text(raw.get("objective"), normalized_conversation.get("objective")) or title
    team_id = _first_text(raw.get("team_id"), raw.get("teamId"), normalized_conversation.get("team_id"), team.get("team_id"))
    workspace_id = _first_text(raw.get("workspace_id"), raw.get("workspaceId"), normalized_conversation.get("workspace_id"))
    workspace_path = _first_text(raw.get("workspace_path"), raw.get("workspacePath"), normalized_conversation.get("workspace_path"))
    default_status = "planning" if explicit_mission_id else "draft"
    return {
        "mission_id": explicit_mission_id,
        "entity_kind": entity_kind,
        "activity_id": activity_id,
        "team_id": team_id,
        "conversation_session_id": normalized_conversation["conversation_session_id"],
        "title": title,
        "objective": objective,
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "mode": _first_text(raw.get("mode"), team.get("default_mode")) or "supervised_mission",
        "status": _normalize_mission_status(raw.get("status"), default=default_status),
        "created_by_user_id": _first_text(raw.get("created_by_user_id"), raw.get("createdByUserId"), normalized_conversation.get("created_by_user_id")),
        "created_at": _iso_time(raw.get("created_at") or raw.get("createdAt")) or normalized_conversation["created_at"],
        "updated_at": _iso_time(raw.get("updated_at") or raw.get("updatedAt")) or normalized_conversation["updated_at"],
        "completed_at": _iso_time(raw.get("completed_at") or raw.get("completedAt")),
        "conversation": normalized_conversation,
        "team": team or None,
    }


def _edge_source(edge: Mapping[str, Any]) -> str:
    return _first_text(edge.get("from_node_id"), edge.get("fromNodeId"), edge.get("source"), edge.get("from"))


def _edge_target(edge: Mapping[str, Any]) -> str:
    return _first_text(edge.get("to_node_id"), edge.get("toNodeId"), edge.get("target"), edge.get("to"))


def _depends_on_by_node(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    depends: dict[str, list[str]] = {}
    for edge in edges:
        source = _edge_source(edge)
        target = _edge_target(edge)
        if not source or not target:
            continue
        depends.setdefault(target, []).append(source)
    return depends


def _normalize_runtime(node: dict[str, Any]) -> dict[str, Any]:
    runtime = _object(node.get("runtime"))
    metadata = _object(node.get("metadata"))
    runtime_metadata = _object(runtime.get("metadata"))
    runtime_conversation_session_id = _first_text(
        runtime.get("runtime_conversation_session_id"),
        runtime.get("runtimeConversationSessionId"),
        node.get("runtime_conversation_session_id"),
        node.get("runtimeConversationSessionId"),
        runtime_metadata.get("runtime_conversation_session_id"),
        runtime_metadata.get("runtimeConversationSessionId"),
        metadata.get("runtime_conversation_session_id"),
        metadata.get("runtimeConversationSessionId"),
    )
    mission_id = _first_text(
        node.get("mission_id"),
        node.get("missionId"),
        metadata.get("hermes_mission_id"),
        metadata.get("hermesMissionId"),
        metadata.get("mission_id"),
        metadata.get("missionId"),
    )
    node_id = _first_text(
        metadata.get("hermes_node_id"),
        metadata.get("hermesNodeId"),
        node.get("node_id"),
        node.get("nodeId"),
        node.get("id"),
    )
    task_id = _first_text(
        runtime.get("task_id"),
        runtime.get("taskId"),
        node.get("task_id"),
        node.get("taskId"),
        runtime_metadata.get("task_id"),
        runtime_metadata.get("taskId"),
        metadata.get("task_id"),
        metadata.get("taskId"),
        metadata.get("submitted_task_id"),
        metadata.get("submittedTaskId"),
    )
    runtime_scope_key = _first_text(
        runtime.get("runtime_scope_key"),
        runtime.get("runtimeScopeKey"),
        node.get("runtime_scope_key"),
        node.get("runtimeScopeKey"),
        runtime_metadata.get("runtime_scope_key"),
        runtime_metadata.get("runtimeScopeKey"),
        metadata.get("runtime_scope_key"),
        metadata.get("runtimeScopeKey"),
    )
    run_id = _first_text(
        runtime.get("run_id"),
        runtime.get("runId"),
        node.get("run_id"),
        node.get("runId"),
        runtime_metadata.get("run_id"),
        runtime_metadata.get("runId"),
        metadata.get("run_id"),
        metadata.get("runId"),
    )
    turn_id = _first_text(
        runtime.get("turn_id"),
        runtime.get("turnId"),
        node.get("turn_id"),
        node.get("turnId"),
        runtime_metadata.get("turn_id"),
        runtime_metadata.get("turnId"),
        metadata.get("turn_id"),
        metadata.get("turnId"),
    )
    conversation_session_id = _first_text(
        runtime.get("conversation_session_id"),
        runtime.get("conversationSessionId"),
        node.get("conversation_session_id"),
        node.get("conversationSessionId"),
        runtime_metadata.get("conversation_session_id"),
        runtime_metadata.get("conversationSessionId"),
        metadata.get("conversation_session_id"),
        metadata.get("conversationSessionId"),
        runtime_conversation_session_id,
    )
    actual_conversation_session_id = _first_text(
        runtime_conversation_session_id,
        runtime.get("actual_conversation_session_id"),
        runtime.get("actualConversationSessionId"),
        node.get("actual_conversation_session_id"),
        node.get("actualConversationSessionId"),
        runtime_metadata.get("actual_conversation_session_id"),
        runtime_metadata.get("actualConversationSessionId"),
        metadata.get("actual_conversation_session_id"),
        metadata.get("actualConversationSessionId"),
    )
    conversation_session_id = _first_text(
        runtime_conversation_session_id,
        runtime.get("conversation_session_id"),
        runtime.get("conversationSessionId"),
        node.get("conversation_session_id"),
        node.get("conversationSessionId"),
        node.get("session_id"),
        node.get("sessionId"),
        runtime_metadata.get("conversation_session_id"),
        runtime_metadata.get("conversationSessionId"),
        runtime_metadata.get("session_id"),
        runtime_metadata.get("sessionId"),
        metadata.get("conversation_session_id"),
        metadata.get("conversationSessionId"),
        metadata.get("session_id"),
        metadata.get("sessionId"),
    )
    execution_session_id = _first_text(
        runtime.get("execution_session_id"),
        runtime.get("executionSessionId"),
        node.get("execution_session_id"),
        node.get("executionSessionId"),
        runtime_metadata.get("execution_session_id"),
        runtime_metadata.get("executionSessionId"),
        metadata.get("execution_session_id"),
        metadata.get("executionSessionId"),
    )
    return {
        "source": "hermes-team-mission",
        "mission_id": mission_id,
        "node_id": node_id,
        "node_kind": normalize_team_mission_node_kind(node.get("kind")),
        "node_status": _normalize_node_status(node.get("status")),
        "task_id": task_id,
        "runtime_scope_key": runtime_scope_key,
        "run_id": run_id,
        "turn_id": turn_id,
        "conversation_session_id": conversation_session_id,
        "actual_conversation_session_id": actual_conversation_session_id,
        "conversation_session_id": conversation_session_id,
        "runtime_conversation_session_id": runtime_conversation_session_id,
        "execution_session_id": execution_session_id,
        "metadata": metadata,
    }


def _member_lookup(team: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_member_id: dict[str, dict[str, Any]] = {}
    by_profile_id: dict[str, dict[str, Any]] = {}
    for member in _array(team.get("members")):
        if not isinstance(member, dict):
            continue
        member_id = _text(member.get("member_id") or member.get("id"))
        profile_id = _text(member.get("agent_profile_id"))
        if member_id:
            by_member_id[member_id] = member
        if profile_id:
            by_profile_id[profile_id] = member
    return by_member_id, by_profile_id


def _member_for_node(node: dict[str, Any], team: dict[str, Any]) -> dict[str, Any]:
    metadata = _object(node.get("metadata"))
    member_id = _first_text(
        node.get("assignee_member_id"),
        node.get("assigneeMemberId"),
        metadata.get("assignee_member_id"),
        metadata.get("assigneeMemberId"),
    )
    by_member_id, by_profile_id = _member_lookup(team)
    if member_id and member_id in by_member_id:
        return by_member_id[member_id]
    profile_id = _first_text(
        node.get("assignee_profile_id"),
        node.get("assigneeProfileId"),
        node.get("agent_profile_id"),
        node.get("agentProfileId"),
        node.get("profile_id"),
        node.get("profileId"),
        metadata.get("assignee_profile_id"),
        metadata.get("assigneeProfileId"),
        metadata.get("agent_profile_id"),
        metadata.get("agentProfileId"),
        metadata.get("profile_id"),
        metadata.get("profileId"),
    )
    return by_profile_id.get(profile_id, {}) if profile_id else {}


def _normalize_node(
    node: Any,
    *,
    index: int,
    mission: dict[str, Any],
    team: dict[str, Any],
    depends_by_node: dict[str, list[str]],
) -> dict[str, Any]:
    raw = _object(node)
    node_id = _first_text(raw.get("node_id"), raw.get("nodeId"), raw.get("id"))
    if not node_id:
        return {}
    metadata = _object(raw.get("metadata"))
    member = _member_for_node(raw, team)
    position = _object(raw.get("position"))
    depends_on = _unique_text([
        *depends_by_node.get(node_id, []),
        *_array(raw.get("depends_on") or raw.get("dependsOn")),
    ])
    title = _first_text(raw.get("title"), mission.get("title")) or "团队任务"
    objective = _first_text(raw.get("objective"), raw.get("body"), mission.get("objective"), title) or "团队任务"
    node_mission_id = _first_text(
        raw.get("mission_id"),
        raw.get("missionId"),
        metadata.get("hermes_mission_id"),
        mission.get("mission_id"),
    )
    mission_run_id = _first_text(
        raw.get("mission_run_id"),
        raw.get("missionRunId"),
        metadata.get("mission_run_id"),
        metadata.get("missionRunId"),
    )
    assignee_member_id = _first_text(
        member.get("member_id"),
        member.get("id"),
        raw.get("assignee_member_id"),
        raw.get("assigneeMemberId"),
        metadata.get("assignee_member_id"),
        metadata.get("assigneeMemberId"),
    )
    agent_profile_id = _first_text(
        member.get("agent_profile_id"),
        raw.get("assignee_profile_id"),
        raw.get("assigneeProfileId"),
        raw.get("agent_profile_id"),
        raw.get("agentProfileId"),
        raw.get("profile_id"),
        raw.get("profileId"),
        metadata.get("assignee_profile_id"),
        metadata.get("agent_profile_id"),
        metadata.get("profile_id"),
    )
    position_x = raw.get("position_x") if "position_x" in raw else raw.get("x", position.get("x", 0))
    position_y = raw.get("position_y") if "position_y" in raw else raw.get("y", position.get("y", index * 180))
    return {
        "node_id": node_id,
        "mission_id": node_mission_id,
        "mission_run_id": mission_run_id,
        "kind": normalize_team_mission_node_kind(raw.get("kind")),
        "title": title,
        "objective": objective,
        "status": _normalize_node_status(raw.get("status")),
        "assignee_member_id": assignee_member_id,
        "agent_profile_id": agent_profile_id,
        "depends_on": depends_on,
        "output_contract": _object(raw.get("output_contract") or raw.get("outputContract")),
        "artifact_links": _array(raw.get("artifact_links") or raw.get("artifactLinks")),
        "runtime": _normalize_runtime(raw),
        "position": {
            "x": _number(position_x),
            "y": _number(position_y),
        },
        "created_at": _iso_time(raw.get("created_at") or raw.get("createdAt")),
        "updated_at": _iso_time(raw.get("updated_at") or raw.get("updatedAt")),
    }


def _normalize_edge(edge: Any) -> dict[str, Any]:
    raw = _object(edge)
    from_node_id = _edge_source(raw)
    to_node_id = _edge_target(raw)
    if not from_node_id or not to_node_id:
        return {}
    return {
        "edge_id": _text(raw.get("edge_id") or raw.get("edgeId")),
        "mission_id": _first_text(raw.get("mission_id"), raw.get("missionId")),
        "from_node_id": from_node_id,
        "to_node_id": to_node_id,
        "kind": _first_text(raw.get("kind")) or "depends_on",
        "metadata": _object(raw.get("metadata")),
        "created_at": _iso_time(raw.get("created_at") or raw.get("createdAt")),
    }


def _normalize_task_frame(value: Any) -> dict[str, Any]:
    raw = _object(value)
    frame_id = _first_text(raw.get("id"), raw.get("frame_id"), raw.get("frameId"))
    mission_id = _first_text(raw.get("mission_id"), raw.get("missionId"))
    if not frame_id and not mission_id:
        return {}
    return {
        "id": frame_id,
        "run_id": _first_text(raw.get("run_id"), raw.get("runId")),
        "conversation_session_id": _first_text(
            raw.get("conversation_session_id"),
            raw.get("conversationSessionId"),
            raw.get("conversation_id"),
            raw.get("conversationId"),
        ),
        "mission_id": mission_id,
        "task_id": _first_text(raw.get("task_id"), raw.get("taskId")),
        "title": _text(raw.get("title")),
        "objective": _text(raw.get("objective")),
        "status": _normalize_mission_status(raw.get("status"), default="planning"),
        "source": _text(raw.get("source")),
        "root_node_id": _first_text(raw.get("root_node_id"), raw.get("rootNodeId")),
        "node_ids": _unique_text(_array(raw.get("node_ids") or raw.get("nodeIds"))),
        "created_at": _iso_time(raw.get("created_at") or raw.get("createdAt")),
        "updated_at": _iso_time(raw.get("updated_at") or raw.get("updatedAt")),
        "completed_at": _iso_time(raw.get("completed_at") or raw.get("completedAt")),
        "artifact_refs": _array(raw.get("artifact_refs") or raw.get("artifactRefs")),
        "final_deliverable": _object(raw.get("final_deliverable") or raw.get("finalDeliverable")) or None,
        "deliverable_message_id": _first_text(raw.get("deliverable_message_id"), raw.get("deliverableMessageId")),
    }


def build_team_mission_read_model(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the canonical Team Mission read model returned by snapshot RPCs."""
    snapshot_payload = _object(snapshot)
    graph = _object(snapshot_payload.get("graph"))
    mission_raw = _object(snapshot_payload.get("mission")) or _object(graph.get("mission"))
    conversation_raw = _object(snapshot_payload.get("conversation")) or _object(graph.get("conversation"))
    team = _normalize_team(snapshot_payload.get("team") or graph.get("team"))
    snapshot_mission_id = _first_text(
        snapshot_payload.get("mission_id"),
        snapshot_payload.get("missionId"),
        graph.get("snapshot_mission_id"),
        graph.get("snapshotMissionId"),
        mission_raw.get("mission_id"),
        mission_raw.get("missionId"),
    )
    activity_id = _first_text(
        snapshot_payload.get("activity_id"),
        snapshot_payload.get("activityId"),
        graph.get("activity_id"),
        graph.get("activityId"),
    )
    snapshot_version = _first_text(
        snapshot_payload.get("snapshot_version"),
        snapshot_payload.get("snapshotVersion"),
        graph.get("snapshot_version"),
        graph.get("snapshotVersion"),
    )
    last_event_seq = _integer(
        snapshot_payload.get("last_event_seq")
        if "last_event_seq" in snapshot_payload
        else snapshot_payload.get("lastEventSeq", graph.get("last_event_seq", graph.get("lastEventSeq", 0))),
        0,
    )
    mission = _normalize_mission(
        {
            **mission_raw,
            **({"mission_id": snapshot_mission_id} if snapshot_mission_id and not mission_raw.get("mission_id") else {}),
        },
        conversation_raw,
        team,
        activity_id=activity_id,
    )
    raw_edges = [edge for edge in _array(graph.get("edges") or snapshot_payload.get("edges")) if isinstance(edge, Mapping)]
    normalized_edges = [edge for edge in (_normalize_edge(edge) for edge in raw_edges) if edge]
    depends_by_node = _depends_on_by_node(raw_edges)
    raw_nodes = [node for node in _array(graph.get("nodes") or snapshot_payload.get("nodes")) if isinstance(node, Mapping)]
    normalized_nodes = [
        node
        for node in (
            _normalize_node(raw_node, index=index, mission=mission, team=team, depends_by_node=depends_by_node)
            for index, raw_node in enumerate(raw_nodes)
        )
        if node
    ]
    task_frames = [
        frame
        for frame in (
            _normalize_task_frame(item)
            for item in _array(graph.get("task_frames") or graph.get("taskFrames"))
        )
        if frame
    ]
    runs = [frame for frame in (_normalize_task_frame(item) for item in _array(graph.get("runs"))) if frame]
    page_info = (
        graph.get("message_page_info")
        or graph.get("messagePageInfo")
        or graph.get("page_info")
        or graph.get("pageInfo")
    )
    return {
        "schema_version": READ_MODEL_SCHEMA_VERSION,
        "source": READ_MODEL_SOURCE,
        "snapshot_version": snapshot_version,
        "snapshot_mission_id": snapshot_mission_id,
        "activity_id": activity_id,
        "last_event_seq": last_event_seq,
        "conversation": mission["conversation"],
        "team": team or None,
        "result": _object(snapshot_payload.get("result") or graph.get("result")) or None,
        "mission": mission,
        "nodes": normalized_nodes,
        "edges": normalized_edges,
        "task_frames": task_frames,
        "runs": runs,
        "events": _array(graph.get("events")),
        "recent_messages": _array(graph.get("recent_messages") or graph.get("recentMessages") or graph.get("messages")),
        "message_page_info": _message_page_info(page_info),
    }
