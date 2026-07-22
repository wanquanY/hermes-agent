"""Project Team Mission state into canonical Conversation Activity entities.

Team Mission owns graph persistence.  Conversation clients must not read that
storage model directly, so this module is the single translation boundary used
by both render snapshots and live ``activity.upserted`` events.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hermes_agent.domain.participants import leader_participant_id


_TERMINAL_STATUSES = {"cancelled", "canceled", "completed", "failed", "interrupted"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def mission_node_activity_id(mission_id: str, node: Mapping[str, Any]) -> str:
    """Return the stable Conversation Activity identity for a graph node.

    The root planning node is the Mission Activity itself.  This avoids a
    duplicate visual root and gives its runtime the same owner as the frame.
    Every other graph node is a child Activity with an identity that is stable
    across retries of that node.
    """

    stable_mission_id = _text(mission_id)
    node_id = _text(node.get("node_id") or node.get("nodeId") or node.get("id"))
    if not stable_mission_id:
        return ""
    if _text(node.get("kind")).lower() == "root":
        return f"mission:{stable_mission_id}"
    return f"act-node:{stable_mission_id}:{node_id}" if node_id else ""


def _activity_status(value: Any) -> str:
    status = _text(value).lower()
    if status in {
        "todo",
        "ready",
        "pending",
        "planning",
        "queued",
        "created",
        "waiting_dependency",
        "blocked_waiting_dependency",
    }:
        return "pending"
    if status in {"waiting_approval", "blocked", "partial", "cancelling"}:
        return status
    if status in _TERMINAL_STATUSES:
        return "cancelled" if status == "canceled" else status
    if status in {"starting", "active", "running", "review", "verifying"}:
        return "running"
    # Unknown owner states must never make an idle graph look executable.
    # Keeping them pending is forward-compatible and lets a later canonical
    # state advance the Activity monotonically.
    return "pending"


def _mission_activity_status(value: Any) -> str:
    """Translate Mission lifecycle state to the visible root Activity state.

    A graph-level ``ready`` state means approval/dependencies have cleared and
    the scheduler may execute work. Node-level ``ready`` is still pending, but
    the owning Mission Activity has already started and must resume as running
    instead of regressing from waiting_approval back to pending.
    """

    status = _text(value).lower()
    if status == "ready":
        return "running"
    return _activity_status(status)


def _conversation_session_id(graph: Mapping[str, Any]) -> str:
    mission = _mapping(graph.get("mission"))
    conversation = _mapping(graph.get("conversation"))
    metadata = _mapping(mission.get("metadata"))
    return _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
        or mission.get("leader_session_id")
        or mission.get("leaderSessionId")
        or metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("conversationTeamSessionId")
    )


def _participants(db: Any, conversation_session_id: str) -> list[dict[str, Any]]:
    if not conversation_session_id:
        return []
    try:
        values = db.participants.list_conversation_participants(conversation_session_id)
    except Exception:
        return []
    return [dict(item) for item in values or [] if isinstance(item, Mapping)]


def _leader_owner(
    participants: list[dict[str, Any]],
    *,
    conversation_id: str,
    conversation_session_id: str,
) -> str:
    candidates = []
    for subject in (conversation_id, conversation_session_id):
        if not subject:
            continue
        try:
            candidates.append(leader_participant_id(subject))
        except ValueError:
            pass
    ids = {_text(item.get("participant_id")) for item in participants}
    for candidate in candidates:
        if candidate in ids:
            return candidate
    return next(
        (
            _text(item.get("participant_id"))
            for item in participants
            if _text(item.get("role")).lower() == "leader"
        ),
        candidates[0] if candidates else "",
    )


def _node_owner(
    node: Mapping[str, Any],
    participants: list[dict[str, Any]],
    *,
    leader_owner: str,
) -> str:
    metadata = _mapping(node.get("metadata"))
    role = _text(metadata.get("role") or metadata.get("assignee_role")).lower()
    if _text(node.get("kind")).lower() == "root" or role in {"lead", "leader", "root"}:
        return leader_owner
    explicit = _text(
        node.get("participant_id")
        or node.get("participantId")
        or metadata.get("participant_id")
        or metadata.get("participantId")
    )
    if explicit:
        return explicit
    member_id = _text(
        node.get("member_id")
        or node.get("memberId")
        or metadata.get("member_id")
        or metadata.get("memberId")
        or metadata.get("assignee_member_id")
        or metadata.get("assigneeMemberId")
    )
    profile_id = _text(
        node.get("assignee_profile_id")
        or node.get("assigneeProfileId")
        or metadata.get("agent_profile_id")
        or metadata.get("agentProfileId")
    )
    for participant in participants:
        if member_id and _text(participant.get("member_id")) == member_id:
            return _text(participant.get("participant_id"))
        if profile_id and _text(participant.get("agent_profile_id")) == profile_id:
            return _text(participant.get("participant_id"))
    return ""


def _dependency_activity_ids(
    graph: Mapping[str, Any],
    *,
    mission_id: str,
) -> dict[str, list[str]]:
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, Mapping)]
    by_node_id = {
        _text(node.get("node_id") or node.get("nodeId") or node.get("id")): node
        for node in nodes
    }
    result: dict[str, list[str]] = {}
    for raw_edge in graph.get("edges", []):
        if not isinstance(raw_edge, Mapping):
            continue
        source_id = _text(
            raw_edge.get("from_node_id")
            or raw_edge.get("fromNodeId")
            or raw_edge.get("source")
        )
        target_id = _text(
            raw_edge.get("to_node_id")
            or raw_edge.get("toNodeId")
            or raw_edge.get("target")
        )
        source = by_node_id.get(source_id)
        if not source or not target_id:
            continue
        activity_id = mission_node_activity_id(mission_id, source)
        if activity_id:
            result.setdefault(target_id, []).append(activity_id)
    return result


def project_team_mission_activities(
    db: Any,
    mission_id: str,
    *,
    base_activities: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the Activity entities for one durable Team Mission graph."""

    stable_mission_id = _text(mission_id)
    if not stable_mission_id:
        return []
    graph = db.team_mission_graphs.get_team_mission_graph(stable_mission_id)
    if not isinstance(graph, Mapping) or not isinstance(graph.get("mission"), Mapping):
        return []
    mission = _mapping(graph.get("mission"))
    conversation_id = _text(
        mission.get("conversation_id") or mission.get("conversationId")
    )
    conversation_session_id = _conversation_session_id(graph)
    if not conversation_session_id:
        return []
    participants = _participants(db, conversation_session_id)
    leader_owner = _leader_owner(
        participants,
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
    )
    stored_by_id = {
        _text(activity_id): dict(activity)
        for activity_id, activity in (base_activities or {}).items()
        if isinstance(activity, Mapping)
    }
    if not stored_by_id:
        try:
            stored_by_id = {
                _text(item.get("activity_id")): dict(item)
                for item in db.activities.list(conversation_session_id) or []
                if isinstance(item, Mapping) and _text(item.get("activity_id"))
            }
        except Exception:
            stored_by_id = {}
    root_id = f"mission:{stable_mission_id}"
    root = dict(stored_by_id.get(root_id) or {})
    if not root:
        try:
            candidate = db.activities.get_for_mission(stable_mission_id)
        except Exception:
            candidate = None
        if isinstance(candidate, Mapping):
            root = dict(candidate)
            root_id = _text(root.get("activity_id")) or root_id
    root_node = next(
        (
            node
            for node in graph.get("nodes", [])
            if isinstance(node, Mapping) and _text(node.get("kind")).lower() == "root"
        ),
        {},
    )
    root_status = _mission_activity_status(
        mission.get("status") or root_node.get("status")
    )
    root_entity = {
        **root,
        "activity_id": root_id,
        "conversation_id": conversation_session_id,
        "kind": "mission",
        "status": root_status,
        "title": _text(mission.get("title") or root_node.get("title"))
        or stable_mission_id,
        "objective": _text(mission.get("objective") or root_node.get("objective")),
        "owner_participant_id": leader_owner,
        "target_mission_id": stable_mission_id,
        "mission_id": stable_mission_id,
        "team_id": _text(mission.get("team_id") or mission.get("teamId")),
        "node_kind": "root",
        "graph_node_id": _text(root_node.get("node_id") or root_node.get("nodeId")),
        "created_at": mission.get("created_at") or root.get("created_at") or 0,
        "updated_at": mission.get("updated_at") or root.get("updated_at") or 0,
    }
    entities = [root_entity]
    dependencies = _dependency_activity_ids(graph, mission_id=stable_mission_id)
    for raw_node in graph.get("nodes", []):
        if (
            not isinstance(raw_node, Mapping)
            or _text(raw_node.get("kind")).lower() == "root"
        ):
            continue
        node = dict(raw_node)
        node_id = _text(node.get("node_id") or node.get("nodeId") or node.get("id"))
        activity_id = mission_node_activity_id(stable_mission_id, node)
        if not node_id or not activity_id:
            continue
        stored = dict(stored_by_id.get(activity_id) or {})
        metadata = _mapping(node.get("metadata"))
        entities.append({
            **stored,
            **metadata,
            "activity_id": activity_id,
            "conversation_id": conversation_session_id,
            "parent_activity_id": root_id,
            "kind": "agent_dispatch",
            "status": _activity_status(node.get("status")),
            "title": _text(node.get("title") or node.get("objective")) or node_id,
            "objective": _text(node.get("objective")),
            "owner_participant_id": _node_owner(
                node,
                participants,
                leader_owner=leader_owner,
            ),
            "target_profile_id": _text(
                node.get("assignee_profile_id") or node.get("assigneeProfileId")
            ),
            "target_mission_id": stable_mission_id,
            "mission_id": stable_mission_id,
            "graph_node_id": node_id,
            "node_kind": _text(node.get("kind")) or "worker",
            "dependency_activity_ids": dependencies.get(node_id, []),
            "created_at": node.get("created_at") or stored.get("created_at") or 0,
            "updated_at": node.get("updated_at") or stored.get("updated_at") or 0,
        })
    return entities


def project_conversation_activities(
    db: Any, conversation_session_id: str
) -> list[dict[str, Any]]:
    """Return the complete Conversation Activity projection for snapshots."""

    stable_session_id = _text(conversation_session_id)
    if not stable_session_id:
        return []
    base = [
        dict(item)
        for item in db.activities.list(stable_session_id) or []
        if isinstance(item, Mapping)
    ]
    by_id = {
        _text(item.get("activity_id")): item
        for item in base
        if _text(item.get("activity_id"))
    }
    mission_ids = []
    for item in base:
        mission_id = _text(item.get("target_mission_id"))
        if mission_id and mission_id not in mission_ids:
            mission_ids.append(mission_id)
    try:
        resolved = db.resolve_team_mission_conversation(stable_session_id)
    except Exception:
        resolved = {}
    mission = resolved.get("mission") if isinstance(resolved, Mapping) else None
    active_mission_id = _text(
        mission.get("mission_id") if isinstance(mission, Mapping) else ""
    )
    if active_mission_id and active_mission_id not in mission_ids:
        mission_ids.append(active_mission_id)
    for mission_id in mission_ids:
        for entity in project_team_mission_activities(
            db,
            mission_id,
            base_activities=by_id,
        ):
            by_id[_text(entity.get("activity_id"))] = entity
    return sorted(
        by_id.values(),
        key=lambda item: (
            float(item.get("created_at") or 0),
            _text(item.get("activity_id")),
        ),
    )


__all__ = [
    "mission_node_activity_id",
    "project_conversation_activities",
    "project_team_mission_activities",
]
