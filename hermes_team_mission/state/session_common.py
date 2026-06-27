from __future__ import annotations

import json
import logging
import sqlite3
import time

_log = logging.getLogger(__name__)
from dataclasses import replace
from typing import Any, Dict, List, Optional

from hermes_team_mission.domain.utils import MEMORY_COMMITTED_STATUS as _MEMORY_COMMITTED_STATUS
from hermes_team_mission.domain.utils import stable_id as _stable_id
from hermes_team_mission.domain.utils import text as _text
from hermes_team_mission.runtime.failure import classify_team_mission_failure as _classify_team_mission_failure
from hermes_team_mission.runtime.conversation_mirror import mirror_event_to_conversation as _mirror_team_mission_event
from hermes_runtime_event_payloads import primary_deliverable_text
from hermes_team_mission.state.conversation import delete_team_mission_conversation as _delete_team_mission_conversation
from hermes_team_mission.state.conversation import is_placeholder_team_mission_conversation_title as _is_placeholder_team_mission_conversation_title
from hermes_team_mission.state.conversation import is_replaceable_team_mission_conversation_title as _is_replaceable_team_mission_conversation_title
from hermes_team_mission.state.conversation import is_routeable_team_mission_conversation as _conversation_routeable
from hermes_team_mission.state.conversation import team_mission_conversation_message_page as _conversation_message_page
from hermes_team_mission.state.conversation import rename_team_mission_conversation as _rename_team_mission_conversation
from hermes_team_mission.state.conversation import team_mission_conversation_history_sql as _conversation_history_sql
from hermes_team_mission.context.conversation_projection import dedupe_artifact_refs as _dedupe_artifact_refs
from hermes_team_mission.context.conversation_projection import final_deliverable_for_frame as _final_deliverable_for_frame
from hermes_team_mission.context.conversation_projection import final_deliverable_from_message as _final_deliverable_from_message
from hermes_team_mission.context.conversation_projection import final_deliverable_with_artifact_refs as _final_deliverable_with_artifact_refs
from hermes_team_mission.context.conversation_projection import message_summary_from_message as _message_summary_from_message
from hermes_team_mission.context.conversation_projection import message_with_deliverable_artifact_refs as _message_with_deliverable_artifact_refs
import hermes_team_mission.state.memory as _memory_state
import hermes_team_mission.state.graph as _graph_state
import hermes_team_mission.state.event_log as _event_log
import hermes_team_mission.state.deliverables as _deliverable_state
from hermes_team_mission.domain.assignees import assignee_public_fields as _assignee_public_fields
from hermes_team_mission.domain.assignees import mission_metadata_with_members as _mission_metadata_with_members
from hermes_team_mission.domain.assignees import resolve_node_assignee as _resolve_node_assignee
from hermes_team_mission.domain.identities import canonical_node_id as _canonical_node_id
from hermes_team_mission.domain.node_kinds import TEAM_MISSION_CONTROL_NODE_KINDS
from hermes_team_mission.domain.node_kinds import metadata_with_normalized_node_kind as _metadata_with_normalized_node_kind
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind as _normalize_node_kind
from hermes_team_mission.domain.modes import TeamMissionEdgeSpec
from hermes_team_mission.domain.modes import TeamMissionNodeSpec
from hermes_team_mission.domain.modes import TeamMissionStrategyActions
from hermes_team_mission.domain.modes import strategy_for_mode


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
    # Subagent streaming deltas are the dominant canonical-log growth (a worker
    # node streams tens of thousands of these per mission) yet carry no durable
    # info once terminal — the final text is in the kept subagent.complete /
    # message.complete events. They were MISSING from this list, so the prune
    # ran, matched ~nothing, and team_mission_events grew into the GBs (the 2GB
    # DB). Keep structural/milestone events (subagent.start/complete/tool, node.*,
    # message.complete) — only drop the per-token deltas + progress.
    "subagent.output_delta",
    "subagent.reasoning_delta",
    "subagent.thinking",
    "subagent.progress",
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
    "mission.cancelled",
    "mission.status.projected",
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


def _node_requires_explicit_handoff(node: Dict[str, Any] | None) -> bool:
    node = node if isinstance(node, dict) else {}
    output_contract = node.get("output_contract") if isinstance(node.get("output_contract"), dict) else {}
    if output_contract.get("requires_explicit_handoff") is True:
        return True
    if output_contract.get("requiresExplicitHandoff") is True:
        return True
    delivery_channel = _text(output_contract.get("delivery_channel") or output_contract.get("deliveryChannel")).lower()
    if delivery_channel in {"handoff", "internal_handoff"}:
        return True
    return False


def _terminal_run_status_for_event(event_type: str, payload: Dict[str, Any] | None) -> str | None:
    event_type = _text(event_type)
    payload = payload if isinstance(payload, dict) else {}
    if event_type == "error":
        return "failed"
    if event_type != "message.complete":
        return None
    status = _text(payload.get("status")).lower()
    if status == "interrupted":
        return "interrupted"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"error", "failed"}:
        return "failed"
    return "completed"


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
    # CR-P3.3: graph identity only; for speaker use participant_id.
    return _canonical_node_id(mission_id, node_id)


def node_to_participant_id(node: Dict[str, Any] | None) -> str:
    """Return only an explicit participant stamp from node metadata.

    CR-P3.3: graph identity only; for speaker use participant_id. This helper
    deliberately never derives speaker identity from node_id/canonical_node_id.
    """
    if not isinstance(node, dict):
        return ""
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return _text(
        node.get("participant_id")
        or node.get("participantId")
        or metadata.get("participant_id")
        or metadata.get("participantId")
    )


def _participant_id_from_runtime_event_context(
    *,
    mission: Dict[str, Any],
    node: Dict[str, Any],
    binding: Dict[str, Any],
) -> str:
    binding_metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
    mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    run_context = binding_metadata.get("run_context") if isinstance(binding_metadata.get("run_context"), dict) else {}
    if not run_context and _text(binding_metadata.get("run_context_json") or binding_metadata.get("runContextJson")):
        try:
            parsed = json.loads(_text(binding_metadata.get("run_context_json") or binding_metadata.get("runContextJson")))
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        run_context = parsed if isinstance(parsed, dict) else {}
    return _text(
        binding_metadata.get("participant_id")
        or binding_metadata.get("participantId")
        or run_context.get("participant_id")
        or run_context.get("participantId")
        or node_to_participant_id(node)
        or mission_metadata.get("participant_id")
        or mission_metadata.get("participantId")
    )


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
    # CR-P3.3: graph identity only; for speaker use participant_id.
    canonical_id = _canonical_node_id(mission_id, node_id)
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
    participant_id = _participant_id_from_runtime_event_context(
        mission=mission,
        node=node,
        binding=binding,
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
        "canonical_node_id": canonical_id,
        "canonicalNodeId": canonical_id,
        "node_kind": node_kind,
        "nodeKind": node_kind,
        "participant_id": participant_id,
        "participantId": participant_id,
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
    participant_id = _text(
        frame.get("participant_id")
        or frame.get("participantId")
        or payload.get("participant_id")
        or payload.get("participantId")
        or identity.get("participant_id")
        or identity.get("participantId")
    )
    for key, value in identity.items():
        if payload_declares_subject and key in {"node_id", "nodeId"}:
            continue
        if _text(value) and not _text(payload.get(key)):
            payload[key] = value
    if participant_id:
        frame.setdefault("participant_id", participant_id)
        frame.setdefault("participantId", participant_id)
        payload.setdefault("participant_id", participant_id)
        payload.setdefault("participantId", participant_id)
    if source_seq > 0:
        frame["source_seq"] = source_seq
        payload["source_seq"] = source_seq
        payload["sourceSeq"] = source_seq
    if mission_event_seq > 0:
        frame["team_mission_event_seq"] = mission_event_seq
        payload["team_mission_event_seq"] = mission_event_seq
        payload["teamMissionEventSeq"] = mission_event_seq
    for key in (
        "mission_id",
        "conversation_id",
        "stable_session_id",
        "node_id",
        "canonical_node_id",
        "participant_id",
        "task_id",
        "task_frame_id",
    ):
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



# Export underscore-prefixed helpers/constants to the split mixin modules.
__all__ = [name for name in globals() if not name.startswith("__")]
