from __future__ import annotations

from typing import Any, Dict

from hermes_team_mission.domain.utils import text as _text

TEAM_MISSION_EVENT_PROTOCOL = "team_mission.event.v1"
TEAM_MISSION_CONVERSATION_STATUS_KIND = "conversation.status.updated"


def _numeric_projection_value(value: Any) -> Any:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if not parsed:
        return 0
    return int(parsed) if parsed.is_integer() else parsed


def _int_projection_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _slim_result_projection(result: Dict[str, Any] | None) -> Dict[str, Any]:
    result = result if isinstance(result, dict) else {}
    result_id = _text(result.get("result_id") or result.get("resultId"))
    status = _text(result.get("status"))
    outcome = _text(result.get("outcome"))
    if not any((result_id, status, outcome)):
        return {}
    activity_id = _text(result.get("activity_id") or result.get("activityId"))
    return {
        "result_id": result_id,
        "resultId": result_id,
        "status": status,
        "outcome": outcome,
        "activity_id": activity_id,
        "activityId": activity_id,
    }


def slim_team_mission_conversation_status_projection(
    projection: Dict[str, Any],
    *,
    mission_id: str,
) -> Dict[str, Any]:
    projection = projection if isinstance(projection, dict) else {}
    mission_id = _text(mission_id)
    active_mission_id = _text(
        projection.get("active_mission_id") or projection.get("activeMissionId"),
    ) or mission_id
    mission_status = _text(
        projection.get("mission_status") or projection.get("missionStatus") or projection.get("status"),
    )
    run_state = _text(
        projection.get("run_state")
        or projection.get("runState")
        or projection.get("activity_state")
        or projection.get("activityState"),
    )
    activity_state = _text(projection.get("activity_state") or projection.get("activityState") or run_state)
    leader_report_status = _text(projection.get("leader_report_status") or projection.get("leaderReportStatus"))
    leader_report_run_id = _text(projection.get("leader_report_run_id") or projection.get("leaderReportRunId"))
    leader_report_message_id = _text(
        projection.get("leader_report_message_id") or projection.get("leaderReportMessageId"),
    )
    result = _slim_result_projection(
        projection.get("active_result") if isinstance(projection.get("active_result"), dict)
        else projection.get("activeResult") if isinstance(projection.get("activeResult"), dict)
        else {},
    )
    conversation_id = _text(projection.get("conversation_id") or projection.get("conversationId"))
    conversation_session_id = _text(projection.get("conversation_session_id") or projection.get("conversationSessionId"))
    team_id = _text(projection.get("team_id") or projection.get("teamId"))
    workspace_id = _text(projection.get("workspace_id") or projection.get("workspaceId"))
    workspace_path = _text(projection.get("workspace_path") or projection.get("workspacePath"))
    running = bool(projection.get("running"))
    waiting_approval = bool(projection.get("waiting_approval") or projection.get("waitingApproval"))
    pending_approval_count = _int_projection_value(
        projection.get("pending_approval_count") or projection.get("pendingApprovalCount"),
    )
    active_node_count = _int_projection_value(
        projection.get("active_node_count") or projection.get("activeNodeCount"),
    )
    active_run_id = _text(projection.get("active_run_id") or projection.get("activeRunId"))
    active_turn_id = _text(projection.get("active_turn_id") or projection.get("activeTurnId"))
    active_execution_session_id = _text(
        projection.get("active_execution_session_id") or projection.get("activeExecutionSessionId"),
    )
    runtime_scope_key = _text(projection.get("runtime_scope_key") or projection.get("runtimeScopeKey"))
    run_started_at = _numeric_projection_value(projection.get("run_started_at") or projection.get("runStartedAt"))
    run_updated_at = _numeric_projection_value(projection.get("run_updated_at") or projection.get("runUpdatedAt"))
    mission_started_at = _numeric_projection_value(
        projection.get("mission_started_at") or projection.get("missionStartedAt"),
    )
    mission_updated_at = _numeric_projection_value(
        projection.get("mission_updated_at") or projection.get("missionUpdatedAt"),
    )
    mission_completed_at = _numeric_projection_value(
        projection.get("mission_completed_at") or projection.get("missionCompletedAt"),
    )
    message_count = _int_projection_value(projection.get("message_count") or projection.get("messageCount"))
    last_message_preview = _text(projection.get("last_message_preview") or projection.get("lastMessagePreview"))
    last_message_at = _numeric_projection_value(projection.get("last_message_at") or projection.get("lastMessageAt"))
    updated_at = _numeric_projection_value(projection.get("updated_at") or projection.get("updatedAt"))
    return {
        "schema_version": 2,
        "schemaVersion": 2,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        "team_id": team_id,
        "teamId": team_id,
        "title": _text(projection.get("title")),
        "objective": _text(projection.get("objective")),
        "workspace_id": workspace_id,
        "workspaceId": workspace_id,
        "workspace_path": workspace_path,
        "workspacePath": workspace_path,
        "mission_id": mission_id,
        "missionId": mission_id,
        "active_mission_id": active_mission_id,
        "activeMissionId": active_mission_id,
        "mission_status": mission_status,
        "missionStatus": mission_status,
        "status": mission_status,
        "running": running,
        "run_state": run_state,
        "runState": run_state,
        "activity_state": activity_state,
        "activityState": activity_state,
        "waiting_approval": waiting_approval,
        "waitingApproval": waiting_approval,
        "pending_approval_count": pending_approval_count,
        "pendingApprovalCount": pending_approval_count,
        "active_node_count": active_node_count,
        "activeNodeCount": active_node_count,
        "active_run_id": active_run_id,
        "activeRunId": active_run_id,
        "active_turn_id": active_turn_id,
        "activeTurnId": active_turn_id,
        "active_execution_session_id": active_execution_session_id,
        "activeExecutionSessionId": active_execution_session_id,
        "runtime_scope_key": runtime_scope_key,
        "runtimeScopeKey": runtime_scope_key,
        "run_started_at": run_started_at,
        "runStartedAt": run_started_at,
        "run_updated_at": run_updated_at,
        "runUpdatedAt": run_updated_at,
        "mission_started_at": mission_started_at,
        "missionStartedAt": mission_started_at,
        "mission_updated_at": mission_updated_at,
        "missionUpdatedAt": mission_updated_at,
        "mission_completed_at": mission_completed_at,
        "missionCompletedAt": mission_completed_at,
        "leader_report_status": leader_report_status,
        "leaderReportStatus": leader_report_status,
        "leader_report_run_id": leader_report_run_id,
        "leaderReportRunId": leader_report_run_id,
        "leader_report_message_id": leader_report_message_id,
        "leaderReportMessageId": leader_report_message_id,
        "result": result,
        "message_count": message_count,
        "messageCount": message_count,
        "last_message_preview": last_message_preview,
        "lastMessagePreview": last_message_preview,
        "last_message_at": last_message_at,
        "lastMessageAt": last_message_at,
        "updated_at": updated_at,
        "updatedAt": updated_at,
    }


def slim_team_mission_conversation_status_payload(
    projection: Dict[str, Any],
    *,
    mission_id: str,
    conversation_id: str = "",
    conversation_session_id: str = "",
    source_event_type: str = "",
    source_run_id: str = "",
    source_seq: int = 0,
    team_mission_event_seq: int = 0,
    protocol: str = "",
    kind: str = TEAM_MISSION_CONVERSATION_STATUS_KIND,
) -> Dict[str, Any]:
    slim = slim_team_mission_conversation_status_projection(
        projection,
        mission_id=mission_id,
    )
    conversation_id = _text(conversation_id) or _text(slim.get("conversation_id"))
    conversation_session_id = _text(conversation_session_id) or _text(slim.get("conversation_session_id"))
    mission_id = _text(mission_id) or _text(slim.get("mission_id"))
    active_mission_id = _text(slim.get("active_mission_id")) or mission_id
    source_seq_value = _int_projection_value(source_seq)
    team_mission_event_seq_value = _int_projection_value(team_mission_event_seq)
    payload = {
        "schema_version": 2,
        "schemaVersion": 2,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        "mission_id": mission_id,
        "missionId": mission_id,
        "active_mission_id": active_mission_id,
        "activeMissionId": active_mission_id,
        "source_event_type": _text(source_event_type),
        "sourceEventType": _text(source_event_type),
        "source_run_id": _text(source_run_id),
        "sourceRunId": _text(source_run_id),
        "source_seq": source_seq_value,
        "sourceSeq": source_seq_value,
        "source_event_seq": source_seq_value,
        "sourceEventSeq": source_seq_value,
        "team_mission_event_seq": team_mission_event_seq_value,
        "teamMissionEventSeq": team_mission_event_seq_value,
        "mission_status": slim.get("mission_status") or "",
        "missionStatus": slim.get("missionStatus") or "",
        "status": slim.get("status") or "",
        "running": bool(slim.get("running")),
        "run_state": slim.get("run_state") or "",
        "runState": slim.get("runState") or "",
        "activity_state": slim.get("activity_state") or "",
        "activityState": slim.get("activityState") or "",
        "waiting_approval": bool(slim.get("waiting_approval")),
        "waitingApproval": bool(slim.get("waitingApproval")),
        "pending_approval_count": slim.get("pending_approval_count") or 0,
        "pendingApprovalCount": slim.get("pendingApprovalCount") or 0,
        "active_node_count": slim.get("active_node_count") or 0,
        "activeNodeCount": slim.get("activeNodeCount") or 0,
        "active_run_id": slim.get("active_run_id") or "",
        "activeRunId": slim.get("activeRunId") or "",
        "active_execution_session_id": slim.get("active_execution_session_id") or "",
        "activeExecutionSessionId": slim.get("activeExecutionSessionId") or "",
        "runtime_scope_key": slim.get("runtime_scope_key") or "",
        "runtimeScopeKey": slim.get("runtimeScopeKey") or "",
        "leader_report_status": slim.get("leader_report_status") or "",
        "leaderReportStatus": slim.get("leaderReportStatus") or "",
        "leader_report_run_id": slim.get("leader_report_run_id") or "",
        "leaderReportRunId": slim.get("leaderReportRunId") or "",
        "leader_report_message_id": slim.get("leader_report_message_id") or "",
        "leaderReportMessageId": slim.get("leaderReportMessageId") or "",
        "updated_at": slim.get("updated_at") or 0,
        "updatedAt": slim.get("updatedAt") or 0,
        "state": {
            "missionStatus": slim.get("missionStatus") or "",
            "projectedState": slim.get("runState") or "",
            "running": bool(slim.get("running")),
            "waitingApproval": bool(slim.get("waitingApproval")),
            "activeNodeCount": slim.get("activeNodeCount") or 0,
            "pendingApprovalCount": slim.get("pendingApprovalCount") or 0,
        },
        "leaderReport": {
            "status": slim.get("leaderReportStatus") or "",
            "runId": slim.get("leaderReportRunId") or "",
            "messageId": slim.get("leaderReportMessageId") or "",
        },
        "result": slim.get("result") if isinstance(slim.get("result"), dict) else {},
        "projection": slim,
    }
    if protocol:
        payload["protocol"] = _text(protocol)
    if kind:
        payload["kind"] = _text(kind)
    return payload


def compact_team_mission_conversation_status_event(
    event: Dict[str, Any],
    *,
    mission_id: str = "",
) -> tuple[Dict[str, Any], bool]:
    event = dict(event) if isinstance(event, dict) else {}
    if not event or _text(event.get("type")) != "team_mission.conversation.status":
        return event, False
    payload = dict(event.get("payload")) if isinstance(event.get("payload"), dict) else {}
    projection = (
        payload.get("conversation")
        if isinstance(payload.get("conversation"), dict)
        else payload.get("projection")
        if isinstance(payload.get("projection"), dict)
        else payload
    )
    if not isinstance(projection, dict) or not projection:
        return event, False
    source_seq = payload.get("source_event_seq") or payload.get("sourceEventSeq") or payload.get("source_seq")
    team_mission_event_seq = (
        payload.get("team_mission_event_seq")
        or payload.get("teamMissionEventSeq")
        or event.get("team_mission_event_seq")
        or event.get("seq")
    )
    event_mission_id = _text(event.get("mission_id"))
    payload_mission_id = _text(payload.get("mission_id") or payload.get("missionId"))
    next_payload = slim_team_mission_conversation_status_payload(
        projection,
        mission_id=_text(mission_id) or payload_mission_id or event_mission_id,
        conversation_id=_text(
            payload.get("conversation_id")
            or payload.get("conversationId")
            or event.get("conversation_id")
            or event.get("conversationId"),
        ),
        conversation_session_id=_text(
            payload.get("conversation_session_id")
            or payload.get("conversationSessionId")
            or event.get("conversation_session_id")
            or event.get("conversationSessionId"),
        ),
        source_event_type=_text(payload.get("source_event_type") or payload.get("sourceEventType")),
        source_run_id=_text(payload.get("source_run_id") or payload.get("sourceRunId") or event.get("source_run_id")),
        source_seq=_int_projection_value(source_seq),
        team_mission_event_seq=_int_projection_value(team_mission_event_seq),
        protocol=_text(payload.get("protocol")) or TEAM_MISSION_EVENT_PROTOCOL,
        kind=_text(payload.get("kind")) or TEAM_MISSION_CONVERSATION_STATUS_KIND,
    )
    next_event = {
        **event,
        "payload": next_payload,
    }
    changed = (
        "conversation" in payload
        or payload.get("projection") != next_payload.get("projection")
        or payload != next_payload
    )
    return next_event, changed
