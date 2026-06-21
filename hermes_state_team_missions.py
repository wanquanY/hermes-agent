from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from hermes_team_mission_memory_utils import MEMORY_COMMITTED_STATUS as _MEMORY_COMMITTED_STATUS
from hermes_team_mission_memory_utils import stable_id as _stable_id
from hermes_team_mission_memory_utils import text as _text
from hermes_team_mission_conversation_utils import mirror_event_to_conversation as _mirror_team_mission_event
from hermes_runtime_event_payloads import primary_deliverable_text
from hermes_team_mission_conversation_state import delete_team_mission_conversation as _delete_team_mission_conversation
from hermes_team_mission_conversation_state import is_placeholder_team_mission_conversation_title as _is_placeholder_team_mission_conversation_title
from hermes_team_mission_conversation_state import is_replaceable_team_mission_conversation_title as _is_replaceable_team_mission_conversation_title
from hermes_team_mission_conversation_state import is_routeable_team_mission_conversation as _conversation_routeable
from hermes_team_mission_conversation_state import team_mission_conversation_message_page as _conversation_message_page
from hermes_team_mission_conversation_state import rename_team_mission_conversation as _rename_team_mission_conversation
from hermes_team_mission_conversation_state import team_mission_conversation_history_sql as _conversation_history_sql
from hermes_team_mission_conversation_projection import dedupe_artifact_refs as _dedupe_artifact_refs
from hermes_team_mission_conversation_projection import final_deliverable_for_frame as _final_deliverable_for_frame
from hermes_team_mission_conversation_projection import final_deliverable_from_message as _final_deliverable_from_message
from hermes_team_mission_conversation_projection import final_deliverable_with_artifact_refs as _final_deliverable_with_artifact_refs
from hermes_team_mission_conversation_projection import message_summary_from_message as _message_summary_from_message
from hermes_team_mission_conversation_projection import message_with_deliverable_artifact_refs as _message_with_deliverable_artifact_refs
import hermes_team_mission_memory_state as _memory_state
import hermes_team_mission_graph_state as _graph_state
import hermes_team_mission_event_log as _event_log
from hermes_team_mission_assignees import assignee_public_fields as _assignee_public_fields
from hermes_team_mission_assignees import mission_metadata_with_members as _mission_metadata_with_members
from hermes_team_mission_assignees import resolve_node_assignee as _resolve_node_assignee
from hermes_team_mission_node_kinds import TEAM_MISSION_CONTROL_NODE_KINDS
from hermes_team_mission_node_kinds import metadata_with_normalized_node_kind as _metadata_with_normalized_node_kind
from hermes_team_mission_node_kinds import normalize_team_mission_node_kind as _normalize_node_kind
from hermes_team_mission_modes import TeamMissionEdgeSpec
from hermes_team_mission_modes import TeamMissionNodeSpec
from hermes_team_mission_modes import TeamMissionStrategyActions
from hermes_team_mission_modes import strategy_for_mode


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


_DEPENDENCY_EDGE_KINDS = {"depends_on", "dependency", "blocks", "delegates"}
_DEPENDENCY_SATISFIED_STATUSES = {"completed", "verified"}
_STARTABLE_NODE_STATUSES = {"ready"}
_WAITING_DEPENDENCY_STATUSES = {"todo", "waiting_dependency", "blocked_waiting_dependency"}
_ACTIVE_NODE_STATUSES = {"running", "starting", "waiting_approval"}
_CANCELLABLE_NODE_STATUSES = {
    "todo",
    "ready",
    "waiting_dependency",
    "blocked_waiting_dependency",
    "waiting_approval",
    "starting",
    "running",
    "blocked",
}
_TERMINAL_NODE_STATUSES = {"completed", "verified", "failed", "cancelled", "canceled", "interrupted"}
_TERMINAL_NODE_STATUS_RANK = {
    "cancelled": 1,
    "canceled": 1,
    "interrupted": 1,
    "failed": 1,
    "completed": 2,
    "verified": 2,
}
_ACTIVE_RUN_STATUSES = {
    "queued",
    "starting",
    "running",
    "waiting_approval",
    "cancelling",
    "finalizing",
}
# Any run status that is NOT in this set is treated as still-live by the cancel
# reaper, so unexpected/intermediate statuses can never survive a mission cancel
# as zombie "running" runs in the control-plane DB.
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
# High-volume per-token stream deltas pruned from team_mission_events once a
# mission is terminal (final text is preserved in the kept message.complete rows;
# message.start / tool.start / tool.complete / structural / approval events stay).
_TEAM_MISSION_PRUNABLE_SOURCE_TYPES = (
    "message.delta",
    "reasoning.delta",
    "thinking.delta",
    "tool.progress",
    "tool.generating",
)
_TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
_EXECUTION_MODES_REQUIRE_FINALIZERS = {"supervised_mission", "autonomous_mission", "manual_graph"}
_NON_WORK_NODE_KINDS = TEAM_MISSION_CONTROL_NODE_KINDS
_TEAM_MISSION_RUNTIME_EVENT_TYPE = "team_mission.runtime.event"
_TEAM_MISSION_CONVERSATION_STATUS_EVENT_TYPE = "team_mission.conversation.status"
_TEAM_MISSION_CONVERSATION_STATUS_SOURCE_EVENT_TYPES = {
    "message.start",
    "message.complete",
    "error",
    "session.interrupted",
    "session.recalled",
    "mission.strategy.actions",
    "mission.approval.requested",
    "mission.plan.rejected",
    "mission.node.created",
    "mission.node.updated",
    "mission.node.started",
    "mission.node.run.bound",
    "mission.edge.created",
}
_RUNNING_MISSION_STATUSES = {
    "planning",
    "waiting_approval",
    "running",
    "partially_blocked",
    "blocked",
    "verifying",
}


def _event_seq(event: Dict[str, Any] | None) -> int:
    event = event if isinstance(event, dict) else {}
    for key in ("source_seq", "seq"):
        try:
            value = int(event.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _should_emit_conversation_status_projection(event: Dict[str, Any] | None) -> bool:
    event_type = _text((event or {}).get("type"))
    return event_type in _TEAM_MISSION_CONVERSATION_STATUS_SOURCE_EVENT_TYPES


def _payload_text_value(payload: Dict[str, Any] | None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    for key in ("delta", "text", "output", "content", "final_response", "finalResponse", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    message = payload.get("message")
    if isinstance(message, dict):
        for key in ("content", "text", "output"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _event_has_deliverable_text(event_type: str, payload: Dict[str, Any] | None) -> bool:
    event_type = _text(event_type)
    if event_type not in {"message.delta", "message.complete", "subagent.output_delta"}:
        return False
    payload = payload if isinstance(payload, dict) else {}
    if event_type == "message.complete" and _text(payload.get("status")).lower() in {"error", "failed"}:
        return bool(primary_deliverable_text(payload))
    return bool(_payload_text_value(payload))


def _prefer_terminal_node_status(existing_status: str, next_status: str) -> str:
    existing = str(existing_status or "").strip().lower()
    incoming = str(next_status or "").strip().lower()
    if existing not in _TERMINAL_NODE_STATUSES or incoming not in _TERMINAL_NODE_STATUSES:
        return incoming or existing
    existing_rank = _TERMINAL_NODE_STATUS_RANK.get(existing, 0)
    incoming_rank = _TERMINAL_NODE_STATUS_RANK.get(incoming, 0)
    if incoming_rank > existing_rank:
        return incoming
    return existing


def _conversation_id_from_metadata(metadata: Dict[str, Any] | None, fallback: str = "") -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    return _text(
        metadata.get("conversation_id")
        or metadata.get("conversationId")
        or metadata.get("team_conversation_id")
        or metadata.get("teamConversationId")
        or fallback
    )


def _stable_session_id_from_metadata(metadata: Dict[str, Any] | None, fallback: str = "") -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    return _text(
        metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("stable_session_id")
        or metadata.get("stableSessionId")
        or metadata.get("stable_team_session_id")
        or metadata.get("stableTeamSessionId")
        or metadata.get("team_session_id")
        or metadata.get("teamSessionId")
        or fallback
    )


def _conversation_status(value: str | None) -> str:
    normalized = _text(value).lower()
    if normalized in {"archived", "deleted"}:
        return normalized
    return "active"


def _node_spec_from_graph_node(node: Dict[str, Any]) -> TeamMissionNodeSpec:
    return TeamMissionNodeSpec(
        node_id=str(node.get("node_id") or ""),
        kind=_normalize_node_kind(node.get("kind")),
        title=str(node.get("title") or ""),
        objective=str(node.get("objective") or ""),
        status=str(node.get("status") or "todo"),
        assignee_profile_id=str(node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=str(node.get("runtime_scope_key") or ""),
        output_contract=dict(node.get("output_contract") or {}),
        metadata=dict(node.get("metadata") or {}),
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )


def _edge_spec_from_graph_edge(edge: Dict[str, Any]) -> TeamMissionEdgeSpec:
    return TeamMissionEdgeSpec(
        edge_id=str(edge.get("edge_id") or ""),
        from_node_id=str(edge.get("from_node_id") or ""),
        to_node_id=str(edge.get("to_node_id") or ""),
        kind=str(edge.get("kind") or "depends_on"),
        metadata=dict(edge.get("metadata") or {}),
    )


def _task_id_from_metadata(metadata: Dict[str, Any] | None) -> str:
    if not isinstance(metadata, dict):
        return ""
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), dict) else {}
    return _text(
        metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
        or metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
    )


def _task_id_from_node_and_binding(node: Dict[str, Any] | None, binding: Dict[str, Any] | None = None) -> str:
    node_metadata = node.get("metadata") if isinstance(node, dict) else {}
    binding_metadata = binding.get("metadata") if isinstance(binding, dict) else {}
    return _task_id_from_metadata(node_metadata) or _task_id_from_metadata(binding_metadata)


def _conversation_graph_node_id(mission_id: str, node_id: str) -> str:
    mission_id = _text(mission_id)
    node_id = _text(node_id)
    if not node_id:
        return ""
    if mission_id and (
        node_id.startswith(f"{mission_id}:")
        or node_id.startswith(f"team-mission:{mission_id}:")
    ):
        return node_id
    return f"{mission_id}:{node_id}" if mission_id else node_id


def _task_id_from_mission(mission: Dict[str, Any] | None) -> str:
    if not isinstance(mission, dict):
        return ""
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    return _task_id_from_metadata(metadata) or _text(mission.get("mission_id"))


def _team_mission_runtime_event_identity(
    *,
    mission: Dict[str, Any] | None,
    node: Dict[str, Any] | None,
    binding: Dict[str, Any] | None,
) -> Dict[str, str]:
    mission = mission if isinstance(mission, dict) else {}
    node = node if isinstance(node, dict) else {}
    binding = binding if isinstance(binding, dict) else {}
    mission_id = _text(mission.get("mission_id") or binding.get("mission_id") or node.get("mission_id"))
    mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    node_id = _text(node.get("node_id") or binding.get("node_id"))
    node_kind = _normalize_node_kind(node.get("kind"))
    output_contract = node.get("output_contract") if isinstance(node.get("output_contract"), dict) else {}
    output_contract_format = _text(output_contract.get("format"))
    runtime_stable_session_id = _text(
        binding.get("session_id")
        or node.get("runtime_stable_session_id")
        or node.get("stored_session_id")
        or node.get("actual_stable_session_id")
    )
    runtime_session_id = _text(binding.get("runtime_session_id") or node.get("runtime_session_id"))
    runtime_scope_key = _text(binding.get("runtime_scope_key") or node.get("runtime_scope_key"))
    task_id = (
        _task_id_from_node_and_binding(node, binding)
        or _task_id_from_mission(mission)
        or mission_id
    )
    conversation_id = _conversation_id_from_metadata(
        mission_metadata,
        _text(mission.get("conversation_id")),
    )
    stable_session_id = _stable_session_id_from_metadata(
        mission_metadata,
        _text(mission.get("leader_session_id") or mission.get("team_id") or mission_id),
    )
    return {
        "mission_id": mission_id,
        "missionId": mission_id,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "stable_session_id": stable_session_id,
        "stableSessionId": stable_session_id,
        "node_id": node_id,
        "nodeId": node_id,
        "node_kind": node_kind,
        "nodeKind": node_kind,
        "output_contract_format": output_contract_format,
        "outputContractFormat": output_contract_format,
        "runtime_stable_session_id": runtime_stable_session_id,
        "runtimeStableSessionId": runtime_stable_session_id,
        "runtime_session_id": runtime_session_id,
        "runtimeSessionId": runtime_session_id,
        "runtime_scope_key": runtime_scope_key,
        "runtimeScopeKey": runtime_scope_key,
        "task_id": task_id,
        "taskId": task_id,
        "task_frame_id": f"mission-frame:{mission_id}" if mission_id else "",
        "taskFrameId": f"mission-frame:{mission_id}" if mission_id else "",
    }


def _event_payload_declares_business_subject(event_type: str, payload: Dict[str, Any]) -> bool:
    event_type = _text(event_type)
    payload = payload if isinstance(payload, dict) else {}
    if event_type.startswith("mission.node.") and (
        isinstance(payload.get("node"), dict) or isinstance(payload.get("nodes"), list)
    ):
        return True
    if event_type == "mission.edge.created" and (
        isinstance(payload.get("edge"), dict) or isinstance(payload.get("edges"), list)
    ):
        return True
    if event_type == "mission.approval.requested" and _text(
        payload.get("approval_id") or payload.get("approvalId") or payload.get("id")
    ):
        return True
    if event_type == "mission.strategy.actions" and (
        isinstance(payload.get("nodes"), list)
        or isinstance(payload.get("edges"), list)
        or isinstance(payload.get("approval_requests"), list)
        or isinstance(payload.get("approvalRequests"), list)
    ):
        return True
    return False


def _runtime_event_with_team_mission_identity(
    event: Dict[str, Any],
    identity: Dict[str, str],
    *,
    source_seq: int = 0,
    mission_event_seq: int = 0,
) -> Dict[str, Any]:
    frame = dict(event or {})
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    payload = dict(payload)
    payload_declares_subject = _event_payload_declares_business_subject(_text(frame.get("type")), payload)
    for key, value in identity.items():
        if payload_declares_subject and key in {"node_id", "nodeId"}:
            continue
        if _text(value) and not _text(payload.get(key)):
            payload[key] = value
    if source_seq > 0:
        frame["source_seq"] = source_seq
        payload["source_seq"] = source_seq
        payload["sourceSeq"] = source_seq
    if mission_event_seq > 0:
        frame["team_mission_event_seq"] = mission_event_seq
        payload["team_mission_event_seq"] = mission_event_seq
        payload["teamMissionEventSeq"] = mission_event_seq
    for key in ("mission_id", "conversation_id", "stable_session_id", "node_id", "task_id", "task_frame_id"):
        if payload_declares_subject and key == "node_id":
            continue
        value = _text(identity.get(key))
        if value and not _text(frame.get(key)):
            frame[key] = value
    frame["payload"] = payload
    return frame


def _node_matches_task(node: Dict[str, Any] | None, task_id: str) -> bool:
    normalized = _text(task_id)
    if not normalized:
        return True
    return _task_id_from_node_and_binding(node) == normalized


def _edge_matches_task(edge: Dict[str, Any] | None, selected_node_ids: set[str], task_id: str) -> bool:
    if not isinstance(edge, dict):
        return False
    normalized = _text(task_id)
    if not normalized:
        return True
    edge_metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    edge_task_id = _task_id_from_metadata(edge_metadata)
    if edge_task_id:
        return edge_task_id == normalized
    return (
        _text(edge.get("from_node_id")) in selected_node_ids
        and _text(edge.get("to_node_id")) in selected_node_ids
    )


def _task_scoped_node_id(mission_id: str, task_id: str, suffix: str) -> str:
    normalized = _text(task_id)
    if not normalized:
        return f"team-mission:{mission_id}:{suffix}"
    return f"team-mission:{mission_id}:{normalized}:{suffix}"


def _metadata_with_task_id(metadata: Dict[str, Any] | None, task_id: str) -> Dict[str, Any]:
    result = dict(metadata or {})
    normalized = _text(task_id)
    if normalized:
        result.setdefault("task_id", normalized)
    return result


def _event_with_task_id(event: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    normalized = _text(task_id)
    if not normalized or not isinstance(event, dict):
        return event
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        **event,
        "payload": {
            **payload,
            "task_id": payload.get("task_id") or payload.get("taskId") or normalized,
        },
    }


def _strategy_actions_with_task_id(actions: TeamMissionStrategyActions, task_id: str) -> TeamMissionStrategyActions:
    normalized = _text(task_id)
    if not normalized:
        return actions
    return replace(
        actions,
        nodes=tuple(
            replace(node, metadata=_metadata_with_task_id(node.metadata, normalized))
            for node in actions.nodes
        ),
        edges=tuple(
            replace(edge, metadata=_metadata_with_task_id(edge.metadata, normalized))
            for edge in actions.edges
        ),
        approval_requests=tuple(
            {
                **request,
                "task_id": request.get("task_id") or request.get("taskId") or normalized,
            }
            for request in actions.approval_requests
            if isinstance(request, dict)
        ),
        events=tuple(
            _event_with_task_id(event, normalized)
            for event in actions.events
            if isinstance(event, dict)
        ),
    )


class SessionDBTeamMissionMixin:
    """Native Hermes Team Mission graph and run-binding persistence.

    Team Mission stores only mission-specific graph and ownership metadata here.
    Runtime facts remain in ordinary Hermes ``runs``, ``run_events``, and
    ``messages`` tables so stream coalescing, retention, and history semantics
    stay identical to ordinary sessions.
    """

    def _team_mission_runtime_event_identity(
        self,
        *,
        mission: Dict[str, Any] | None,
        node: Dict[str, Any] | None,
        binding: Dict[str, Any] | None,
    ) -> Dict[str, str]:
        return _team_mission_runtime_event_identity(
            mission=mission,
            node=node,
            binding=binding,
        )

    def _team_mission_conversation_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
        display_title_source = _text(
            (metadata if isinstance(metadata, dict) else {}).get("display_title_source")
            or (metadata if isinstance(metadata, dict) else {}).get("displayTitleSource")
            or "conversation_title"
        )
        updated_at = _row_value(row, "activity_updated_at", _row_value(row, "updated_at", 0))
        message_count = int(_row_value(row, "message_count", 0) or 0)
        return {
            "conversation_id": str(_row_value(row, "conversation_id", "") or ""),
            "team_id": str(_row_value(row, "team_id", "") or ""),
            "stable_session_id": str(_row_value(row, "stable_session_id", "") or ""),
            "title": str(_row_value(row, "title", "") or ""),
            "display_title": str(_row_value(row, "title", "") or ""),
            "display_title_source": display_title_source,
            "objective": str(_row_value(row, "objective", "") or ""),
            "workspace_id": str(_row_value(row, "workspace_id", "") or ""),
            "workspace_path": str(_row_value(row, "workspace_path", "") or ""),
            "status": str(_row_value(row, "status", "") or ""),
            "active_mission_id": str(_row_value(row, "active_mission_id", "") or ""),
            "created_by_user_id": str(_row_value(row, "created_by_user_id", "") or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": float(_row_value(row, "created_at", 0) or 0),
            "updated_at": float(updated_at or 0),
            "message_count": message_count,
        }

    def _team_mission_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "mission_id": str(row["mission_id"] or ""),
            "conversation_id": str(_row_value(row, "conversation_id", "") or ""),
            "team_id": str(row["team_id"] or ""),
            "title": str(row["title"] or ""),
            "objective": str(row["objective"] or ""),
            "workspace_id": str(row["workspace_id"] or ""),
            "workspace_path": str(row["workspace_path"] or ""),
            "mode": str(row["mode"] or ""),
            "status": str(row["status"] or ""),
            "leader_session_id": str(row["leader_session_id"] or ""),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
            "completed_at": row["completed_at"],
            "metadata": _json_loads(row["metadata_json"], {}),
        }

    def _team_mission_node_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        metadata = _json_loads(row["metadata_json"], {})
        raw_kind = str(row["kind"] or "")
        kind = _normalize_node_kind(raw_kind)
        metadata = _metadata_with_normalized_node_kind(metadata, raw_kind=raw_kind, canonical_kind=kind)
        return {
            "node_id": str(row["node_id"] or ""),
            "mission_id": str(row["mission_id"] or ""),
            "kind": kind,
            "title": str(row["title"] or ""),
            "objective": str(row["objective"] or ""),
            "status": str(row["status"] or ""),
            "assignee_profile_id": str(row["assignee_profile_id"] or ""),
            "assignee_profile_version_id": str(row["assignee_profile_version_id"] or ""),
            "canonical_node_id": _text(_row_value(row, "canonical_node_id", "")) or _conversation_graph_node_id(
                _text(_row_value(row, "mission_id", "")),
                _text(_row_value(row, "node_id", "")),
            ),
            "canonicalNodeId": _text(_row_value(row, "canonical_node_id", "")) or _conversation_graph_node_id(
                _text(_row_value(row, "mission_id", "")),
                _text(_row_value(row, "node_id", "")),
            ),
            "task_frame_id": _text(_row_value(row, "task_frame_id", "")) or (
                f"mission-frame:{_text(_row_value(row, 'mission_id', ''))}"
                if _text(_row_value(row, "mission_id", ""))
                else ""
            ),
            "taskFrameId": _text(_row_value(row, "task_frame_id", "")) or (
                f"mission-frame:{_text(_row_value(row, 'mission_id', ''))}"
                if _text(_row_value(row, "mission_id", ""))
                else ""
            ),
            "runtime_stable_session_id": _text(_row_value(row, "runtime_stable_session_id", "")),
            "runtimeStableSessionId": _text(_row_value(row, "runtime_stable_session_id", "")),
            "runtime_session_id": _text(_row_value(row, "runtime_session_id", "")),
            "runtimeSessionId": _text(_row_value(row, "runtime_session_id", "")),
            "runtime_scope_key": str(row["runtime_scope_key"] or ""),
            "runtimeScopeKey": str(row["runtime_scope_key"] or ""),
            "output_contract": _json_loads(row["output_contract_json"], {}),
            "metadata": metadata,
            **_assignee_public_fields(metadata),
            "position_x": float(row["position_x"] or 0),
            "position_y": float(row["position_y"] or 0),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def _team_mission_node_with_resolved_assignee(
        self,
        node: Dict[str, Any] | None,
        *,
        mission_metadata: Dict[str, Any] | None = None,
        leader_node: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if not isinstance(node, dict) or not node:
            return {}
        resolved_profile_id, resolved_profile_version_id, resolved_runtime_scope_key, resolved_metadata = _resolve_node_assignee(
            mission_id=str(node.get("mission_id") or ""),
            node_id=str(node.get("node_id") or ""),
            kind=_normalize_node_kind(node.get("kind")),
            incoming_profile_id=str(node.get("assignee_profile_id") or ""),
            incoming_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
            incoming_runtime_scope_key=str(node.get("runtime_scope_key") or ""),
            metadata=dict(node.get("metadata") or {}),
            mission_metadata=mission_metadata if isinstance(mission_metadata, dict) else {},
            existing_node=node,
            leader_node=leader_node if isinstance(leader_node, dict) else {},
        )
        resolved_node = {
            **node,
            "assignee_profile_id": resolved_profile_id,
            "assignee_profile_version_id": resolved_profile_version_id,
            "runtime_scope_key": resolved_runtime_scope_key,
            "metadata": resolved_metadata,
        }
        resolved_node.update(_assignee_public_fields(resolved_metadata))
        return resolved_node

    def _team_mission_nodes_with_resolved_assignees(
        self,
        nodes: List[Dict[str, Any]],
        *,
        mission_metadata: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        leader_node = next((_node for _node in nodes if _normalize_node_kind(_node.get("kind")) == "root"), {})
        return [
            self._team_mission_node_with_resolved_assignee(
                node,
                mission_metadata=mission_metadata,
                leader_node=leader_node,
            )
            for node in nodes
        ]

    def _team_mission_edge_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "edge_id": str(row["edge_id"] or ""),
            "mission_id": str(row["mission_id"] or ""),
            "from_node_id": str(row["from_node_id"] or ""),
            "to_node_id": str(row["to_node_id"] or ""),
            "kind": str(row["kind"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": float(row["created_at"] or 0),
        }

    def _team_mission_run_binding_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "mission_id": str(row["mission_id"] or ""),
            "node_id": str(row["node_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "session_id": str(row["session_id"] or ""),
            "runtime_session_id": str(row["runtime_session_id"] or ""),
            "runtime_scope_key": str(row["runtime_scope_key"] or ""),
            "role": str(row["role"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def _team_mission_latest_run_bindings_by_node(
        self,
        bindings: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            node_id = _text(binding.get("node_id"))
            if not node_id:
                continue
            current = latest.get(node_id)
            if current is None:
                latest[node_id] = binding
                continue
            current_key = (
                float(current.get("updated_at") or 0),
                float(current.get("created_at") or 0),
                _text(current.get("run_id")),
            )
            binding_key = (
                float(binding.get("updated_at") or 0),
                float(binding.get("created_at") or 0),
                _text(binding.get("run_id")),
            )
            if binding_key >= current_key:
                latest[node_id] = binding
        return latest

    def _team_mission_node_with_runtime_binding(
        self,
        node: Dict[str, Any] | None,
        binding: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        if not isinstance(node, dict) or not node:
            return {}
        if not isinstance(binding, dict) or not binding:
            return node
        node_id = _text(node.get("node_id"))
        if node_id and _text(binding.get("node_id")) and node_id != _text(binding.get("node_id")):
            return node

        session_id = _text(node.get("runtime_stable_session_id")) or _text(binding.get("session_id"))
        runtime_session_id = _text(node.get("runtime_session_id")) or _text(binding.get("runtime_session_id"))
        runtime_scope_key = _text(node.get("runtime_scope_key")) or _text(binding.get("runtime_scope_key"))
        run_id = _text(binding.get("run_id"))
        canonical_node_id = _text(node.get("canonical_node_id")) or _conversation_graph_node_id(
            _text(node.get("mission_id") or binding.get("mission_id")),
            node_id,
        )
        task_frame_id = _text(node.get("task_frame_id")) or (
            f"mission-frame:{_text(node.get('mission_id') or binding.get('mission_id'))}"
            if _text(node.get("mission_id") or binding.get("mission_id"))
            else ""
        )

        return {
            **node,
            "canonical_node_id": canonical_node_id,
            "canonicalNodeId": canonical_node_id,
            "task_frame_id": task_frame_id,
            "taskFrameId": task_frame_id,
            "run_id": run_id,
            "runId": run_id,
            "session_id": session_id,
            "sessionId": session_id,
            "stable_session_id": session_id,
            "stableSessionId": session_id,
            "stored_session_id": session_id,
            "storedSessionId": session_id,
            "actual_stable_session_id": session_id,
            "actualStableSessionId": session_id,
            "runtime_stable_session_id": session_id,
            "runtimeStableSessionId": session_id,
            "runtime_session_id": runtime_session_id,
            "runtimeSessionId": runtime_session_id,
            "runtime_scope_key": runtime_scope_key,
            "runtimeScopeKey": runtime_scope_key,
            "runtime_binding": binding,
            "runtimeBinding": binding,
        }

    def _team_mission_nodes_with_runtime_bindings(
        self,
        nodes: List[Dict[str, Any]],
        bindings: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        latest_by_node = self._team_mission_latest_run_bindings_by_node(bindings)
        return [
            self._team_mission_node_with_runtime_binding(
                node,
                latest_by_node.get(_text(node.get("node_id"))),
            )
            for node in nodes
        ]

    def _team_mission_memory_item_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "team_id": str(row["team_id"] or ""),
            "mission_id": str(row["mission_id"] or ""),
            "conversation_session_id": str(row["conversation_session_id"] or ""),
            "task_id": str(row["task_id"] or ""),
            "scope": str(row["scope"] or ""),
            "kind": str(row["kind"] or ""),
            "content": str(row["content"] or ""),
            "structured_payload": _json_loads(row["structured_payload_json"], {}),
            "source_node_ids": _json_loads(row["source_node_ids_json"], []),
            "source_run_ids": _json_loads(row["source_run_ids_json"], []),
            "artifact_refs": _json_loads(row["artifact_refs_json"], []),
            "workspace_refs": _json_loads(row["workspace_refs_json"], []),
            "confidence": float(row["confidence"] or 0),
            "visibility": str(row["visibility"] or ""),
            "status": str(row["status"] or ""),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
            "invalidated_at": row["invalidated_at"],
        }

    def _team_mission_message_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        if row is None:
            return {}
        message = dict(row)
        if "content" in message:
            message["content"] = self._decode_content(message["content"])
        if message.get("metadata_json"):
            metadata = _json_loads(message.get("metadata_json"), None)
            if isinstance(metadata, dict):
                message["metadata"] = metadata
        return message

    def _team_mission_memory_edge_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "from_memory_id": str(row["from_memory_id"] or ""),
            "to_memory_id": str(row["to_memory_id"] or ""),
            "relation": str(row["relation"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": float(row["created_at"] or 0),
        }

    def upsert_team_mission_conversation(
        self,
        *,
        conversation_id: str,
        team_id: str = "",
        stable_session_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        status: str = "active",
        active_mission_id: str = "",
        created_by_user_id: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
        metadata: Dict[str, Any] | None = None,
        replace_title: bool = False,
        touch: bool = False,
    ) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        stable_session_id = _text(stable_session_id) or conversation_id
        now = time.time()
        created = float(created_at or now)
        requested_updated = float(updated_at if updated_at is not None else now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, title, created_at, updated_at FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            existing_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            merged_metadata = dict(existing_metadata)
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata["conversation_id"] = conversation_id
            merged_metadata["stable_session_id"] = stable_session_id
            existing_title = _text(_row_value(existing, "title", ""))
            requested_title = _text(title)
            existing_display_title_source = _text(
                existing_metadata.get("display_title_source")
                or existing_metadata.get("displayTitleSource")
            )
            requested_display_title_source = _text(
                merged_metadata.get("display_title_source")
                or merged_metadata.get("displayTitleSource")
            )
            existing_title_is_replaceable = _is_replaceable_team_mission_conversation_title(
                existing_title,
                existing_display_title_source,
            )
            should_write_requested_title = bool(
                requested_title
                and (replace_title or not existing_title or existing_title_is_replaceable)
            )
            if (
                should_write_requested_title
                and not _is_placeholder_team_mission_conversation_title(requested_title)
                and not requested_display_title_source
            ):
                merged_metadata["display_title_source"] = "first_user_message"
            insert_title = requested_title or existing_title or "Team Mission"
            update_title = requested_title if should_write_requested_title else ""
            existing_updated = float(_row_value(existing, "updated_at", requested_updated) or requested_updated)
            update_updated = requested_updated if (updated_at is not None or touch or existing is None) else existing_updated
            conn.execute(
                """
                INSERT INTO team_mission_conversations (
                    conversation_id, team_id, stable_session_id, title, objective,
                    workspace_id, workspace_path, status, active_mission_id,
                    created_by_user_id, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    team_id = COALESCE(NULLIF(excluded.team_id, ''), team_id),
                    stable_session_id = excluded.stable_session_id,
                    title = COALESCE(NULLIF(?, ''), title),
                    objective = COALESCE(NULLIF(excluded.objective, ''), objective),
                    workspace_id = COALESCE(NULLIF(excluded.workspace_id, ''), workspace_id),
                    workspace_path = COALESCE(NULLIF(excluded.workspace_path, ''), workspace_path),
                    status = excluded.status,
                    active_mission_id = COALESCE(NULLIF(excluded.active_mission_id, ''), active_mission_id),
                    created_by_user_id = COALESCE(NULLIF(excluded.created_by_user_id, ''), created_by_user_id),
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    _text(team_id),
                    stable_session_id,
                    insert_title,
                    _text(objective),
                    _text(workspace_id),
                    _text(workspace_path),
                    _conversation_status(status),
                    _text(active_mission_id),
                    _text(created_by_user_id),
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                    float(_row_value(existing, "created_at", created) or created),
                    update_updated,
                    update_title,
                ),
            )
            return self._team_mission_conversation_from_row(conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()) or {}

        record = self._execute_write(_do)
        self._project_team_conversation_to_session_index(record)
        return record

    def _project_team_conversation_to_session_index(self, record: Dict[str, Any]) -> None:
        """Surface a team-mission conversation in the control-plane session_index.

        ON CONFLICT updates only the static/team fields — never the live status
        projection (that is owned by update_session_index_for_mission, driven by
        the graph reducer). Keyed by the conversation's stable session id; carries
        mission_id so mission-status updates can target it. Best-effort."""
        if not record:
            return
        sid = _text(record.get("stable_session_id")) or _text(record.get("conversation_id"))
        if not sid:
            return
        title = _text(record.get("title"))
        team_id = _text(record.get("team_id"))
        conversation_id = _text(record.get("conversation_id"))
        mission_id = _text(record.get("active_mission_id"))
        message_count = int(record.get("message_count") or 0)
        started = float(record.get("created_at") or 0)
        updated = float(record.get("updated_at") or 0) or started

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO session_index (
                    session_id, title, source, session_kind, team_id,
                    conversation_id, mission_id, message_count, started_at, updated_at
                ) VALUES (?, ?, 'team_mission', 'team_mission', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title=excluded.title,
                    source=excluded.source,
                    session_kind=excluded.session_kind,
                    team_id=excluded.team_id,
                    conversation_id=excluded.conversation_id,
                    mission_id=excluded.mission_id,
                    message_count=excluded.message_count,
                    updated_at=excluded.updated_at
                """,
                (sid, title, team_id, conversation_id, mission_id, message_count, started, updated),
            )

        try:
            self._execute_write(_do)
        except Exception:
            pass

    def update_session_index_for_mission(
        self,
        mission_id: str,
        *,
        status: str,
        running: bool,
        waiting_approval: bool = False,
    ) -> int:
        """Project a mission's live state onto its conversation's session_index row
        (matched by mission_id). UPDATE-only; returns rows affected."""
        mid = _text(mission_id)
        if not mid:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            if not running:
                # A not-running row must NOT keep a stale active_run_id /
                # active_runtime_session_id. The sidebar derives running as
                # (running || active_run_id), so a leftover active_run_id makes a
                # finished team conversation spin forever even with running=0.
                return int(conn.execute(
                    """
                    UPDATE session_index
                       SET status = ?, running = 0, waiting_approval = ?,
                           active_run_id = '', active_runtime_session_id = '',
                           pending_approval_count = 0
                     WHERE mission_id = ?
                    """,
                    (str(status or "idle"), 1 if waiting_approval else 0, mid),
                ).rowcount or 0)
            return int(conn.execute(
                """
                UPDATE session_index
                   SET status = ?, running = 1, waiting_approval = ?
                 WHERE mission_id = ?
                """,
                (str(status or "idle"), 1 if waiting_approval else 0, mid),
            ).rowcount or 0)

        try:
            return self._execute_write(_do)
        except Exception:
            return 0

    def ensure_team_mission_conversation(
        self,
        *,
        conversation_id: str = "",
        stable_session_id: str = "",
        mission: Dict[str, Any] | None = None,
        mission_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        created_by_user_id: str = "",
        metadata: Dict[str, Any] | None = None,
        updated_at: float | None = None,
        touch: bool = False,
    ) -> Dict[str, Any]:
        mission = mission if isinstance(mission, dict) else {}
        mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
        merged_metadata = dict(mission_metadata)
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        resolved_mission_id = _text(mission_id or mission.get("mission_id"))
        explicit_conversation_id = (
            _text(conversation_id)
            or _text(mission.get("conversation_id"))
            or _conversation_id_from_metadata(merged_metadata)
        )
        resolved_conversation_id = (
            explicit_conversation_id
            or _text((self.get_team_mission_conversation_by_session(
                _stable_session_id_from_metadata(
                    merged_metadata,
                    _text(mission.get("leader_session_id") or mission.get("team_id") or resolved_mission_id),
                )
            ) or {}).get("conversation_id"))
            or resolved_mission_id
        )
        if not resolved_conversation_id:
            return {}
        resolved_stable_session_id = (
            _text(stable_session_id)
            or _stable_session_id_from_metadata(
                merged_metadata,
                _text(mission.get("leader_session_id") or mission.get("team_id") or resolved_conversation_id),
            )
        )
        merged_metadata["conversation_id"] = resolved_conversation_id
        merged_metadata["conversation_session_id"] = resolved_stable_session_id
        merged_metadata["stableTeamSessionId"] = resolved_stable_session_id
        conversation = self.upsert_team_mission_conversation(
            conversation_id=resolved_conversation_id,
            team_id=_text(team_id or mission.get("team_id")),
            stable_session_id=resolved_stable_session_id,
            title=_text(title),
            objective=_text(objective),
            workspace_id=_text(workspace_id or mission.get("workspace_id")),
            workspace_path=_text(workspace_path or mission.get("workspace_path")),
            status="active",
            active_mission_id=resolved_mission_id,
            created_by_user_id=_text(created_by_user_id),
            updated_at=updated_at,
            metadata=merged_metadata,
            touch=touch,
        )
        if resolved_mission_id:
            def _bind(conn: sqlite3.Connection) -> None:
                conn.execute(
                    """
                    UPDATE team_missions
                    SET conversation_id = ?,
                        leader_session_id = COALESCE(NULLIF(leader_session_id, ''), ?)
                    WHERE mission_id = ?
                    """,
                    (resolved_conversation_id, resolved_stable_session_id, resolved_mission_id),
                )

            self._execute_write(_bind)
        try:
            if resolved_stable_session_id and not self.get_session(resolved_stable_session_id):
                self.create_session(resolved_stable_session_id, source="team_mission", transient=False)
        except Exception:
            pass
        return conversation

    def get_team_mission_conversation(self, conversation_id: str) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        with self._lock:
            return self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()) or {}

    def get_team_mission_conversation_by_session(self, stable_session_id: str) -> Dict[str, Any]:
        stable_session_id = _text(stable_session_id)
        if not stable_session_id:
            return {}
        with self._lock:
            return self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE stable_session_id = ?",
                (stable_session_id,),
            ).fetchone()) or {}

    def resolve_team_mission_conversation(self, identifier: str) -> Dict[str, Any]:
        identifier = _text(identifier)
        if not identifier:
            return {}
        conversation = self.get_team_mission_conversation(identifier) or self.get_team_mission_conversation_by_session(identifier)
        if conversation and not _conversation_routeable(self, conversation.get("conversation_id")):
            conversation = {}
        if not conversation:
            with self._lock:
                mission = self._team_mission_from_row(self._conn.execute(
                    "SELECT * FROM team_missions WHERE mission_id = ?",
                    (identifier,),
                ).fetchone())
            if mission:
                conversation = self.ensure_team_mission_conversation(mission=mission)
        if not conversation:
            with self._lock:
                mission = self._team_mission_from_row(self._conn.execute(
                    "SELECT * FROM team_missions WHERE conversation_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (identifier,),
                ).fetchone())
            if mission:
                conversation = self.ensure_team_mission_conversation(
                    conversation_id=identifier,
                    mission=mission,
                )
        if not conversation:
            return {}
        graph = self.get_team_mission_conversation_graph(_text(conversation.get("conversation_id")))
        messages = list((graph or {}).get("recent_messages") or [])
        page_info = (graph or {}).get("message_page_info") or {}
        return {
            "conversation": conversation,
            "mission": (graph.get("mission") if isinstance(graph, dict) else {}) or {},
            "graph": graph if isinstance(graph, dict) else {},
            "messages": messages,
            "pageInfo": page_info,
            "page_info": page_info,
        }

    def list_team_mission_conversations(
        self,
        *,
        team_id: str = "",
        workspace_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if _text(team_id):
            clauses.append("team_id = ?")
            params.append(_text(team_id))
        if _text(workspace_id):
            clauses.append("workspace_id = ?")
            params.append(_text(workspace_id))
        if _text(status):
            clauses.append("status = ?")
            params.append(_conversation_status(status))
        clauses.append(_conversation_history_sql())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit or 100), 500))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT team_mission_conversations.*,
                    COALESCE(session_summary.message_count, 0) AS message_count,
                    MAX(
                        COALESCE(
                            session_summary.last_active,
                            session_summary.started_at,
                            0
                        ),
                        COALESCE(created_at, 0)
                    ) AS activity_updated_at
                FROM team_mission_conversations
                LEFT JOIN sessions session_summary
                  ON session_summary.id = team_mission_conversations.stable_session_id
                {where_sql}
                ORDER BY activity_updated_at DESC, created_at DESC, conversation_id ASC
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()
        return [
            conversation for conversation in (
                self._team_mission_conversation_from_row(row)
                for row in rows
            ) if conversation is not None
        ]

    def list_team_mission_conversation_runtime_session_ids(
        self,
        *,
        team_id: str = "",
        workspace_id: str = "",
        status: str = "",
        mission_id: str = "",
        limit: int = 500,
    ) -> List[str]:
        """Return only Team Mission conversation and node runtime session ids.

        This is intentionally separate from ``list_team_mission_conversations``.
        Sidebar/runtime indexing needs identifiers, not graph, message, or
        deliverable payloads. Keeping this query narrow prevents history size
        from inflating WebSocket responses.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if _text(team_id):
            clauses.append("c.team_id = ?")
            params.append(_text(team_id))
        if _text(workspace_id):
            clauses.append("c.workspace_id = ?")
            params.append(_text(workspace_id))
        if _text(status):
            clauses.append("c.status = ?")
            params.append(_conversation_status(status))
        normalized_mission_id = _text(mission_id)
        if normalized_mission_id:
            clauses.append(
                """(
                    c.active_mission_id = ?
                    OR c.conversation_id = ?
                    OR c.stable_session_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM team_missions mission_filter
                        WHERE mission_filter.conversation_id = c.conversation_id
                          AND mission_filter.mission_id = ?
                    )
                )"""
            )
            params.extend([
                normalized_mission_id,
                normalized_mission_id,
                normalized_mission_id,
                normalized_mission_id,
            ])
        clauses.append(_conversation_history_sql("c"))
        where_sql = f"WHERE {' AND '.join(clauses)}"
        bounded_limit = max(1, min(int(limit or 500), 500))

        def append_unique(target: list[str], seen: set[str], *values: Any) -> None:
            for value in values:
                normalized = _text(value)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    target.append(normalized)

        with self._lock:
            conversation_rows = self._conn.execute(
                f"""
                SELECT c.conversation_id, c.stable_session_id
                FROM team_mission_conversations c
                {where_sql}
                ORDER BY COALESCE(c.updated_at, c.created_at, 0) DESC,
                         c.created_at DESC,
                         c.conversation_id ASC
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()

            conversation_ids = [
                _text(_row_value(row, "conversation_id", ""))
                for row in conversation_rows
                if _text(_row_value(row, "conversation_id", ""))
            ]
            stable_session_ids = [
                _text(_row_value(row, "stable_session_id", ""))
                for row in conversation_rows
                if _text(_row_value(row, "stable_session_id", ""))
            ]

            mission_rows: list[sqlite3.Row] = []
            binding_rows: list[sqlite3.Row] = []
            if conversation_ids:
                conversation_placeholders = ",".join("?" for _ in conversation_ids)
                mission_rows = self._conn.execute(
                    f"""
                    SELECT mission_id, leader_session_id
                    FROM team_missions
                    WHERE conversation_id IN ({conversation_placeholders})
                    ORDER BY created_at ASC, mission_id ASC
                    """,
                    tuple(conversation_ids),
                ).fetchall()
                mission_ids = [
                    _text(_row_value(row, "mission_id", ""))
                    for row in mission_rows
                    if _text(_row_value(row, "mission_id", ""))
                ]
                if mission_ids:
                    mission_placeholders = ",".join("?" for _ in mission_ids)
                    binding_rows = self._conn.execute(
                        f"""
                        SELECT session_id, runtime_session_id
                        FROM team_mission_run_bindings
                        WHERE mission_id IN ({mission_placeholders})
                        ORDER BY created_at ASC, run_id ASC
                        """,
                        tuple(mission_ids),
                    ).fetchall()

            active_run_rows: list[sqlite3.Row] = []
            if stable_session_ids:
                stable_placeholders = ",".join("?" for _ in stable_session_ids)
                status_placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)
                active_run_rows = self._conn.execute(
                    f"""
                    SELECT session_id, runtime_session_id
                    FROM runs
                    WHERE session_id IN ({stable_placeholders})
                      AND status IN ({status_placeholders})
                    ORDER BY updated_at DESC, started_at DESC, run_id ASC
                    """,
                    (*stable_session_ids, *sorted(_ACTIVE_RUN_STATUSES)),
                ).fetchall()

        ids: list[str] = []
        seen_ids: set[str] = set()
        append_unique(ids, seen_ids, *stable_session_ids)
        for row in mission_rows:
            append_unique(ids, seen_ids, _row_value(row, "leader_session_id", ""))
        for row in active_run_rows:
            append_unique(
                ids,
                seen_ids,
                _row_value(row, "session_id", ""),
                _row_value(row, "runtime_session_id", ""),
            )
        for row in binding_rows:
            append_unique(
                ids,
                seen_ids,
                _row_value(row, "session_id", ""),
                _row_value(row, "runtime_session_id", ""),
            )
        return ids

    def _team_mission_conversation_deliverable_projection(
        self,
        conversation: Dict[str, Any],
        missions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        mission_ids = [
            _text(mission.get("mission_id"))
            for mission in missions
            if _text(mission.get("mission_id"))
        ]
        mission_id_set = set(mission_ids)
        stable_session_id = _text(
            conversation.get("stable_session_id")
            or conversation.get("stableSessionId")
        )

        last_message: Dict[str, Any] = {}
        final_deliverables: List[Dict[str, Any]] = []
        if stable_session_id:
            with self._lock:
                last_message_row = self._conn.execute(
                    """
                    SELECT *
                    FROM messages
                    WHERE session_id = ?
                      AND active = 1
                      AND role IN ('user', 'assistant')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (stable_session_id,),
                ).fetchone()
                final_rows = self._conn.execute(
                    """
                    SELECT *
                    FROM messages
                    WHERE session_id = ?
                      AND active = 1
                      AND role = 'assistant'
                      AND metadata_json LIKE ?
                    ORDER BY id ASC
                    """,
                    (stable_session_id, "%final_deliverable%"),
                ).fetchall()
            last_message = _message_summary_from_message(self._team_mission_message_from_row(last_message_row))
            for row in final_rows:
                message = self._team_mission_message_from_row(row)
                deliverable = _final_deliverable_from_message(message)
                if not deliverable:
                    continue
                mission_id = _text(deliverable.get("mission_id"))
                if mission_id not in mission_id_set:
                    continue
                final_deliverables.append(deliverable)

        if not mission_ids:
            return {
                "last_message": last_message,
                "last_message_preview": _text(last_message.get("preview")),
                "last_message_at": last_message.get("timestamp") or 0,
                "final_deliverables": [],
                "final_deliverables_by_mission": {},
                "final_deliverables_by_task": {},
                "artifact_refs": [],
                "artifact_refs_by_mission": {},
                "artifact_refs_by_task": {},
            }

        placeholders = ",".join("?" for _ in mission_ids)
        memory_params: List[Any] = [*mission_ids, _MEMORY_COMMITTED_STATUS]
        memory_clauses = [
            f"mission_id IN ({placeholders})",
            "status = ?",
        ]
        if stable_session_id:
            memory_clauses.append("conversation_session_id = ?")
            memory_params.append(stable_session_id)
        with self._lock:
            memory_rows = self._conn.execute(
                f"""
                SELECT *
                FROM team_mission_memory_items
                WHERE {' AND '.join(memory_clauses)}
                ORDER BY created_at ASC, updated_at ASC, id ASC
                """,
                tuple(memory_params),
            ).fetchall()
        artifact_refs_by_mission: Dict[str, List[Dict[str, Any]]] = {}
        artifact_refs_by_task: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        all_artifact_refs: List[Dict[str, Any]] = []
        for row in memory_rows:
            item = self._team_mission_memory_item_from_row(row)
            if not item:
                continue
            refs = _dedupe_artifact_refs(list(item.get("artifact_refs") or []))
            if not refs:
                continue
            mission_id = _text(item.get("mission_id"))
            task_id = _text(item.get("task_id"))
            artifact_refs_by_mission[mission_id] = _dedupe_artifact_refs([
                *artifact_refs_by_mission.get(mission_id, []),
                *refs,
            ])
            if task_id:
                artifact_refs_by_task[(mission_id, task_id)] = _dedupe_artifact_refs([
                    *artifact_refs_by_task.get((mission_id, task_id), []),
                    *refs,
                ])
            all_artifact_refs.extend(refs)

        final_deliverables = [
            _final_deliverable_with_artifact_refs(
                deliverable,
                artifact_refs_by_mission,
                artifact_refs_by_task,
            )
            for deliverable in final_deliverables
        ]
        final_deliverables_by_mission: Dict[str, List[Dict[str, Any]]] = {}
        final_deliverables_by_task: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for deliverable in final_deliverables:
            mission_id = _text(deliverable.get("mission_id"))
            task_id = _text(deliverable.get("task_id"))
            final_deliverables_by_mission.setdefault(mission_id, []).append(deliverable)
            if task_id:
                final_deliverables_by_task.setdefault((mission_id, task_id), []).append(deliverable)
        last_message = _message_with_deliverable_artifact_refs(last_message, final_deliverables)

        return {
            "last_message": last_message,
            "last_message_preview": _text(last_message.get("preview")),
            "last_message_at": last_message.get("timestamp") or 0,
            "final_deliverables": final_deliverables,
            "final_deliverables_by_mission": final_deliverables_by_mission,
            "final_deliverables_by_task": final_deliverables_by_task,
            "artifact_refs": _dedupe_artifact_refs(all_artifact_refs),
            "artifact_refs_by_mission": artifact_refs_by_mission,
            "artifact_refs_by_task": artifact_refs_by_task,
        }

    def get_team_mission_conversation_runtime_summary(self, conversation_id: str) -> Dict[str, Any]:
        """Return the lightweight Team Mission facts needed by conversation lists.

        The full conversation graph is still available through
        ``resolve_team_mission_conversation``. List views need a stable
        projection of task frames, active nodes, approval gates, and runtime
        session ids without loading run event history or node detail payloads.
        """
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        with self._lock:
            conversation = self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone())
            if conversation is None:
                return {}
            missions = [
                mission for mission in (
                    self._team_mission_from_row(row)
                    for row in self._conn.execute(
                        """
                        SELECT *
                        FROM team_missions
                        WHERE conversation_id = ?
                        ORDER BY created_at ASC, updated_at ASC, mission_id ASC
                        """,
                        (conversation_id,),
                    ).fetchall()
                ) if mission is not None
            ]
            mission_ids = [_text(mission.get("mission_id")) for mission in missions if _text(mission.get("mission_id"))]
            placeholders = ",".join("?" for _ in mission_ids)
            node_rows = self._conn.execute(
                f"""
                SELECT *
                FROM team_mission_nodes
                WHERE mission_id IN ({placeholders})
                ORDER BY created_at ASC, node_id ASC
                """,
                tuple(mission_ids),
            ).fetchall() if mission_ids else []
            binding_rows = self._conn.execute(
                f"""
                SELECT *
                FROM team_mission_run_bindings
                WHERE mission_id IN ({placeholders})
                ORDER BY created_at ASC, run_id ASC
                """,
                tuple(mission_ids),
            ).fetchall() if mission_ids else []

        if not missions:
            deliverable_projection = self._team_mission_conversation_deliverable_projection(conversation, [])
            return {
                "conversation": conversation,
                "mission": {},
                "active_mission_id": _text(conversation.get("active_mission_id")),
                "mission_status": "",
                "task_frames": [],
                "task_frame_count": 0,
                "active_task_frame": {},
                "pending_approvals": [],
                "pending_approval_count": 0,
                "active_node_count": 0,
                "run_bindings": [],
                "run_session_ids": [],
                "last_message": deliverable_projection.get("last_message") or {},
                "last_message_preview": deliverable_projection.get("last_message_preview") or "",
                "last_message_at": deliverable_projection.get("last_message_at") or 0,
                "final_deliverables": [],
                "artifact_refs": [],
            }

        nodes_by_mission: Dict[str, List[Dict[str, Any]]] = {mission_id: [] for mission_id in mission_ids}
        for row in node_rows:
            node = self._team_mission_node_from_row(row)
            if not node:
                continue
            nodes_by_mission.setdefault(_text(node.get("mission_id")), []).append(node)

        bindings: List[Dict[str, Any]] = []
        bindings_by_mission: Dict[str, List[Dict[str, Any]]] = {}
        run_session_ids: List[str] = []
        seen_run_session_ids: set[str] = set()
        for row in binding_rows:
            binding = self._team_mission_run_binding_from_row(row)
            if not binding:
                continue
            bindings.append(binding)
            bindings_by_mission.setdefault(_text(binding.get("mission_id")), []).append(binding)
            for key in ("session_id", "runtime_session_id"):
                value = _text(binding.get(key))
                if value and value not in seen_run_session_ids:
                    seen_run_session_ids.add(value)
                    run_session_ids.append(value)

        for mission_id, mission_nodes in list(nodes_by_mission.items()):
            nodes_by_mission[mission_id] = self._team_mission_nodes_with_runtime_bindings(
                mission_nodes,
                bindings_by_mission.get(mission_id) or [],
            )

        active_mission_id = _text(conversation.get("active_mission_id"))
        latest_mission = missions[-1]
        active_mission = next(
            (mission for mission in missions if _text(mission.get("mission_id")) == active_mission_id),
            latest_mission,
        )
        active_mission_id = _text(active_mission.get("mission_id")) or active_mission_id
        deliverable_projection = self._team_mission_conversation_deliverable_projection(conversation, missions)
        deliverables_by_mission = deliverable_projection.get("final_deliverables_by_mission") or {}
        deliverables_by_task = deliverable_projection.get("final_deliverables_by_task") or {}
        artifact_refs_by_mission = deliverable_projection.get("artifact_refs_by_mission") or {}
        artifact_refs_by_task = deliverable_projection.get("artifact_refs_by_task") or {}

        task_frames: List[Dict[str, Any]] = []
        pending_approvals: List[Dict[str, Any]] = []
        active_node_count = 0
        for mission in missions:
            mission_id = _text(mission.get("mission_id"))
            mission_nodes = nodes_by_mission.get(mission_id) or []
            node_ids: List[str] = []
            root_node_id = ""
            for node in mission_nodes:
                original_node_id = _text(node.get("node_id"))
                if not original_node_id:
                    continue
                namespaced_node_id = _conversation_graph_node_id(mission_id, original_node_id)
                node_ids.append(namespaced_node_id)
                node_kind = _normalize_node_kind(_text(node.get("kind")))
                node_status = _text(node.get("status")).lower()
                if not root_node_id and node_kind == "root":
                    root_node_id = namespaced_node_id
                if node_status in _ACTIVE_NODE_STATUSES:
                    active_node_count += 1
                if node_kind == "approval_gate" and node_status == "waiting_approval":
                    pending_approvals.append({
                        "mission_id": mission_id,
                        "missionId": mission_id,
                        "task_frame_id": f"mission-frame:{mission_id}",
                        "taskFrameId": f"mission-frame:{mission_id}",
                        "node_id": namespaced_node_id,
                        "nodeId": namespaced_node_id,
                        "hermes_node_id": original_node_id,
                        "hermesNodeId": original_node_id,
                        "title": _text(node.get("title")) or "审批任务图",
                        "status": node_status,
                        "run_id": _text(node.get("run_id")),
                        "runId": _text(node.get("run_id")),
                        "stored_session_id": _text(node.get("stored_session_id")),
                        "storedSessionId": _text(node.get("stored_session_id")),
                        "runtime_session_id": _text(node.get("runtime_session_id")),
                        "runtimeSessionId": _text(node.get("runtime_session_id")),
                        "runtime_scope_key": _text(node.get("runtime_scope_key")),
                        "runtimeScopeKey": _text(node.get("runtime_scope_key")),
                        "runtime_binding": dict(node.get("runtime_binding") or {}),
                        "runtimeBinding": dict(node.get("runtime_binding") or {}),
                    })
            task_id = _task_id_from_mission(mission)
            frame_artifact_refs = _dedupe_artifact_refs([
                *list(artifact_refs_by_mission.get(mission_id, [])),
                *list(artifact_refs_by_task.get((mission_id, task_id), [])),
            ])
            final_deliverable = _final_deliverable_for_frame(
                deliverables_by_mission,
                deliverables_by_task,
                mission_id,
                task_id,
                frame_artifact_refs,
            )
            frame = {
                "id": f"mission-frame:{mission_id}",
                "runId": _text(mission.get("leader_session_id")),
                "missionId": mission_id,
                "mission_id": mission_id,
                "taskId": task_id,
                "task_id": task_id,
                "title": _text(mission.get("title")) or _text(conversation.get("title")) or "团队任务",
                "objective": _text(mission.get("objective")) or _text(mission.get("title")) or "团队任务",
                "status": _text(mission.get("status")) or "planning",
                "source": "hermes_conversation",
                "rootNodeId": root_node_id or (node_ids[0] if node_ids else ""),
                "root_node_id": root_node_id or (node_ids[0] if node_ids else ""),
                "nodeIds": node_ids,
                "node_ids": node_ids,
                "createdAt": mission.get("created_at") or 0,
                "created_at": mission.get("created_at") or 0,
                "updatedAt": mission.get("updated_at") or 0,
                "updated_at": mission.get("updated_at") or 0,
                "completedAt": mission.get("completed_at"),
                "completed_at": mission.get("completed_at"),
                "artifactRefs": frame_artifact_refs,
                "artifact_refs": frame_artifact_refs,
            }
            if final_deliverable:
                frame.update({
                    "finalDeliverable": final_deliverable,
                    "final_deliverable": final_deliverable,
                    "deliverableMessageId": final_deliverable.get("messageId") or "",
                    "deliverable_message_id": final_deliverable.get("message_id") or "",
                })
            task_frames.append(frame)

        active_task_frame = next(
            (frame for frame in task_frames if _text(frame.get("missionId")) == active_mission_id),
            task_frames[-1] if task_frames else {},
        )
        mission_status = _text(active_mission.get("status")) or _text(conversation.get("status"))
        return {
            "conversation": conversation,
            "mission": active_mission,
            "active_mission_id": active_mission_id,
            "mission_status": mission_status,
            "task_frames": task_frames,
            "task_frame_count": len(task_frames),
            "active_task_frame": active_task_frame,
            "pending_approvals": pending_approvals,
            "pending_approval_count": len(pending_approvals),
            "active_node_count": active_node_count,
            "run_bindings": bindings,
            "run_session_ids": run_session_ids,
            "last_message": deliverable_projection.get("last_message") or {},
            "last_message_preview": deliverable_projection.get("last_message_preview") or "",
            "last_message_at": deliverable_projection.get("last_message_at") or 0,
            "final_deliverables": list(deliverable_projection.get("final_deliverables") or []),
            "artifact_refs": list(deliverable_projection.get("artifact_refs") or []),
        }

    def _team_mission_conversation_active_run(self, stable_session_id: str) -> Dict[str, Any]:
        stable_session_id = _text(stable_session_id)
        if not stable_session_id:
            return {}
        placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT *
                FROM runs
                WHERE session_id = ?
                  AND status IN ({placeholders})
                ORDER BY updated_at DESC, started_at DESC, run_id ASC
                LIMIT 1
                """,
                (stable_session_id, *sorted(_ACTIVE_RUN_STATUSES)),
            ).fetchone()
        try:
            return self._run_from_row(row) or {}
        except Exception:
            return {}

    def _team_mission_active_node_run_from_bindings(
        self,
        bindings: List[Dict[str, Any]] | None,
    ) -> Dict[str, Any]:
        active_run: Dict[str, Any] = {}
        for binding in bindings or []:
            if not isinstance(binding, dict):
                continue
            run_id = _text(binding.get("run_id") or binding.get("runId"))
            if not run_id:
                continue
            run = self.get_run(run_id) if hasattr(self, "get_run") else None
            if _text((run or {}).get("status")).lower() not in _ACTIVE_RUN_STATUSES:
                continue
            merged_run = {
                **binding,
                **dict(run or {}),
                "run_id": run_id,
                "runtime_session_id": _text((run or {}).get("runtime_session_id") or binding.get("runtime_session_id")),
                "runtime_scope_key": _text((run or {}).get("runtime_scope_key") or binding.get("runtime_scope_key")),
                "turn_id": _text((run or {}).get("turn_id") or binding.get("turn_id")),
            }
            if not active_run or float(merged_run.get("updated_at") or 0) >= float(active_run.get("updated_at") or 0):
                active_run = merged_run
        return active_run

    def _team_mission_conversation_message_count(self, stable_session_id: str) -> int:
        stable_session_id = _text(stable_session_id)
        if not stable_session_id:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(message_count, 0) AS message_count FROM sessions WHERE id = ?",
                (stable_session_id,),
            ).fetchone()
        try:
            return int(_row_value(row, "message_count", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def get_team_mission_conversation_status_projection(self, conversation_id: str) -> Dict[str, Any]:
        """Return the canonical Team Mission conversation status event payload.

        This is the event-stream counterpart to ``team_mission.conversation.list``:
        it projects only sidebar/index facts and keeps raw runtime trace in
        ``run_events``.
        """
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        summary = self.get_team_mission_conversation_runtime_summary(conversation_id)
        if not isinstance(summary, dict) or not summary:
            return {}
        conversation = summary.get("conversation") if isinstance(summary.get("conversation"), dict) else {}
        conversation = dict(conversation or self.get_team_mission_conversation(conversation_id) or {})
        if not conversation:
            return {}
        stable_session_id = _text(conversation.get("stable_session_id") or conversation.get("stableSessionId"))
        active_run = self._team_mission_conversation_active_run(stable_session_id)
        active_node_run = self._team_mission_active_node_run_from_bindings(
            [
                item for item in summary.get("run_bindings") or []
                if isinstance(item, dict)
            ],
        )
        pending_approvals = [
            item for item in summary.get("pending_approvals") or []
            if isinstance(item, dict)
        ]
        mission_status = _text(summary.get("mission_status"))
        # Resolve the run state from the ACTIVE MISSION's real status. Never fall
        # back to conversation.get("status") — that is the conversation lifecycle
        # state ('active' = not archived), NOT a run state, and treating it as
        # non-terminal made finished team conversations show as "running".
        if not mission_status:
            active_mission_id = _text(
                conversation.get("active_mission_id") or conversation.get("activeMissionId")
            )
            if active_mission_id:
                with self._lock:
                    mission_row = self._conn.execute(
                        "SELECT status FROM team_missions WHERE mission_id = ?",
                        (active_mission_id,),
                    ).fetchone()
                if mission_row is not None:
                    mission_status = _text(_row_value(mission_row, "status", ""))
        active_node_count = int(summary.get("active_node_count") or 0)
        if active_node_run and active_node_count <= 0:
            active_node_count = 1
        terminal = mission_status in _TERMINAL_MISSION_STATUSES
        # A terminal mission's conversation is NEVER running — ignore any lingering
        # active_run / active_node_run / stale active node count (those are zombies
        # from a worker that didn't get to emit its terminal event). This is the
        # robust source of truth and does not depend on a fresh graph reduce.
        running = (not terminal) and (
            bool(active_run)
            or bool(active_node_run)
            or active_node_count > 0
            or mission_status in _RUNNING_MISSION_STATUSES
        )
        waiting_approval = bool(pending_approvals) or mission_status == "waiting_approval"
        projected_state = "waiting_approval" if waiting_approval else "running" if running else (
            "completed" if mission_status == "completed"
            else "failed" if mission_status == "failed"
            else "cancelled" if mission_status in {"cancelled", "canceled", "interrupted"}
            else "idle"
        )
        projected_active_run = active_run or active_node_run
        run_updated_at = max(
            float((active_run or {}).get("updated_at") or 0),
            float((active_node_run or {}).get("updated_at") or 0),
        )
        last_message_at = summary.get("last_message_at") or 0
        updated_at = max(
            float(conversation.get("updated_at") or 0),
            float(last_message_at or 0),
            float(run_updated_at or 0),
        )
        projection = {
            **conversation,
            "conversation_id": conversation_id,
            "stable_session_id": stable_session_id,
            "team_id": _text(conversation.get("team_id")),
            "active_mission_id": _text(summary.get("active_mission_id") or conversation.get("active_mission_id")),
            "activeMissionId": _text(summary.get("active_mission_id") or conversation.get("active_mission_id")),
            "mission_status": mission_status,
            "status": mission_status or _text(conversation.get("status")),
            "running": running,
            "run_state": projected_state,
            "activity_state": projected_state,
            "waiting_approval": waiting_approval,
            "pending_approval_count": len(pending_approvals),
            "pending_approvals": pending_approvals,
            "active_run_id": _text(projected_active_run.get("run_id")) if running and projected_active_run else "",
            "active_turn_id": _text(projected_active_run.get("turn_id")) if running and projected_active_run else "",
            "active_runtime_session_id": _text(projected_active_run.get("runtime_session_id")) if running and projected_active_run else "",
            "runtime_scope_key": _text(projected_active_run.get("runtime_scope_key")) if running and projected_active_run else "",
            "run_started_at": projected_active_run.get("started_at") or 0 if running and projected_active_run else 0,
            "run_updated_at": run_updated_at,
            "active_node_count": active_node_count,
            "task_frames": list(summary.get("task_frames") or []),
            "task_frame_count": int(summary.get("task_frame_count") or 0),
            "active_task_frame": summary.get("active_task_frame") if isinstance(summary.get("active_task_frame"), dict) else {},
            "run_session_ids": list(summary.get("run_session_ids") or []),
            "last_message": summary.get("last_message") if isinstance(summary.get("last_message"), dict) else {},
            "last_message_preview": _text(summary.get("last_message_preview")),
            "last_message_at": last_message_at,
            "final_deliverables": list(summary.get("final_deliverables") or []),
            "artifact_refs": list(summary.get("artifact_refs") or []),
            "message_count": int(conversation.get("message_count") or 0) or self._team_mission_conversation_message_count(stable_session_id),
            "updated_at": updated_at,
        }
        return projection

    def _team_mission_conversation_status_event(
        self,
        *,
        mission_id: str,
        source_event: Dict[str, Any],
        source_seq: int,
        projection_seq: int,
    ) -> Dict[str, Any]:
        mission_id = _text(mission_id)
        if not mission_id:
            return {}
        with self._lock:
            mission = self._team_mission_from_row(self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())
        conversation_id = _text((mission or {}).get("conversation_id"))
        if not conversation_id:
            return {}
        projection = self.get_team_mission_conversation_status_projection(conversation_id)
        if not projection:
            return {}
        stable_session_id = _text(projection.get("stable_session_id"))
        source_type = _text(source_event.get("type"))
        timestamp = float(source_event.get("timestamp") or time.time())
        payload = {
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
            "stable_session_id": stable_session_id,
            "stableSessionId": stable_session_id,
            "mission_id": mission_id,
            "missionId": mission_id,
            "active_mission_id": _text(projection.get("active_mission_id")) or mission_id,
            "activeMissionId": _text(projection.get("active_mission_id")) or mission_id,
            "source_event_type": source_type,
            "sourceEventType": source_type,
            "source_seq": source_seq,
            "sourceSeq": source_seq,
            "team_mission_event_seq": projection_seq,
            "teamMissionEventSeq": projection_seq,
            "projection": projection,
            "conversation": projection,
        }
        return {
            "type": _TEAM_MISSION_CONVERSATION_STATUS_EVENT_TYPE,
            "seq": projection_seq,
            "source_seq": source_seq,
            "team_mission_event_seq": projection_seq,
            "timestamp": timestamp,
            "mission_id": mission_id,
            "missionId": mission_id,
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
            "stable_session_id": stable_session_id,
            "stableSessionId": stable_session_id,
            "payload": payload,
        }

    def rename_team_mission_conversation(self, identifier: str, title: str) -> Dict[str, Any]:
        return _rename_team_mission_conversation(self, identifier, title)

    def delete_team_mission_conversation(self, identifier: str) -> Dict[str, Any]:
        return _delete_team_mission_conversation(self, identifier)

    def upsert_team_mission(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        mode: str = "supervised_mission",
        status: str = "planning",
        leader_session_id: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
        completed_at: float | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        now = time.time()
        created = float(created_at or now)
        updated = float(updated_at or now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, created_at, conversation_id FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            candidate_stable_session_id = _stable_session_id_from_metadata(
                merged_metadata,
                _text(leader_session_id or team_id or mission_id),
            )
            conversation_by_session = conn.execute(
                "SELECT conversation_id FROM team_mission_conversations WHERE stable_session_id = ?",
                (candidate_stable_session_id,),
            ).fetchone() if candidate_stable_session_id else None
            resolved_conversation_id = (
                _text(conversation_id)
                or _text(_row_value(existing, "conversation_id", ""))
                or _conversation_id_from_metadata(merged_metadata)
                or _text(_row_value(conversation_by_session, "conversation_id", ""))
                or mission_id
            )
            if resolved_conversation_id:
                merged_metadata["conversation_id"] = resolved_conversation_id
            conn.execute(
                """
                INSERT INTO team_missions (
                    mission_id, conversation_id, team_id, title, objective, workspace_id, workspace_path,
                    mode, status, leader_session_id, created_at, updated_at, completed_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    conversation_id = COALESCE(NULLIF(excluded.conversation_id, ''), conversation_id),
                    team_id = excluded.team_id,
                    title = excluded.title,
                    objective = excluded.objective,
                    workspace_id = excluded.workspace_id,
                    workspace_path = excluded.workspace_path,
                    mode = excluded.mode,
                    status = excluded.status,
                    leader_session_id = COALESCE(NULLIF(excluded.leader_session_id, ''), leader_session_id),
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    mission_id,
                    resolved_conversation_id,
                    str(team_id or ""),
                    str(title or ""),
                    str(objective or ""),
                    str(workspace_id or ""),
                    str(workspace_path or ""),
                    str(mode or "supervised_mission"),
                    str(status or "planning"),
                    str(leader_session_id or ""),
                    float(_row_value(existing, "created_at", created) or created),
                    updated,
                    completed_at,
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                ),
            )
            return self._team_mission_from_row(conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()) or {}

        mission = self._execute_write(_do)
        if mission:
            self.ensure_team_mission_conversation(mission=mission)
            refreshed = self.get_team_mission_graph(mission_id).get("mission")
            return refreshed or mission
        return mission

    def initialize_team_mission_from_strategy(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        mode: str = "supervised_mission",
        members: List[Dict[str, Any]] | None = None,
        graph_payload: Dict[str, Any] | None = None,
        leader_session_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create a Team Mission through the Hermes mode strategy contract.

        This is the native entry point for product modes. Callers provide the
        requested mode and optional manual graph payload; Hermes chooses the
        initial graph/status through ``TeamMissionModeStrategy`` and persists
        it in the Team Mission tables.
        """
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        strategy = strategy_for_mode(mode)
        patch = strategy.initialize_graph(
            mission_id=mission_id,
            title=str(title or ""),
            objective=str(objective or ""),
            members=members or [],
            graph_payload=graph_payload or {},
        )
        mission_metadata = _mission_metadata_with_members({
            "mode_strategy": strategy.mode,
            "start_leader": patch.start_leader,
            "auto_start_ready_nodes": patch.auto_start_ready_nodes,
            "requires_whole_graph_approval": patch.requires_whole_graph_approval,
            **(patch.metadata or {}),
        }, members)
        if isinstance(metadata, dict):
            mission_metadata.update(metadata)
            mission_metadata = _mission_metadata_with_members(mission_metadata, members)
        self.upsert_team_mission(
            mission_id=mission_id,
            conversation_id=conversation_id,
            team_id=team_id,
            title=title,
            objective=objective,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            mode=strategy.mode,
            status=patch.mission_status,
            leader_session_id=leader_session_id,
            metadata=mission_metadata,
        )
        for node in patch.nodes:
            self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=node.node_id,
                kind=node.kind,
                title=node.title,
                objective=node.objective,
                status=node.status,
                assignee_profile_id=node.assignee_profile_id,
                assignee_profile_version_id=node.assignee_profile_version_id,
                runtime_scope_key=node.runtime_scope_key,
                output_contract=node.output_contract,
                metadata=node.metadata,
                position_x=node.position_x,
                position_y=node.position_y,
            )
        for edge in patch.edges:
            self.upsert_team_mission_edge(
                mission_id=mission_id,
                edge_id=edge.edge_id,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                kind=edge.kind,
                metadata=edge.metadata,
            )
        return self.get_team_mission_graph(mission_id)

    def upsert_team_mission_node(
        self,
        *,
        mission_id: str,
        node_id: str,
        kind: str = "worker",
        title: str = "",
        objective: str = "",
        status: str = "todo",
        assignee_profile_id: str = "",
        assignee_profile_version_id: str = "",
        canonical_node_id: str = "",
        task_frame_id: str = "",
        runtime_stable_session_id: str = "",
        runtime_session_id: str = "",
        runtime_scope_key: str = "",
        output_contract: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
        position_x: float = 0,
        position_y: float = 0,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        now = time.time()
        created = float(created_at or now)
        updated = float(updated_at or now)
        raw_kind = str(kind or "worker")
        canonical_kind = _normalize_node_kind(raw_kind)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata = _metadata_with_normalized_node_kind(
                merged_metadata if isinstance(merged_metadata, dict) else {},
                raw_kind=raw_kind,
                canonical_kind=canonical_kind,
            )
            mission_row = conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            mission_metadata = _json_loads(_row_value(mission_row, "metadata_json", ""), {})
            leader_row = conn.execute(
                """
                SELECT * FROM team_mission_nodes
                 WHERE mission_id = ? AND kind = ?
                 ORDER BY created_at ASC
                 LIMIT 1
                """,
                (mission_id, "root"),
            ).fetchone()
            existing_node = self._team_mission_node_from_row(existing) or {}
            leader_node = self._team_mission_node_from_row(leader_row) or {}
            resolved_profile_id, resolved_profile_version_id, resolved_runtime_scope_key, resolved_metadata = _resolve_node_assignee(
                mission_id=mission_id,
                node_id=node_id,
                kind=canonical_kind,
                incoming_profile_id=str(assignee_profile_id or ""),
                incoming_profile_version_id=str(assignee_profile_version_id or ""),
                incoming_runtime_scope_key=str(runtime_scope_key or ""),
                metadata=merged_metadata if isinstance(merged_metadata, dict) else {},
                mission_metadata=mission_metadata if isinstance(mission_metadata, dict) else {},
                existing_node=existing_node,
                leader_node=leader_node,
            )
            existing_canonical_node_id = _text(_row_value(existing, "canonical_node_id", ""))
            existing_task_frame_id = _text(_row_value(existing, "task_frame_id", ""))
            existing_runtime_stable_session_id = _text(_row_value(existing, "runtime_stable_session_id", ""))
            existing_runtime_session_id = _text(_row_value(existing, "runtime_session_id", ""))
            effective_canonical_node_id = (
                _text(canonical_node_id)
                or existing_canonical_node_id
                or _conversation_graph_node_id(mission_id, node_id)
            )
            effective_task_frame_id = (
                _text(task_frame_id)
                or existing_task_frame_id
                or (f"mission-frame:{mission_id}" if mission_id else "")
            )
            effective_runtime_stable_session_id = (
                _text(runtime_stable_session_id)
                or existing_runtime_stable_session_id
            )
            effective_runtime_session_id = (
                _text(runtime_session_id)
                or existing_runtime_session_id
            )
            conn.execute(
                """
                INSERT INTO team_mission_nodes (
                    node_id, mission_id, kind, title, objective, status,
                    assignee_profile_id, assignee_profile_version_id,
                    canonical_node_id, task_frame_id, runtime_stable_session_id,
                    runtime_session_id, runtime_scope_key,
                    output_contract_json, metadata_json, position_x, position_y,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id, node_id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    objective = excluded.objective,
                    status = excluded.status,
                    assignee_profile_id = excluded.assignee_profile_id,
                    assignee_profile_version_id = excluded.assignee_profile_version_id,
                    canonical_node_id = excluded.canonical_node_id,
                    task_frame_id = excluded.task_frame_id,
                    runtime_stable_session_id = excluded.runtime_stable_session_id,
                    runtime_session_id = excluded.runtime_session_id,
                    runtime_scope_key = excluded.runtime_scope_key,
                    output_contract_json = excluded.output_contract_json,
                    metadata_json = excluded.metadata_json,
                    position_x = excluded.position_x,
                    position_y = excluded.position_y,
                    updated_at = excluded.updated_at
                """,
                (
                    node_id,
                    mission_id,
                    canonical_kind,
                    str(title or ""),
                    str(objective or ""),
                    str(status or "todo"),
                    resolved_profile_id,
                    resolved_profile_version_id,
                    effective_canonical_node_id,
                    effective_task_frame_id,
                    effective_runtime_stable_session_id,
                    effective_runtime_session_id,
                    resolved_runtime_scope_key,
                    _json_dumps(output_contract or {}),
                    _json_dumps(resolved_metadata),
                    float(position_x or 0),
                    float(position_y or 0),
                    float(_row_value(existing, "created_at", created) or created),
                    updated,
                ),
            )
            return self._team_mission_node_from_row(conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def get_team_mission_node(self, mission_id: str, node_id: str) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            mission_row = self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            leader_row = self._conn.execute(
                """
                SELECT * FROM team_mission_nodes
                 WHERE mission_id = ? AND kind = ?
                 ORDER BY created_at ASC
                 LIMIT 1
                """,
                (mission_id, "root"),
            ).fetchone()
            binding_row = self._conn.execute(
                """
                SELECT *
                FROM team_mission_run_bindings
                WHERE mission_id = ? AND node_id = ?
                ORDER BY updated_at DESC, created_at DESC, run_id DESC
                LIMIT 1
                """,
                (mission_id, node_id),
            ).fetchone()
        mission = self._team_mission_from_row(mission_row) or {}
        resolved_node = self._team_mission_node_with_resolved_assignee(
            self._team_mission_node_from_row(row) or {},
            mission_metadata=dict(mission.get("metadata") or {}),
            leader_node=self._team_mission_node_from_row(leader_row) or {},
        )
        return self._team_mission_node_with_runtime_binding(
            resolved_node,
            self._team_mission_run_binding_from_row(binding_row) or {},
        )

    def claim_team_mission_node_start(
        self,
        *,
        mission_id: str,
        node_id: str,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Atomically claim a ready node before submitting its runtime run."""
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            node = self._team_mission_node_from_row(row)
            if not node:
                return {}
            if str(node.get("status") or "") != "ready":
                return node
            merged_metadata = dict(node.get("metadata") or {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata["start_claimed_at"] = now
            cursor = conn.execute(
                """
                UPDATE team_mission_nodes
                   SET status = ?,
                       metadata_json = ?,
                       updated_at = ?
                 WHERE mission_id = ?
                   AND node_id = ?
                   AND status = ?
                """,
                (
                    "starting",
                    _json_dumps(merged_metadata),
                    now,
                    mission_id,
                    node_id,
                    "ready",
                ),
            )
            if cursor.rowcount <= 0:
                return self._team_mission_node_from_row(conn.execute(
                    "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                    (mission_id, node_id),
                ).fetchone()) or {}
            return self._team_mission_node_from_row(conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def upsert_team_mission_edge(
        self,
        *,
        mission_id: str,
        from_node_id: str,
        to_node_id: str,
        edge_id: str = "",
        kind: str = "depends_on",
        metadata: Dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        from_node_id = str(from_node_id or "").strip()
        to_node_id = str(to_node_id or "").strip()
        normalized_edge_id = str(edge_id or f"{mission_id}:{from_node_id}:{to_node_id}:{kind}").strip()
        if not mission_id or not from_node_id or not to_node_id:
            return {}
        created = float(created_at or time.time())

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT INTO team_mission_edges (
                    edge_id, mission_id, from_node_id, to_node_id, kind, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    from_node_id = excluded.from_node_id,
                    to_node_id = excluded.to_node_id,
                    kind = excluded.kind,
                    metadata_json = excluded.metadata_json
                """,
                (
                    normalized_edge_id,
                    mission_id,
                    from_node_id,
                    to_node_id,
                    str(kind or "depends_on"),
                    _json_dumps(metadata or {}),
                    created,
                ),
            )
            return self._team_mission_edge_from_row(conn.execute(
                "SELECT * FROM team_mission_edges WHERE edge_id = ?",
                (normalized_edge_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def apply_team_mission_strategy_actions(
        self,
        *,
        mission_id: str,
        actions: TeamMissionStrategyActions,
        run_id: str = "",
        event_source: str = "strategy",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        if actions.mission_status:
            self.upsert_team_mission(
                mission_id=mission_id,
                team_id=str(mission.get("team_id") or ""),
                title=str(mission.get("title") or ""),
                objective=str(mission.get("objective") or ""),
                workspace_id=str(mission.get("workspace_id") or ""),
                workspace_path=str(mission.get("workspace_path") or ""),
                mode=str(mission.get("mode") or ""),
                status=actions.mission_status,
                leader_session_id=str(mission.get("leader_session_id") or ""),
                metadata=dict(mission.get("metadata") or {}),
            )
        created_nodes = []
        for node in actions.nodes:
            created_nodes.append(
                self.upsert_team_mission_node(
                    mission_id=mission_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    title=node.title,
                    objective=node.objective,
                    status=node.status,
                    assignee_profile_id=node.assignee_profile_id,
                    assignee_profile_version_id=node.assignee_profile_version_id,
                    runtime_scope_key=node.runtime_scope_key,
                    output_contract=node.output_contract,
                    metadata=node.metadata,
                    position_x=node.position_x,
                    position_y=node.position_y,
                )
            )
        created_edges = []
        for edge in actions.edges:
            created_edges.append(
                self.upsert_team_mission_edge(
                    mission_id=mission_id,
                    edge_id=edge.edge_id,
                    from_node_id=edge.from_node_id,
                    to_node_id=edge.to_node_id,
                    kind=edge.kind,
                    metadata=edge.metadata,
                )
            )
        if run_id:
            for event in actions.events:
                if isinstance(event, dict):
                    self.append_team_mission_run_event(
                        mission_id=mission_id,
                        run_id=run_id,
                        event=event,
                    )
            self.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=run_id,
                event={
                    "type": "mission.strategy.actions",
                    "payload": {
                        "source": event_source,
                        "mission_status": actions.mission_status,
                        "nodes": created_nodes,
                        "edges": created_edges,
                        "start_node_ids": list(actions.start_node_ids),
                        "approval_requests": list(actions.approval_requests),
                        "auto_start_ready_nodes": actions.auto_start_ready_nodes,
                    },
                },
            )
        return self.get_team_mission_graph(mission_id)

    def _approval_actions_with_leader_assignee(
        self,
        mission_id: str,
        actions: TeamMissionStrategyActions,
    ) -> TeamMissionStrategyActions:
        """Stamp approval-gate node specs with the resolved leader assignee.

        The mode strategy creates the approval gate without an assignee. This
        backfills the leader/root node's already-resolved assignee (profile +
        member id + display name) onto each approval-gate spec so the approval
        node is owned by the real leader instead of the synthetic "Leader"
        placeholder when the mission members list is momentarily unavailable.
        """
        if not any(_normalize_node_kind(node.kind) == "approval_gate" for node in actions.nodes):
            return actions
        graph = self.get_team_mission_graph(mission_id)
        graph_nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        leader_node = next(
            (
                node
                for node in graph_nodes
                if isinstance(node, dict) and _normalize_node_kind(node.get("kind")) == "root"
            ),
            None,
        )
        if not isinstance(leader_node, dict):
            return actions
        leader_profile_id = _text(leader_node.get("assignee_profile_id"))
        leader_profile_version_id = _text(leader_node.get("assignee_profile_version_id"))
        leader_metadata = leader_node.get("metadata") if isinstance(leader_node.get("metadata"), dict) else {}
        leader_member_id = _text(
            leader_metadata.get("assignee_member_id") or leader_metadata.get("assigneeMemberId")
        )
        # The synthetic placeholder owner uses member_id == "leader"; never
        # propagate it as if it were a real member.
        if leader_member_id == "leader" and not leader_profile_id:
            leader_member_id = ""
        leader_display_name = _text(
            leader_metadata.get("assignee_display_name") or leader_metadata.get("assigneeDisplayName")
        )
        # Nothing real to inherit (mission has no resolvable leader); leave the
        # spec untouched so existing fallback resolution still applies.
        if not leader_profile_id and not leader_member_id:
            return actions

        def _with_leader(node: TeamMissionNodeSpec) -> TeamMissionNodeSpec:
            if _normalize_node_kind(node.kind) != "approval_gate":
                return node
            metadata = dict(node.metadata or {})
            metadata.setdefault("role", "leader")
            metadata.setdefault("phase", "approval")
            if leader_member_id:
                metadata["assignee_member_id"] = leader_member_id
                metadata["assigneeMemberId"] = leader_member_id
            if leader_display_name:
                metadata.setdefault("assignee_display_name", leader_display_name)
                metadata.setdefault("assigneeDisplayName", leader_display_name)
            return replace(
                node,
                assignee_profile_id=node.assignee_profile_id or leader_profile_id,
                assignee_profile_version_id=node.assignee_profile_version_id or leader_profile_version_id,
                metadata=metadata,
            )

        def _with_leader_event(event: Dict[str, Any]) -> Dict[str, Any]:
            # The strategy builds the mission.approval.requested event before the
            # leader assignee is known (the mode has no DB access), so the LIVE event
            # carries no owner and the frontend renders the approval node with the
            # generic "Leader" placeholder until a graph reload picks up the stamped
            # node. Copy the resolved leader assignee into the event payload so the
            # live approval node shows the real leader member immediately.
            if not isinstance(event, dict) or _text(event.get("type")) != "mission.approval.requested":
                return event
            payload = dict(event.get("payload") or {})
            if leader_member_id:
                payload.setdefault("assignee_member_id", leader_member_id)
                payload.setdefault("assigneeMemberId", leader_member_id)
            if leader_profile_id:
                payload.setdefault("assignee_profile_id", leader_profile_id)
                payload.setdefault("agent_profile_id", leader_profile_id)
                payload.setdefault("agentProfileId", leader_profile_id)
            if leader_profile_version_id:
                payload.setdefault("assignee_profile_version_id", leader_profile_version_id)
            if leader_display_name:
                payload.setdefault("assignee_display_name", leader_display_name)
                payload.setdefault("assigneeDisplayName", leader_display_name)
            return {**event, "payload": payload}

        return replace(
            actions,
            nodes=tuple(_with_leader(node) for node in actions.nodes),
            events=tuple(_with_leader_event(event) for event in actions.events),
        )

    def complete_team_mission_plan(
        self,
        *,
        mission_id: str,
        run_id: str = "",
        task_id: str = "",
        event_source: str = "plan.complete",
    ) -> Dict[str, Any]:
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        strategy = strategy_for_mode(str(mission.get("mode") or "supervised_mission"))
        normalized_task_id = _text(task_id)
        if not normalized_task_id and run_id:
            binding = self.get_team_mission_run_binding(run_id)
            bound_node = self.get_team_mission_node(
                str(binding.get("mission_id") or mission_id),
                str(binding.get("node_id") or ""),
            )
            normalized_task_id = _task_id_from_node_and_binding(bound_node, binding)
        planned_nodes = tuple(
            _node_spec_from_graph_node(node)
            for node in graph.get("nodes", [])
            if isinstance(node, dict)
            and str(node.get("kind") or "") not in {"root", "approval_gate"}
            and _node_matches_task(node, normalized_task_id)
        )
        planned_node_ids = {
            node.node_id
            for node in planned_nodes
            if node.node_id
        }
        planned_edges = tuple(
            _edge_spec_from_graph_edge(edge)
            for edge in graph.get("edges", [])
            if isinstance(edge, dict)
            and _edge_matches_task(edge, planned_node_ids, normalized_task_id)
        )
        actions = strategy.on_plan_completed(
            mission_id=mission_id,
            planned_nodes=planned_nodes,
            planned_edges=planned_edges,
        )
        actions = _strategy_actions_with_task_id(actions, normalized_task_id)
        # Approval-gate nodes are leader-owned. The mode strategy has no DB
        # access, so it cannot stamp the real leader assignee on the approval
        # node and leaves it blank. If we persist it blank and the mission
        # members list is not resolvable at that instant, assignee resolution
        # falls back to the synthetic "Leader" placeholder and the approval node
        # appears undispatched. Seed the approval node spec with the leader/root
        # node's already-resolved assignee so it is owned by the real leader.
        actions = self._approval_actions_with_leader_assignee(mission_id, actions)
        updated_graph = self.apply_team_mission_strategy_actions(
            mission_id=mission_id,
            actions=actions,
            run_id=run_id,
            event_source=event_source,
        )
        reduced = self.reduce_team_mission_graph(mission_id)
        return {
            "mission_id": mission_id,
            "mission_status": actions.mission_status,
            "approval_requests": list(actions.approval_requests),
            "auto_start_ready_nodes": actions.auto_start_ready_nodes,
            "actions": actions,
            "graph": reduced.get("graph") if isinstance(reduced, dict) and reduced.get("graph") else updated_graph,
        }

    def reject_team_mission_plan(
        self,
        *,
        mission_id: str,
        task_id: str = "",
        rejected_by: str = "",
        reason: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        normalized_task_id = _text(task_id)
        if not normalized_task_id:
            root_nodes = [
                node for node in nodes
                if str(node.get("kind") or "") == "root"
                and _task_id_from_node_and_binding(node)
            ]
            root_nodes.sort(key=lambda node: float(node.get("created_at") or node.get("updated_at") or 0))
            normalized_task_id = _task_id_from_node_and_binding(root_nodes[-1]) if root_nodes else ""
        selected_nodes = [
            node for node in nodes
            if (
                normalized_task_id
                and _task_id_from_node_and_binding(node) == normalized_task_id
            )
        ]
        if not selected_nodes:
            selected_nodes = [
                node for node in nodes
                if str(node.get("kind") or "") in {"root", "approval_gate"}
                or str(node.get("status") or "") in {"ready", "todo", "waiting_dependency", "blocked_waiting_dependency", "waiting_approval"}
            ]
        canceled_nodes = []
        for node in selected_nodes:
            canceled_nodes.append(self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=str(node.get("node_id") or ""),
                kind=str(node.get("kind") or "worker"),
                title=str(node.get("title") or ""),
                objective=str(node.get("objective") or ""),
                status="cancelled",
                assignee_profile_id=str(node.get("assignee_profile_id") or ""),
                assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
                runtime_scope_key=str(node.get("runtime_scope_key") or ""),
                output_contract=dict(node.get("output_contract") or {}),
                metadata={
                    **dict(node.get("metadata") or {}),
                    "rejected_by": _text(rejected_by),
                    "rejected_reason": _text(reason),
                    **({"task_id": normalized_task_id} if normalized_task_id else {}),
                },
                position_x=float(node.get("position_x") or 0),
                position_y=float(node.get("position_y") or 0),
            ))
        self.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="draft",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata=dict(mission.get("metadata") or {}),
        )
        event = {
            "type": "mission.plan.rejected",
            "payload": {
                "task_id": normalized_task_id,
                "node_ids": [str(node.get("node_id") or "") for node in canceled_nodes],
                "rejected_by": _text(rejected_by),
                "reason": _text(reason),
            },
        }
        if run_id:
            self.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=run_id,
                event=event,
            )
        return {
            "mission_id": mission_id,
            "task_id": normalized_task_id,
            "canceled_nodes": canceled_nodes,
            "graph": self.get_team_mission_graph(mission_id),
        }

    def cancel_team_mission(
        self,
        *,
        mission_id: str,
        canceled_by: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        mission_status = _text(mission.get("status")).lower()
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        bindings = [binding for binding in graph.get("run_bindings", []) if isinstance(binding, dict)]
        canceled_at = time.time()

        # Safety reaper: re-read the *current* run status for EVERY run bound to
        # this mission (not just a stale graph snapshot) and force any run that
        # is not already terminal to a terminal status in the control-plane DB.
        # This guarantees no member-node worker run can survive a mission cancel
        # as a zombie "running" run, even if the scheduler started it around or
        # after the cancel.
        cancel_run_bindings: list[Dict[str, Any]] = []
        active_run_ids: set[str] = set()
        for binding in bindings:
            run_id = _text(binding.get("run_id"))
            if not run_id or run_id in active_run_ids:
                continue
            run = self.get_run(run_id) if hasattr(self, "get_run") else None
            run_status = _text((run or {}).get("status")).lower()
            if run and run_status not in _TERMINAL_RUN_STATUSES:
                active_run_ids.add(run_id)
                cancel_run_bindings.append(binding)
                if hasattr(self, "upsert_run"):
                    # Reap the run to a terminal status so the control-plane DB
                    # can never report it as 'running' after a cancel. The
                    # gateway still issues run.cancel for live worker
                    # termination; this is the durable backstop.
                    self.upsert_run(
                        run_id=run_id,
                        session_id=_text(run.get("session_id")) or _text(binding.get("session_id")),
                        runtime_scope_key=_text(run.get("runtime_scope_key")) or _text(binding.get("runtime_scope_key")),
                        turn_id=_text(run.get("turn_id")),
                        runtime_session_id=_text(run.get("runtime_session_id")) or _text(binding.get("runtime_session_id")),
                        status="cancelled",
                        completed_at=canceled_at,
                        metadata={
                            "cancelled_by": _text(canceled_by) or "team_mission.cancel",
                            "cancel_reason": _text(reason),
                            "cancelled_mission_id": mission_id,
                        },
                    )

        if mission_status in _TERMINAL_MISSION_STATUSES:
            # Mission is already terminal, but we still return (and have just
            # reaped) any runs that were left non-terminal so the gateway can
            # terminate the live worker runs and clear the zombie state.
            return {
                "mission_id": mission_id,
                "mission_status": mission_status or "cancelled",
                "canceled_nodes": [],
                "cancel_run_bindings": cancel_run_bindings,
                "graph": self.get_team_mission_graph(mission_id),
            }
        cancellation_metadata = {
            "canceled_by": _text(canceled_by),
            "cancel_reason": _text(reason),
            "canceled_at": canceled_at,
        }
        canceled_nodes = []
        canceled_node_ids: set[str] = set()
        for node in nodes:
            node_id = _text(node.get("node_id"))
            node_status = _text(node.get("status")).lower()
            if not node_id or node_status in _TERMINAL_NODE_STATUSES:
                continue
            if node_status not in _CANCELLABLE_NODE_STATUSES and node_status:
                continue
            canceled_node_ids.add(node_id)
            canceled_nodes.append(self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=node_id,
                kind=str(node.get("kind") or "worker"),
                title=str(node.get("title") or ""),
                objective=str(node.get("objective") or ""),
                status="cancelled",
                assignee_profile_id=str(node.get("assignee_profile_id") or ""),
                assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
                runtime_scope_key=str(node.get("runtime_scope_key") or ""),
                output_contract=dict(node.get("output_contract") or {}),
                metadata={
                    **dict(node.get("metadata") or {}),
                    **cancellation_metadata,
                },
                position_x=float(node.get("position_x") or 0),
                position_y=float(node.get("position_y") or 0),
            ))
        for binding in bindings:
            run_id = _text(binding.get("run_id"))
            node_id = _text(binding.get("node_id"))
            if not run_id:
                continue
            if run_id in active_run_ids or node_id in canceled_node_ids:
                if not any(_text(item.get("run_id")) == run_id for item in cancel_run_bindings):
                    cancel_run_bindings.append(binding)
                if run_id not in active_run_ids and hasattr(self, "get_run") and hasattr(self, "upsert_run"):
                    # Reap runs surfaced only via a cancelled node (not seen in
                    # the first status sweep) so they cannot stay non-terminal.
                    run = self.get_run(run_id)
                    if run and _text(run.get("status")).lower() not in _TERMINAL_RUN_STATUSES:
                        active_run_ids.add(run_id)
                        self.upsert_run(
                            run_id=run_id,
                            session_id=_text(run.get("session_id")) or _text(binding.get("session_id")),
                            runtime_scope_key=_text(run.get("runtime_scope_key")) or _text(binding.get("runtime_scope_key")),
                            turn_id=_text(run.get("turn_id")),
                            runtime_session_id=_text(run.get("runtime_session_id")) or _text(binding.get("runtime_session_id")),
                            status="cancelled",
                            completed_at=canceled_at,
                            metadata={
                                "cancelled_by": _text(canceled_by) or "team_mission.cancel",
                                "cancel_reason": _text(reason),
                                "cancelled_mission_id": mission_id,
                            },
                        )
        self.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="cancelled",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata={
                **dict(mission.get("metadata") or {}),
                **cancellation_metadata,
            },
        )
        # Cancel does NOT go through reduce_team_mission_graph, so project the now-
        # terminal status onto the conversation's session_index here — otherwise the
        # sidebar keeps showing the cancelled mission as "running" after restart.
        self.update_session_index_for_mission(
            mission_id, status="idle", running=False, waiting_approval=False,
        )
        return {
            "mission_id": mission_id,
            "mission_status": "cancelled",
            "canceled_nodes": canceled_nodes,
            "cancel_run_bindings": cancel_run_bindings,
            "graph": self.get_team_mission_graph(mission_id),
        }

    def reap_terminal_mission_runs(self, mission_id: str) -> int:
        """Force any still-active run bound to an already-terminal mission to a
        terminal status. Stale-run watchdog: a member-node run can be left
        'running' in the control-plane DB after its mission reached a terminal
        state (cancel race, completion without a node terminal event, or a run
        that stalled while its gateway stayed alive — none of which the
        orphaned-run recovery catches, since it only fails runs of a DEAD
        gateway). Scoped to terminal missions, so it never touches a slow or
        approval-waiting run of an active mission. Idempotent; returns the count
        reaped. Call it when a mission is observed (subscribe/status)."""
        mission_id = _text(mission_id)
        if not mission_id or not hasattr(self, "upsert_run"):
            return 0
        with self._lock:
            mission_row = self._conn.execute(
                "SELECT status FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            if mission_row is None:
                return 0
            if _text(_row_value(mission_row, "status", "")).lower() not in _TERMINAL_MISSION_STATUSES:
                return 0
            binding_rows = self._conn.execute(
                """
                SELECT run_id, session_id, runtime_scope_key, runtime_session_id
                FROM team_mission_run_bindings
                WHERE mission_id = ?
                """,
                (mission_id,),
            ).fetchall()
        reaped = 0
        for row in binding_rows:
            run_id = _text(_row_value(row, "run_id", ""))
            if not run_id:
                continue
            run = self.get_run(run_id) if hasattr(self, "get_run") else None
            if not run or _text(run.get("status")).lower() in _TERMINAL_RUN_STATUSES:
                continue
            self.upsert_run(
                run_id=run_id,
                session_id=_text(run.get("session_id")) or _text(_row_value(row, "session_id", "")),
                runtime_scope_key=_text(run.get("runtime_scope_key")) or _text(_row_value(row, "runtime_scope_key", "")),
                turn_id=_text(run.get("turn_id")),
                runtime_session_id=_text(run.get("runtime_session_id")) or _text(_row_value(row, "runtime_session_id", "")),
                status="interrupted",
                completed_at=time.time(),
                error="runtime run reaped: bound team mission already terminal",
                metadata={**dict(run.get("metadata") or {}), "reaped_reason": "terminal_mission_stale_run"},
            )
            reaped += 1
        return reaped

    def prune_team_mission_events(self, mission_id: str) -> int:
        """Drop high-volume streaming delta rows for an already-terminal mission.

        team_mission_events had no retention (unlike run_events), so every
        streamed token delta accumulated forever — the canonical log grew into
        the GBs, which slowed every query and made the concurrent session-list
        loads time out (manifesting as 'lost' running state). Once a mission is
        terminal its per-token deltas are no longer needed: the final text lives
        in the kept message.complete events, and structure/tool boundaries are
        kept too, so reopening a finished mission still renders its result. Only
        prunes terminal missions; active missions are never touched. Idempotent;
        freed pages are reused so growth is capped without a VACUUM."""
        mission_id = _text(mission_id)
        if not mission_id:
            return 0
        with self._lock:
            mission_row = self._conn.execute(
                "SELECT status FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            if mission_row is None:
                return 0
            if _text(_row_value(mission_row, "status", "")).lower() not in _TERMINAL_MISSION_STATUSES:
                return 0
        placeholders = ",".join("?" for _ in _TEAM_MISSION_PRUNABLE_SOURCE_TYPES)

        def _do(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                f"""
                DELETE FROM team_mission_events
                WHERE mission_id = ?
                  AND source_event_type IN ({placeholders})
                """,
                (mission_id, *_TEAM_MISSION_PRUNABLE_SOURCE_TYPES),
            )
            return int(cursor.rowcount or 0)

        return self._execute_write(_do)

    def bind_team_mission_run(
        self,
        *,
        mission_id: str,
        node_id: str,
        run_id: str,
        session_id: str,
        role: str = "worker",
        runtime_session_id: str = "",
        runtime_scope_key: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        run_id = str(run_id or "").strip()
        session_id = str(session_id or "").strip()
        if not mission_id or not run_id or not session_id:
            return {}
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, created_at FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            conn.execute(
                """
                INSERT INTO team_mission_run_bindings (
                    mission_id, node_id, run_id, session_id, runtime_session_id,
                    runtime_scope_key, role, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    node_id = excluded.node_id,
                    session_id = excluded.session_id,
                    runtime_session_id = excluded.runtime_session_id,
                    runtime_scope_key = excluded.runtime_scope_key,
                    role = excluded.role,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    mission_id,
                    str(node_id or ""),
                    run_id,
                    session_id,
                    str(runtime_session_id or ""),
                    str(runtime_scope_key or ""),
                    str(role or "worker"),
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                    float(_row_value(existing, "created_at", now) or now),
                    now,
                ),
            )
            if str(node_id or "").strip():
                canonical_node_id = _conversation_graph_node_id(mission_id, str(node_id or ""))
                task_frame_id = f"mission-frame:{mission_id}" if mission_id else ""
                conn.execute(
                    """
                    UPDATE team_mission_nodes
                       SET canonical_node_id = COALESCE(NULLIF(canonical_node_id, ''), ?),
                           task_frame_id = COALESCE(NULLIF(task_frame_id, ''), ?),
                           runtime_stable_session_id = COALESCE(NULLIF(?, ''), runtime_stable_session_id),
                           runtime_session_id = COALESCE(NULLIF(?, ''), runtime_session_id),
                           runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                           updated_at = ?
                     WHERE mission_id = ?
                       AND node_id = ?
                    """,
                    (
                        canonical_node_id,
                        task_frame_id,
                        session_id,
                        str(runtime_session_id or ""),
                        str(runtime_scope_key or ""),
                        now,
                        mission_id,
                        str(node_id or ""),
                    ),
                )
            return self._team_mission_run_binding_from_row(conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def get_team_mission_run_binding(self, run_id: str) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._team_mission_run_binding_from_row(row) or {}

    def team_mission_run_session_ids(self, session_ids: list[str]) -> set[str]:
        normalized = [str(session_id or "").strip() for session_id in session_ids]
        normalized = [session_id for session_id in normalized if session_id]
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT DISTINCT session_id, runtime_session_id
                FROM team_mission_run_bindings
                WHERE session_id IN ({placeholders})
                   OR runtime_session_id IN ({placeholders})
                """,
                tuple(normalized + normalized),
            ).fetchall()
        requested = set(normalized)
        internal_ids: set[str] = set()
        for row in rows:
            for key in ("session_id", "runtime_session_id"):
                value = str(_row_value(row, key, "") or "").strip()
                if value and value in requested:
                    internal_ids.add(value)
        return internal_ids

    def is_team_mission_run_session(self, session_id: str) -> bool:
        session_id = str(session_id or "").strip()
        return bool(session_id and session_id in self.team_mission_run_session_ids([session_id]))

    def _team_mission_run_has_deliverable_text(self, run_id: str, *, max_seq: int = 0) -> bool:
        run_id = str(run_id or "").strip()
        if not run_id:
            return False
        sql = "SELECT event_type, payload_json, event_json, seq FROM run_events WHERE run_id = ?"
        params: list[Any] = [run_id]
        if max_seq > 0:
            sql += " AND seq <= ?"
            params.append(max_seq)
        sql += " ORDER BY seq ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        for row in rows:
            event_type = _text(_row_value(row, "event_type"))
            payload = _json_loads(_row_value(row, "payload_json", ""), None)
            if not isinstance(payload, dict):
                event = _json_loads(_row_value(row, "event_json", ""), {})
                payload = event.get("payload") if isinstance(event, dict) and isinstance(event.get("payload"), dict) else {}
            if _event_has_deliverable_text(event_type, payload):
                return True
        return False

    def reduce_team_mission_run_event(self, *, run_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            return {}
        binding = self.get_team_mission_run_binding(run_id)
        if not binding:
            return {}
        event_type = str((event or {}).get("type") or "").strip()
        payload = event.get("payload") if isinstance((event or {}).get("payload"), dict) else {}
        next_status = ""
        if event_type == "error":
            next_status = "failed"
        elif event_type == "message.complete":
            status = str(payload.get("status") or "").strip().lower()
            if status in {"cancelled", "canceled"}:
                next_status = "cancelled"
            elif status == "interrupted":
                next_status = "interrupted"
            elif status in {"failed", "error"}:
                if self._team_mission_run_has_deliverable_text(run_id, max_seq=_event_seq(event)):
                    next_status = "completed"
                else:
                    next_status = "failed"
            else:
                next_status = "completed"
        if not next_status:
            return {}
        node = self.get_team_mission_node(str(binding.get("mission_id") or ""), str(binding.get("node_id") or ""))
        if not node:
            return {}
        metadata = dict(node.get("metadata") or {})
        event_seq = _event_seq(event)
        existing_terminal_run_id = str(metadata.get("last_run_id") or "").strip()
        existing_terminal_status = str(
            metadata.get("last_run_terminal_status")
            or node.get("status")
            or ""
        ).strip().lower()
        try:
            existing_terminal_seq = int(metadata.get("last_run_terminal_seq") or 0)
        except (TypeError, ValueError):
            existing_terminal_seq = 0
        if existing_terminal_run_id == run_id and existing_terminal_status in _TERMINAL_NODE_STATUSES:
            if event_seq > 0 and existing_terminal_seq >= event_seq:
                return node
            preferred_status = _prefer_terminal_node_status(existing_terminal_status, next_status)
            if preferred_status != next_status:
                return node
        updated = self.upsert_team_mission_node(
            mission_id=str(binding.get("mission_id") or ""),
            node_id=str(binding.get("node_id") or ""),
            kind=str(node.get("kind") or "worker"),
            title=str(node.get("title") or ""),
            objective=str(node.get("objective") or ""),
            status=next_status,
            assignee_profile_id=str(node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
            runtime_scope_key=str(node.get("runtime_scope_key") or binding.get("runtime_scope_key") or ""),
            output_contract=dict(node.get("output_contract") or {}),
            metadata={
                **metadata,
                "last_run_id": run_id,
                "last_run_terminal_event": event_type,
                "last_run_terminal_status": next_status,
                "last_run_terminal_seq": event_seq,
            },
            position_x=float(node.get("position_x") or 0),
            position_y=float(node.get("position_y") or 0),
        )
        self.reduce_team_mission_graph(str(binding.get("mission_id") or ""))
        compile_mode = "final"
        if next_status in {"cancelled", "interrupted"}:
            compile_mode = "canceled"
        elif next_status in {"failed"}:
            compile_mode = "blocked"
        try:
            self.compile_team_mission_memory(
                mission_id=str(binding.get("mission_id") or ""),
                mode=compile_mode,
                source_run_ids=[run_id],
                emit_event=True,
            )
        except Exception:
            pass
        # Planning lifecycle backstop: a leader planning run that ends WITHOUT calling
        # team_mission_plan_complete (e.g. the model wrote the clarification or the
        # plan as prose and stopped instead of using the clarify/plan_complete tools)
        # would otherwise leave the mission stuck in running/planning with nothing to
        # approve or execute — the conversation list and input box stay "running"
        # forever. If a leader planning node has just terminated and there are still
        # no worker/verifier/synthesis/approval nodes, fail the mission so the UI
        # unblocks. A live clarify keeps the run blocked waiting on the user, so the
        # terminal event never fires and this branch does not run — clarify flows
        # are safe.
        try:
            mission_id_for_check = str(binding.get("mission_id") or "")
            if (
                mission_id_for_check
                and _normalize_node_kind(node.get("kind")) == "root"
                and next_status in {"completed", "failed", "cancelled", "interrupted"}
            ):
                graph_for_check = self.get_team_mission_graph(mission_id_for_check)
                mission_for_check = graph_for_check.get("mission") or {}
                mission_status = _text(mission_for_check.get("status")).lower()
                if mission_status not in _TERMINAL_MISSION_STATUSES and mission_status != "waiting_approval":
                    has_real_node = any(
                        _normalize_node_kind(_n.get("kind")) not in {"root", ""}
                        for _n in (graph_for_check.get("nodes") or [])
                        if isinstance(_n, dict)
                    )
                    if not has_real_node:
                        self.upsert_team_mission(
                            mission_id=mission_id_for_check,
                            team_id=_text(mission_for_check.get("team_id")),
                            title=_text(mission_for_check.get("title")),
                            mode=_text(mission_for_check.get("mode")) or "supervised_mission",
                            status="failed",
                            leader_session_id=_text(mission_for_check.get("leader_session_id")),
                        )
        except Exception:
            pass
        return updated

    def append_team_mission_run_event(
        self,
        *,
        mission_id: str,
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        run_id = str(run_id or "").strip()
        if not mission_id or not run_id:
            return {}
        with self._lock:
            binding = self._conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE mission_id = ? AND run_id = ?",
                (mission_id, run_id),
            ).fetchone()
            mission_row = self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            node_row = self._conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, _row_value(binding, "node_id", "") if binding is not None else ""),
            ).fetchone() if binding is not None else None
        if binding is None:
            return {}
        mission = self._team_mission_from_row(mission_row) or {"mission_id": mission_id}
        node = self._team_mission_node_from_row(node_row) or {}
        binding_value = self._team_mission_run_binding_from_row(binding) or {}
        identity = _team_mission_runtime_event_identity(
            mission=mission,
            node=node,
            binding=binding_value,
        )
        frame = dict(event or {})
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        frame.update({
            "run_id": run_id,
            "session_id": str(frame.get("session_id") or binding["runtime_session_id"] or ""),
            "stored_session_id": str(binding["session_id"] or ""),
            "runtime_scope_key": str(frame.get("runtime_scope_key") or binding["runtime_scope_key"] or binding["session_id"] or ""),
            "payload": payload,
        })
        frame = _runtime_event_with_team_mission_identity(frame, identity)
        prev_projecting = getattr(self, "_team_mission_projecting", False)
        # Guard so the inner append_run_event (and any conversation mirror it
        # triggers) does not re-enter the write-time projection hook; this
        # explicit path performs the canonical projection itself below.
        self._team_mission_projecting = True
        try:
            saved = self.append_run_event(str(binding["session_id"] or ""), frame)
            if (
                isinstance(saved, dict)
                and saved.get("_persistence_disposition") in {"duplicate_terminal", "ignored_after_terminal"}
            ):
                return saved
            source_event = saved or frame
            if _text(frame.get("type")) == "message.delta":
                source_event = dict(frame)
                if isinstance(saved, dict):
                    for key in ("seq", "timestamp", "session_id", "stored_session_id", "runtime_scope_key", "runtime_session_id"):
                        if saved.get(key) is not None and not source_event.get(key):
                            source_event[key] = saved.get(key)
            self._project_team_mission_run_event_locked(
                mission_id=mission_id,
                run_id=run_id,
                binding=binding_value,
                identity=identity,
                source_event=source_event,
            )
            return saved
        finally:
            self._team_mission_projecting = prev_projecting

    def _project_team_mission_run_event_locked(
        self,
        *,
        mission_id: str,
        run_id: str,
        binding: Dict[str, Any],
        identity: Dict[str, str],
        source_event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Canonical projection for one runtime event of a team-mission-bound run.

        Single implementation shared by both the explicit
        ``append_team_mission_run_event`` path and the write-time hook in
        ``append_run_event`` (directly-delivered node events). Callers MUST set
        ``self._team_mission_projecting`` for the duration so the conversation
        mirror's nested ``append_run_event`` does not re-enter the hook.
        """
        mission_event = _event_log.append_team_mission_runtime_event(
            self,
            mission_id=mission_id,
            run_id=run_id,
            source_event=source_event,
            identity=identity,
        )
        self.reduce_team_mission_run_event(run_id=run_id, event=source_event)
        try:
            _mirror_team_mission_event(
                self,
                mission_id=mission_id,
                binding=binding,
                event=source_event,
                source="team_mission_run_event",
            )
        except Exception:
            pass
        if (
            isinstance(mission_event, dict)
            and not mission_event.get("_persistence_disposition")
            and _should_emit_conversation_status_projection(source_event)
        ):
            _event_log.append_team_mission_conversation_status_event(
                self,
                mission_id=mission_id,
                source_event=source_event,
                source_mission_seq=_event_seq(mission_event),
            )
        return mission_event

    def _project_team_mission_run_event(self, *, run_id: str, saved: Dict[str, Any]) -> None:
        """Write-time canonical projection for directly-delivered run events.

        Invoked from ``append_run_event`` for runtime events recorded straight
        onto a node's session (the streaming path that does not go through
        ``append_team_mission_run_event``). Replaces the removed read-time
        ``run_events`` -> mission projection so replay/live share one seq domain.
        """
        run_id = str(run_id or "").strip()
        if not run_id or not isinstance(saved, dict):
            return
        if saved.get("_persistence_disposition") in {
            "duplicate_terminal",
            "ignored_after_terminal",
            "duplicate_mission_event",
        }:
            return
        binding = self.get_team_mission_run_binding(run_id)
        if not binding:
            return
        mission_id = str(binding.get("mission_id") or "").strip()
        if not mission_id:
            return
        node = self.get_team_mission_node(mission_id, str(binding.get("node_id") or "")) or {}
        with self._lock:
            mission_row = self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        mission = self._team_mission_from_row(mission_row) or {"mission_id": mission_id}
        identity = _team_mission_runtime_event_identity(
            mission=mission,
            node=node,
            binding=binding,
        )
        prev_projecting = getattr(self, "_team_mission_projecting", False)
        self._team_mission_projecting = True
        try:
            # Directly-delivered runtime events only need to exist in the
            # canonical log so replay/live share one seq domain (INV-1). Node
            # status reduction, conversation mirroring and status projection are
            # owned by the explicit streaming paths (append_team_mission_run_event
            # and run_control.record_event); doing them here would double-mirror
            # and rewrite conversation stream history.
            _event_log.append_team_mission_runtime_event(
                self,
                mission_id=mission_id,
                run_id=run_id,
                source_event=dict(saved),
                identity=identity,
            )
        finally:
            self._team_mission_projecting = prev_projecting

    def get_team_mission_graph(self, mission_id: str) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        with self._lock:
            mission = self._team_mission_from_row(self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())
            if mission is None:
                return {}
            conversation = self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (_text(mission.get("conversation_id")),),
            ).fetchone()) if _text(mission.get("conversation_id")) else None
            nodes = [
                node for node in (
                    self._team_mission_node_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_nodes WHERE mission_id = ? ORDER BY created_at ASC, node_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if node is not None
            ]
            nodes = self._team_mission_nodes_with_resolved_assignees(
                nodes,
                mission_metadata=dict(mission.get("metadata") or {}),
            )
            edges = [
                edge for edge in (
                    self._team_mission_edge_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_edges WHERE mission_id = ? ORDER BY created_at ASC, edge_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if edge is not None
            ]
            run_bindings = [
                binding for binding in (
                    self._team_mission_run_binding_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_run_bindings WHERE mission_id = ? ORDER BY created_at ASC, run_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if binding is not None
            ]
            nodes = self._team_mission_nodes_with_runtime_bindings(nodes, run_bindings)
        return {
            "mission": mission,
            "conversation": conversation or {},
            "nodes": nodes,
            "edges": edges,
            "run_bindings": run_bindings,
        }

    def get_team_mission_conversation_graph(self, conversation_id: str) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        with self._lock:
            conversation = self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone())
            if conversation is None:
                return {}
            missions = [
                mission for mission in (
                    self._team_mission_from_row(row)
                    for row in self._conn.execute(
                        """
                        SELECT *
                        FROM team_missions
                        WHERE conversation_id = ?
                        ORDER BY created_at ASC, updated_at ASC, mission_id ASC
                        """,
                        (conversation_id,),
                    ).fetchall()
                ) if mission is not None
            ]
        message_page = _conversation_message_page(self, conversation, limit=100)
        if not missions:
            deliverable_projection = self._team_mission_conversation_deliverable_projection(conversation, [])
            return {
                "mission": {},
                "conversation": conversation,
                "nodes": [],
                "edges": [],
                "run_bindings": [],
                "task_frames": [],
                "runs": [],
                "last_message": deliverable_projection.get("last_message") or {},
                "last_message_preview": deliverable_projection.get("last_message_preview") or "",
                "last_message_at": deliverable_projection.get("last_message_at") or 0,
                "final_deliverables": [],
                "artifact_refs": [],
                "recent_messages": list(message_page.get("messages") or []),
                "recentMessages": list(message_page.get("messages") or []),
                "message_page_info": message_page.get("pageInfo") or {},
                "messagePageInfo": message_page.get("pageInfo") or {},
            }

        active_mission_id = _text(conversation.get("active_mission_id"))
        latest_mission = missions[-1]
        active_mission = next(
            (mission for mission in missions if _text(mission.get("mission_id")) == active_mission_id),
            latest_mission,
        )
        deliverable_projection = self._team_mission_conversation_deliverable_projection(conversation, missions)
        deliverables_by_mission = deliverable_projection.get("final_deliverables_by_mission") or {}
        deliverables_by_task = deliverable_projection.get("final_deliverables_by_task") or {}
        artifact_refs_by_mission = deliverable_projection.get("artifact_refs_by_mission") or {}
        artifact_refs_by_task = deliverable_projection.get("artifact_refs_by_task") or {}
        aggregate_nodes: List[Dict[str, Any]] = []
        aggregate_edges: List[Dict[str, Any]] = []
        aggregate_bindings: List[Dict[str, Any]] = []
        task_frames: List[Dict[str, Any]] = []
        runs: List[Dict[str, Any]] = []

        for mission in missions:
            mission_id = _text(mission.get("mission_id"))
            single_graph = self.get_team_mission_graph(mission_id)
            nodes = list(single_graph.get("nodes") or [])
            edges = list(single_graph.get("edges") or [])
            bindings = list(single_graph.get("run_bindings") or [])
            node_ids: List[str] = []
            root_node_id = ""
            for node in nodes:
                original_node_id = _text(node.get("node_id"))
                if not original_node_id:
                    continue
                namespaced_node_id = _conversation_graph_node_id(mission_id, original_node_id)
                metadata = dict(node.get("metadata") or {})
                metadata.setdefault("hermes_mission_id", mission_id)
                metadata.setdefault("hermes_node_id", original_node_id)
                metadata.setdefault("task_id", _task_id_from_mission(mission))
                projected_node = {
                    **node,
                    "node_id": namespaced_node_id,
                    "metadata": metadata,
                }
                aggregate_nodes.append(projected_node)
                node_ids.append(namespaced_node_id)
                if not root_node_id and _normalize_node_kind(_text(node.get("kind"))) == "root":
                    root_node_id = namespaced_node_id
            for edge in edges:
                from_node_id = _text(edge.get("from_node_id"))
                to_node_id = _text(edge.get("to_node_id"))
                if not from_node_id or not to_node_id:
                    continue
                metadata = dict(edge.get("metadata") or {})
                metadata.setdefault("hermes_mission_id", mission_id)
                aggregate_edges.append({
                    **edge,
                    "from_node_id": _conversation_graph_node_id(mission_id, from_node_id),
                    "to_node_id": _conversation_graph_node_id(mission_id, to_node_id),
                    "metadata": metadata,
                })
            for binding in bindings:
                original_node_id = _text(binding.get("node_id"))
                metadata = dict(binding.get("metadata") or {})
                metadata.setdefault("hermes_mission_id", mission_id)
                metadata.setdefault("hermes_node_id", original_node_id)
                aggregate_bindings.append({
                    **binding,
                    "node_id": _conversation_graph_node_id(mission_id, original_node_id) if original_node_id else "",
                    "metadata": metadata,
                })
            task_id = _task_id_from_mission(mission)
            frame_id = f"mission-frame:{mission_id}"
            frame_artifact_refs = _dedupe_artifact_refs([
                *list(artifact_refs_by_mission.get(mission_id, [])),
                *list(artifact_refs_by_task.get((mission_id, task_id), [])),
            ])
            final_deliverable = _final_deliverable_for_frame(
                deliverables_by_mission,
                deliverables_by_task,
                mission_id,
                task_id,
                frame_artifact_refs,
            )
            frame = {
                "id": frame_id,
                "runId": _text(mission.get("leader_session_id")),
                "missionId": mission_id,
                "mission_id": mission_id,
                "taskId": task_id,
                "task_id": task_id,
                "title": _text(mission.get("title")) or _text(conversation.get("title")) or "团队任务",
                "objective": _text(mission.get("objective")) or _text(mission.get("title")) or "团队任务",
                "status": _text(mission.get("status")) or "planning",
                "source": "hermes_conversation",
                "rootNodeId": root_node_id or (node_ids[0] if node_ids else ""),
                "root_node_id": root_node_id or (node_ids[0] if node_ids else ""),
                "nodeIds": node_ids,
                "node_ids": node_ids,
                "createdAt": mission.get("created_at") or 0,
                "created_at": mission.get("created_at") or 0,
                "updatedAt": mission.get("updated_at") or 0,
                "updated_at": mission.get("updated_at") or 0,
                "completedAt": mission.get("completed_at"),
                "completed_at": mission.get("completed_at"),
                "artifactRefs": frame_artifact_refs,
                "artifact_refs": frame_artifact_refs,
            }
            if final_deliverable:
                frame.update({
                    "finalDeliverable": final_deliverable,
                    "final_deliverable": final_deliverable,
                    "deliverableMessageId": final_deliverable.get("messageId") or "",
                    "deliverable_message_id": final_deliverable.get("message_id") or "",
                })
            task_frames.append(frame)
            runs.append({
                "id": frame_id,
                "conversationId": conversation_id,
                "conversation_id": conversation_id,
                **frame,
            })

        return {
            "mission": active_mission,
            "conversation": conversation,
            "nodes": aggregate_nodes,
            "edges": aggregate_edges,
            "run_bindings": aggregate_bindings,
            "task_frames": task_frames,
            "runs": runs,
            "last_message": deliverable_projection.get("last_message") or {},
            "last_message_preview": deliverable_projection.get("last_message_preview") or "",
            "last_message_at": deliverable_projection.get("last_message_at") or 0,
            "final_deliverables": list(deliverable_projection.get("final_deliverables") or []),
            "artifact_refs": list(deliverable_projection.get("artifact_refs") or []),
            "recent_messages": list(message_page.get("messages") or []),
            "recentMessages": list(message_page.get("messages") or []),
            "message_page_info": message_page.get("pageInfo") or {},
            "messagePageInfo": message_page.get("pageInfo") or {},
        }

    def _team_mission_memory_context(self, mission: Dict[str, Any]) -> Dict[str, Any]:
        return _memory_state.team_mission_memory_context(self, mission)

    def upsert_team_mission_memory_item(
        self,
        *,
        memory_id: str = "",
        team_id: str,
        mission_id: str,
        conversation_session_id: str,
        task_id: str = "",
        scope: str = "mission_task",
        kind: str = "summary",
        content: str,
        structured_payload: Dict[str, Any] | None = None,
        source_node_ids: List[str] | None = None,
        source_run_ids: List[str] | None = None,
        artifact_refs: List[Dict[str, Any]] | None = None,
        workspace_refs: List[Dict[str, Any]] | None = None,
        confidence: float = 0.75,
        visibility: str = "team",
        status: str = _MEMORY_COMMITTED_STATUS,
        created_at: float | None = None,
        updated_at: float | None = None,
        invalidated_at: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.upsert_team_mission_memory_item(
            self,
            memory_id=memory_id,
            team_id=team_id,
            mission_id=mission_id,
            conversation_session_id=conversation_session_id,
            task_id=task_id,
            scope=scope,
            kind=kind,
            content=content,
            structured_payload=structured_payload,
            source_node_ids=source_node_ids,
            source_run_ids=source_run_ids,
            artifact_refs=artifact_refs,
            workspace_refs=workspace_refs,
            confidence=confidence,
            visibility=visibility,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            invalidated_at=invalidated_at,
        )

    def list_team_mission_memory_items(
        self,
        *,
        mission_id: str = "",
        conversation_session_id: str = "",
        team_id: str = "",
        task_id: str = "",
        kinds: List[str] | None = None,
        statuses: List[str] | None = None,
        visibility: List[str] | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return _memory_state.list_team_mission_memory_items(
            self,
            mission_id=mission_id,
            conversation_session_id=conversation_session_id,
            team_id=team_id,
            task_id=task_id,
            kinds=kinds,
            statuses=statuses,
            visibility=visibility,
            include_deleted=include_deleted,
            limit=limit,
        )

    def update_team_mission_memory_item(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        structured_payload: Dict[str, Any] | None = None,
        visibility: str | None = None,
        status: str | None = None,
        confidence: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.update_team_mission_memory_item(
            self,
            memory_id,
            content=content,
            structured_payload=structured_payload,
            visibility=visibility,
            status=status,
            confidence=confidence,
        )

    def delete_team_mission_memory_item(self, memory_id: str) -> Dict[str, Any]:
        return _memory_state.delete_team_mission_memory_item(self, memory_id)

    def upsert_team_mission_memory_edge(
        self,
        *,
        from_memory_id: str,
        to_memory_id: str = "",
        relation: str,
        metadata: Dict[str, Any] | None = None,
        edge_id: str = "",
        created_at: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.upsert_team_mission_memory_edge(
            self,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation=relation,
            metadata=metadata,
            edge_id=edge_id,
            created_at=created_at,
        )

    def list_team_mission_memory_edges(
        self,
        *,
        from_memory_id: str = "",
        to_memory_id: str = "",
        relation: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return _memory_state.list_team_mission_memory_edges(
            self,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation=relation,
            limit=limit,
        )

    def compile_team_mission_memory(
        self,
        *,
        mission_id: str,
        task_id: str = "",
        mode: str = "final",
        source_run_ids: List[str] | None = None,
        emit_event: bool = True,
    ) -> Dict[str, Any]:
        return _memory_state.compile_team_mission_memory(
            self,
            mission_id=mission_id,
            task_id=task_id,
            mode=mode,
            source_run_ids=source_run_ids,
            emit_event=emit_event,
        )

    def build_team_mission_memory_pack(
        self,
        *,
        mission_id: str,
        objective: str = "",
        workspace_id: str = "",
        limit: int = 8,
        include_team_scope: bool = False,
    ) -> Dict[str, Any]:
        return _memory_state.build_team_mission_memory_pack(
            self,
            mission_id=mission_id,
            objective=objective,
            workspace_id=workspace_id,
            limit=limit,
            include_team_scope=include_team_scope,
        )

    def build_team_mission_memory_slice(
        self,
        *,
        mission_id: str,
        node_id: str,
        objective: str = "",
        limit: int = 5,
        include_team_scope: bool = False,
    ) -> Dict[str, Any]:
        return _memory_state.build_team_mission_memory_slice(
            self,
            mission_id=mission_id,
            node_id=node_id,
            objective=objective,
            limit=limit,
            include_team_scope=include_team_scope,
        )

    def reduce_team_mission_graph(self, mission_id: str) -> Dict[str, Any]:
        return _graph_state.reduce_team_mission_graph(self, mission_id)

    def _ensure_team_mission_finalizers(
        self,
        *,
        mission: Dict[str, Any],
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return _graph_state.ensure_team_mission_finalizers(
            self,
            mission=mission,
            nodes=nodes,
            edges=edges,
        )

    def append_team_mission_event_for_run(
        self,
        *,
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        return _event_log.append_team_mission_event_for_run(
            self,
            run_id=run_id,
            event=event,
        )

    def append_team_mission_structural_event(
        self,
        *,
        mission_id: str,
        source_event: Dict[str, Any],
        identity: Dict[str, str] | None = None,
        dedupe_key: str = "",
    ) -> Dict[str, Any]:
        return _event_log.append_team_mission_structural_event(
            self,
            mission_id=mission_id,
            source_event=source_event,
            identity=identity,
            dedupe_key=dedupe_key,
        )

    def append_team_mission_conversation_status_event(
        self,
        *,
        mission_id: str,
        source_event: Dict[str, Any],
        source_mission_seq: int,
    ) -> Dict[str, Any]:
        return _event_log.append_team_mission_conversation_status_event(
            self,
            mission_id=mission_id,
            source_event=source_event,
            source_mission_seq=source_mission_seq,
        )

    def list_team_mission_events(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        return _event_log.list_team_mission_events(
            self,
            mission_id,
            after_seq=after_seq,
            limit=limit,
        )

    def list_team_mission_run_events(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return []
        after_seq = int(after_seq or 0)
        # §§5.1 ABI convergence: the canonical team_mission_events log is the
        # single source of truth for replay. Runtime events are appended to it
        # at write time (append_team_mission_run_event), so replay and live
        # share one monotonic per-mission seq domain. The legacy run_events
        # derived projection (rowid * 1e9 + seq) created a second, incompatible
        # seq domain and has been removed (INV-1 / single source of truth).
        return self.list_team_mission_events(
            mission_id,
            after_seq=after_seq,
            limit=limit,
        )
