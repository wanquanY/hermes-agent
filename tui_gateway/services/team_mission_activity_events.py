"""Team Mission activity subscription bridge.

Mission activity subscriptions consume canonical ``run_events`` activity
indexes. ``team_mission_events`` is audit-only and must not be used as the
runtime replay source.
"""

from __future__ import annotations

from typing import Any, Callable

from hermes_team_mission.state.event_log import projection_event
from tui_gateway.services.run_events import (
    list_activity_events as _list_activity_run_events,
    list_mission_activity_events as _list_mission_run_events,
)

_TERMINAL_DELIVERY_LOG_COUNTS: dict[tuple[str, str, str, str, str, str], int] = {}

TEAM_MISSION_ACTIVITY_REPLAY_DEFAULT_LIMIT = 50
TEAM_MISSION_ACTIVITY_REPLAY_MAX_LIMIT = 100
TEAM_MISSION_ACTIVITY_TRANSPORT_PROTOCOL = "team_mission.activity.transport.v1"
TEAM_MISSION_ACTIVITY_STRUCTURAL_PAYLOAD_FIELDS = (
    "node",
    "nodes",
    "edge",
    "edges",
    "binding",
    "run_binding",
    "runBinding",
    "approval",
    "approval_request",
    "approvalRequest",
    "approval_requests",
    "approvalRequests",
    "approval_id",
    "approvalId",
    "scope",
    "approval_scope",
    "approvalScope",
    "reason",
    "title",
    "assignee_member_id",
    "assigneeMemberId",
    "assignee_profile_id",
    "assigneeProfileId",
    "agent_profile_id",
    "agentProfileId",
    "assignee_display_name",
    "assigneeDisplayName",
    "projection",
    "conversation",
    "status_projection",
    "statusProjection",
    "mission_status",
    "missionStatus",
    "active_mission_id",
    "activeMissionId",
    "team_id",
    "teamId",
    "workspace_id",
    "workspaceId",
    "workspace_path",
    "workspacePath",
    "updated_at",
    "updatedAt",
)
TEAM_MISSION_ACTIVITY_TOOL_SOURCE_EVENT_TYPES = {
    "tool.start",
    "tool.progress",
    "tool.generating",
    "tool.complete",
}
TEAM_MISSION_ACTIVITY_INTERACTIVE_SOURCE_EVENT_TYPES = {
    "approval.request",
    "sudo.request",
    "secret.request",
    "clarify.request",
}
TEAM_MISSION_ACTIVITY_TOOL_PAYLOAD_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tool_call_id", ("toolCallId", "tool_id", "toolId")),
    ("tool_name", ("toolName", "name")),
    ("name", ("tool_name", "toolName")),
    ("status", ()),
    ("arguments", ("args",)),
    ("context", ()),
    ("duration_s", ("durationS", "duration_seconds", "durationSeconds", "duration")),
    ("summary", ()),
    ("result", ()),
    ("result_text", ("resultText", "output_text", "outputText")),
    ("inline_diff", ("inlineDiff",)),
    ("todos", ()),
    ("artifact_refs", ("artifactRefs",)),
    ("error", ()),
    ("message", ()),
)
TEAM_MISSION_ACTIVITY_INTERACTIVE_PAYLOAD_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("request_id", ("requestId", "clarify_id", "clarifyId", "id")),
    ("question", ()),
    ("choices", ()),
    ("command", ()),
    ("description", ()),
    ("severity", ()),
    ("env_var", ("envVar",)),
    ("prompt", ()),
    ("tool_call_id", ("toolCallId", "tool_id", "toolId")),
    ("pattern_key", ("patternKey",)),
)
TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}


def text(value: Any) -> str:
    return str(value or "").strip()


def _emit_activity_diagnostic(stage: str, **fields: Any) -> None:
    try:
        from agent.dovie_diagnostics import emit_dovie_diagnostic

        emit_dovie_diagnostic("[dovie-team-activity-debug]", {"stage": stage, **fields})
    except Exception:
        pass


def _terminal_activity_log(stage: str, **fields: Any) -> None:
    event = fields.get("event") if isinstance(fields.get("event"), dict) else {}
    text_event = text(event.get("text_event"))
    event_type = text(event.get("type"))
    source_event_type = text(event.get("source_event_type"))
    is_stream_delta = (
        text_event == "delta"
        or event_type.endswith(".delta")
        or source_event_type.endswith(".delta")
    )
    if is_stream_delta:
        key = (
            stage,
            text(fields.get("mission_id")),
            text(fields.get("activity_id")),
            event_type,
            source_event_type,
            text_event,
        )
        count = _TERMINAL_DELIVERY_LOG_COUNTS.get(key, 0) + 1
        _TERMINAL_DELIVERY_LOG_COUNTS[key] = count
        if count > 5 and count % 50 != 0:
            return
        fields["sampled_delta_count"] = count
    try:
        from agent.dovie_diagnostics import emit_dovie_runtime_diagnostic

        emit_dovie_runtime_diagnostic("dovie-team-activity", stage, fields)
    except Exception:
        pass


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text_stream = event.get("text_stream") if isinstance(event.get("text_stream"), dict) else {}
    if not text_stream:
        text_stream = payload.get("text_stream") if isinstance(payload.get("text_stream"), dict) else {}
    subject = event_subject(event)
    return {
        "type": text(event.get("type")),
        "seq": event.get("seq"),
        "run_id": text(event.get("run_id") or payload.get("run_id")),
        "mission_id": text(event.get("mission_id") or payload.get("mission_id")),
        "kind": text(event.get("kind") or payload.get("kind")),
        "source_event_type": text(payload.get("source_event_type") or payload.get("sourceEventType")),
        "subject_type": text(subject.get("type")),
        "subject_id": text(subject.get("id")),
        "subject_node_id": text(subject.get("node_id") or subject.get("nodeId")),
        "runtime_conversation_session_id": text(
            subject.get("runtime_conversation_session_id") or subject.get("runtimeConversationSessionId")
        ),
        "text_event": text(text_stream.get("event")),
        "text_len": len(text(text_stream.get("delta") or text_stream.get("text"))),
    }


def mission_id(activity_id: str) -> str:
    normalized = str(activity_id or "").strip()
    if normalized.startswith("mission:"):
        return normalized.split("mission:", 1)[1].strip()
    if normalized.startswith("act-node:"):
        remainder = normalized.split("act-node:", 1)[1].strip()
        return remainder.split(":", 1)[0].strip()
    return ""


def is_team_dispatch_activity_id(activity_id: str) -> bool:
    normalized = str(activity_id or "").strip()
    return normalized.startswith("act-team_dispatch-") or normalized.startswith("act-team_dispatch:")


def _activity_target_mission_id(activity_id: str, db: Any = None) -> str:
    normalized = str(activity_id or "").strip()
    if not normalized:
        return ""
    if db is None:
        return ""
    try:
        activity = db.activities.get(normalized)
    except Exception:
        return ""
    if not isinstance(activity, dict):
        return ""
    return text(activity.get("target_mission_id") or activity.get("targetMissionId"))


def mission_id_for_activity(activity_id: str, db: Any = None) -> str:
    """Return the Team Mission id owned by an Activity subscription.

    ``mission:<id>`` and ``act-node:<mission>:<node>`` encode the mission in
    the activity id. Activity-first team requests instead use a stable
    ``act-team_dispatch-*`` id and bind the spawned mission through the
    activities table's ``target_mission_id``.
    """
    return mission_id(activity_id) or _activity_target_mission_id(activity_id, db=db)


def mission_id_for_run_event(event: dict[str, Any], db: Any = None) -> str:
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    activity_id = text(
        event.get("activity_id")
        or event.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
    )
    direct = text(
        event.get("mission_id")
        or event.get("missionId")
        or payload.get("mission_id")
        or payload.get("missionId")
    )
    if direct:
        return direct
    encoded = mission_id(activity_id)
    if encoded:
        return encoded
    run_id = text(event.get("run_id") or event.get("runId") or payload.get("run_id"))
    if not run_id or db is None:
        return ""
    try:
        return db.team_missions.mission_id_for_run(run_id)
    except Exception:
        return ""


def session_id_for_activity(activity_id: str) -> str:
    normalized = text(activity_id)
    if normalized.startswith("chat:"):
        return normalized.removeprefix("chat:")
    if normalized.startswith("act-member_chat:"):
        parts = normalized.split(":")
        if len(parts) >= 2:
            return text(parts[1])
    return ""


def node_selector(activity_id: str) -> str:
    normalized = str(activity_id or "").strip()
    if not normalized.startswith("act-node:"):
        return ""
    remainder = normalized.split("act-node:", 1)[1].strip()
    parts = remainder.split(":", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def is_activity_id(activity_id: str) -> bool:
    normalized = str(activity_id or "").strip()
    return bool(
        mission_id(normalized)
        or is_team_dispatch_activity_id(normalized)
    )


def uses_event_log(activity_id: str, db: Any = None) -> bool:
    """Return true when an activity id is backed by Team Mission activity replay.

    ``mission:<id>`` is also used by the generic Activity command bridge in a
    few legacy paths. Those activities have no Team Mission graph and must keep
    reading ordinary activity-indexed ``run_events``. A real Team Mission graph
    is the boundary that switches to mission-scoped run_events replay.
    """
    normalized_mission_id = mission_id_for_activity(activity_id, db=db)
    if not normalized_mission_id:
        return False
    if str(activity_id or "").strip().startswith("act-node:"):
        return True
    if db is None:
        return False
    try:
        return db.team_missions.exists(normalized_mission_id)
    except Exception:
        return False


def mission_status_for_activity(activity_id: str, db: Any = None) -> str:
    normalized_mission_id = mission_id_for_activity(activity_id, db=db)
    if not normalized_mission_id:
        return ""
    if db is None:
        return ""
    try:
        return db.team_missions.status(normalized_mission_id)
    except Exception:
        return ""


def is_terminal_activity(activity_id: str, db: Any = None) -> bool:
    return mission_status_for_activity(activity_id, db=db) in TERMINAL_MISSION_STATUSES


def activity_last_seq(activity_id: str, db: Any = None) -> int:
    normalized_activity_id = text(activity_id)
    if not normalized_activity_id:
        return 0
    if uses_event_log(normalized_activity_id, db=db):
        events = _list_mission_activity_run_events(
            db,
            normalized_activity_id,
            after_seq=0,
            limit=1,
            reverse=True,
        )
        return max((int(event.get("seq") or 0) for event in events if isinstance(event, dict)), default=0)
    session_id = session_id_for_activity(normalized_activity_id)
    if not session_id:
        return 0
    if db is None:
        return 0
    try:
        status = db.runs.session_status(session_id)
    except Exception:
        return 0
    return max(int(status.get("last_event_seq") or 0), 0)


def event_subject(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text_stream = event.get("text_stream")
    if not isinstance(text_stream, dict):
        text_stream = payload.get("text_stream") if isinstance(payload.get("text_stream"), dict) else {}
    subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
    if not subject:
        subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    if not subject and isinstance(text_stream, dict):
        subject = text_stream.get("subject") if isinstance(text_stream.get("subject"), dict) else {}
    return dict(subject) if isinstance(subject, dict) else {}


def event_text_stream(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text_stream = event.get("text_stream") if isinstance(event.get("text_stream"), dict) else {}
    if not text_stream:
        text_stream = payload.get("text_stream") if isinstance(payload.get("text_stream"), dict) else {}
    return dict(text_stream) if isinstance(text_stream, dict) else {}


def _compact_payload_value(event: dict[str, Any], payload: dict[str, Any], key: str, *aliases: str) -> Any:
    for candidate in (key, *aliases):
        if candidate in event and event.get(candidate) not in (None, ""):
            return event.get(candidate)
        if candidate in payload and payload.get(candidate) not in (None, ""):
            return payload.get(candidate)
    return None


def _copy_compact_payload_fields(
    target: dict[str, Any],
    *,
    event: dict[str, Any],
    payload: dict[str, Any],
    fields: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    for key, aliases in fields:
        value = _compact_payload_value(event, payload, key, *aliases)
        if value not in (None, ""):
            target[key] = value


def _source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    direct = payload.get("source_payload") or payload.get("sourcePayload")
    if isinstance(direct, dict):
        return direct
    source_event = payload.get("source_event") or payload.get("sourceEvent")
    if isinstance(source_event, dict):
        nested = source_event.get("payload")
        if isinstance(nested, dict):
            return nested
    return {}


def _copy_structural_payload_fields(
    target: dict[str, Any],
    *,
    payload: dict[str, Any],
    source_payload: dict[str, Any],
) -> None:
    for key in TEAM_MISSION_ACTIVITY_STRUCTURAL_PAYLOAD_FIELDS:
        value = payload.get(key)
        if value in (None, ""):
            value = source_payload.get(key)
        if value in (None, ""):
            continue
        target[key] = value


def _copy_tool_payload_fields(
    target: dict[str, Any],
    *,
    payload: dict[str, Any],
    source_payload: dict[str, Any],
    source_event_type: str,
) -> None:
    if source_event_type not in TEAM_MISSION_ACTIVITY_TOOL_SOURCE_EVENT_TYPES:
        return
    for key, aliases in TEAM_MISSION_ACTIVITY_TOOL_PAYLOAD_FIELDS:
        value = None
        for candidate in (key, *aliases):
            if candidate in payload and payload.get(candidate) not in (None, ""):
                value = payload.get(candidate)
                break
            if candidate in source_payload and source_payload.get(candidate) not in (None, ""):
                value = source_payload.get(candidate)
                break
        if value not in (None, ""):
            target[key] = value


def _copy_interactive_payload_fields(
    target: dict[str, Any],
    *,
    payload: dict[str, Any],
    source_payload: dict[str, Any],
    source_event_type: str,
) -> None:
    if source_event_type not in TEAM_MISSION_ACTIVITY_INTERACTIVE_SOURCE_EVENT_TYPES:
        return
    for key, aliases in TEAM_MISSION_ACTIVITY_INTERACTIVE_PAYLOAD_FIELDS:
        value = None
        for candidate in (key, *aliases):
            if candidate in payload and payload.get(candidate) not in (None, ""):
                value = payload.get(candidate)
                break
            if candidate in source_payload and source_payload.get(candidate) not in (None, ""):
                value = source_payload.get(candidate)
                break
        if value not in (None, ""):
            target[key] = value


def transport_event_for_subscription(event: dict[str, Any], activity_id: str) -> dict[str, Any]:
    """Project a persisted Team Mission audit event to the live activity ABI.

    ``team_mission_events`` is an audit log and may retain the full source
    event. ``runtime.activity.subscribe`` is a transport ABI: it must carry only
    fields the Dovie canvas reducer needs and must not ship diagnostic blobs
    like ``source_event`` or ``source_payload``.
    """
    source = dict(event or {})
    payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
    source_payload = _source_payload(payload)
    subject = event_subject(source)
    text_stream = event_text_stream(source)
    source_event_type = text(
        payload.get("source_event_type")
        or payload.get("sourceEventType")
        or payload.get("event_type")
        or payload.get("eventType")
    )
    if text_stream:
        text_stream.pop("subject", None)
        if source_event_type == "message.delta" and "delta" in text_stream:
            text_stream.pop("text", None)
    compact_payload: dict[str, Any] = {
        "protocol": TEAM_MISSION_ACTIVITY_TRANSPORT_PROTOCOL,
        "kind": text(source.get("kind") or payload.get("kind")),
        "event_type": source_event_type,
        "source_event_type": source_event_type,
        "activity_id": activity_id,
        "activityId": activity_id,
    }
    if subject:
        compact_payload["subject"] = subject
    if text_stream:
        compact_payload["text_stream"] = text_stream
    _copy_structural_payload_fields(
        compact_payload,
        payload=payload,
        source_payload=source_payload,
    )
    _copy_tool_payload_fields(
        compact_payload,
        payload=payload,
        source_payload=source_payload,
        source_event_type=source_event_type,
    )
    _copy_interactive_payload_fields(
        compact_payload,
        payload=payload,
        source_payload=source_payload,
        source_event_type=source_event_type,
    )
    _copy_compact_payload_fields(
        compact_payload,
        event=source,
        payload=payload,
        fields=(
            ("mission_id", ("missionId",)),
            ("conversation_id", ("conversationId",)),
            ("conversation_session_id", ("conversationSessionId",)),
            ("conversation_session_id", ("conversationSessionId",)),
            ("execution_session_id", ("executionSessionId",)),
            ("runtime_scope_key", ("runtimeScopeKey",)),
            ("run_id", ("runId",)),
            ("turn_id", ("turnId",)),
            ("node_id", ("nodeId",)),
            ("canonical_node_id", ("canonicalNodeId",)),
            ("task_id", ("taskId",)),
            ("task_frame_id", ("taskFrameId",)),
            ("tool_call_id", ("toolCallId", "tool_id", "toolId")),
            ("tool_name", ("toolName", "name")),
            ("name", ("tool_name", "toolName")),
            ("status", ()),
            ("reason_code", ("reasonCode",)),
            ("recoverability", ()),
            ("failure_message", ("failureMessage",)),
        ),
    )
    source_seq = _compact_payload_value(source, payload, "source_seq", "sourceSeq")
    if source_seq not in (None, ""):
        compact_payload["source_seq"] = source_seq
        compact_payload["sourceSeq"] = source_seq
    compact: dict[str, Any] = {
        "type": text(source.get("type")),
        "seq": source.get("seq"),
        "source_seq": source.get("source_seq") or source_seq,
        "team_mission_event_seq": source.get("team_mission_event_seq")
        or payload.get("team_mission_event_seq")
        or payload.get("teamMissionEventSeq")
        or source.get("seq"),
        "timestamp": source.get("timestamp"),
        "activity_id": activity_id,
        "activityId": activity_id,
        "payload": compact_payload,
    }
    if subject:
        compact["subject"] = subject
    _copy_compact_payload_fields(
        compact,
        event=source,
        payload=payload,
        fields=(
            ("mission_id", ("missionId",)),
            ("conversation_id", ("conversationId",)),
            ("conversation_session_id", ("conversationSessionId", "conversation_session_id", "conversationSessionId")),
            ("session_id", ("sessionId", "execution_session_id", "executionSessionId")),
            ("runtime_scope_key", ("runtimeScopeKey",)),
            ("run_id", ("runId",)),
            ("turn_id", ("turnId",)),
            ("node_id", ("nodeId",)),
            ("canonical_node_id", ("canonicalNodeId",)),
            ("task_id", ("taskId",)),
            ("task_frame_id", ("taskFrameId",)),
            ("tool_call_id", ("toolCallId", "tool_id", "toolId")),
            ("tool_name", ("toolName", "name")),
            ("name", ("tool_name", "toolName")),
        ),
    )
    return compact


def event_node_selectors(event: dict[str, Any]) -> set[str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    subject = event_subject(event)
    candidates = {
        subject.get("canonical_node_id"),
        subject.get("canonicalNodeId"),
        subject.get("node_id"),
        subject.get("nodeId"),
        subject.get("approval_id"),
        subject.get("approvalId"),
        subject.get("id"),
        event.get("canonical_node_id"),
        event.get("canonicalNodeId"),
        event.get("node_id"),
        event.get("nodeId"),
        event.get("approval_id"),
        event.get("approvalId"),
        payload.get("canonical_node_id"),
        payload.get("canonicalNodeId"),
        payload.get("node_id"),
        payload.get("nodeId"),
        payload.get("approval_id"),
        payload.get("approvalId"),
        payload.get("id"),
    }
    return {str(value).strip() for value in candidates if str(value or "").strip()}


def event_matches_activity(
    event: dict[str, Any],
    activity_id: str,
    *,
    resolved_mission_id: str = "",
) -> bool:
    normalized_mission_id = text(resolved_mission_id) or mission_id(activity_id)
    if not normalized_mission_id:
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    subject = event_subject(event)
    event_mission_id = str(
        event.get("mission_id")
        or payload.get("mission_id")
        or subject.get("mission_id")
        or ""
    ).strip()
    if event_mission_id and event_mission_id != normalized_mission_id:
        return False
    selector = node_selector(activity_id)
    if not selector:
        return True
    return selector in event_node_selectors(event)


def event_for_subscription(event: dict[str, Any], activity_id: str) -> dict[str, Any]:
    projected = transport_event_for_subscription(event, activity_id)
    projected_payload = projected.get("payload") if isinstance(projected.get("payload"), dict) else {}
    projected_payload = dict(projected_payload)
    projected["activity_id"] = activity_id
    projected["activityId"] = activity_id
    projected_payload["activity_id"] = activity_id
    projected_payload["activityId"] = activity_id
    try:
        activity_event_seq = int(projected.get("seq") or projected_payload.get("seq") or 0)
    except (TypeError, ValueError):
        activity_event_seq = 0
    if activity_event_seq > 0:
        projected["activity_event_seq"] = activity_event_seq
        projected["activityEventSeq"] = activity_event_seq
        projected_payload["activity_event_seq"] = activity_event_seq
        projected_payload["activityEventSeq"] = activity_event_seq
    projected["payload"] = projected_payload
    return projected


def _identity_for_activity_event(
    event: dict[str, Any],
    activity_id: str,
    mission_id_value: str,
) -> dict[str, str]:
    identity: dict[str, str] = {
        "mission_id": mission_id_value,
        "missionId": mission_id_value,
    }
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    source_activity_id = text(
        event.get("activity_id")
        or event.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
    )
    selector = node_selector(source_activity_id) or node_selector(activity_id)
    if selector:
        identity["node_id"] = selector
        identity["nodeId"] = selector
        identity["canonical_node_id"] = selector
        identity["canonicalNodeId"] = selector
    return identity


def _project_run_event_for_subscription(
    event: dict[str, Any],
    activity_id: str,
    *,
    mission_id_value: str,
) -> dict[str, Any]:
    event_type = text(event.get("type"))
    if event_type.startswith("team_mission."):
        return event_for_subscription(event, activity_id)
    try:
        source_seq = int(
            event.get("runtime_source_seq")
            or event.get("runtimeSourceSeq")
            or event.get("seq")
            or 0
        )
    except (TypeError, ValueError):
        source_seq = 0
    projected = projection_event(
        event,
        _identity_for_activity_event(event, activity_id, mission_id_value),
        source_seq=source_seq,
        mission_seq=source_seq,
    )
    return event_for_subscription(projected, activity_id)


def _list_mission_activity_run_events(
    db: Any,
    activity_id: str,
    *,
    after_seq: int,
    limit: int,
    reverse: bool = False,
) -> list[dict[str, Any]]:
    normalized_mission_id = mission_id_for_activity(activity_id, db=db)
    if not normalized_mission_id:
        return []
    if str(activity_id or "").strip().startswith("act-node:"):
        if db is None:
            return []
        events = _list_activity_run_events(db, activity_id, after_seq=after_seq, limit=limit)
    else:
        events = _list_mission_run_events(
            db,
            normalized_mission_id,
            after_seq=after_seq,
            limit=limit,
            reverse=reverse,
        )
    return [event for event in events if isinstance(event, dict)]


def list_activity_events(
    db: Any,
    activity_id: str,
    *,
    after_seq: int = 0,
    limit: int = 2000,
    event_activity_id: Callable[[dict[str, Any]], str],
) -> list[dict[str, Any]]:
    if uses_event_log(activity_id, db=db):
        normalized_mission_id = mission_id_for_activity(activity_id, db=db)
        try:
            bounded_limit = max(
                1,
                min(
                    int(limit or TEAM_MISSION_ACTIVITY_REPLAY_DEFAULT_LIMIT),
                    TEAM_MISSION_ACTIVITY_REPLAY_MAX_LIMIT,
                ),
            )
        except (TypeError, ValueError):
            bounded_limit = TEAM_MISSION_ACTIVITY_REPLAY_DEFAULT_LIMIT
        if not normalized_mission_id:
            _emit_activity_diagnostic(
                "list-activity-events-drop-no-mission-id",
                activity_id=activity_id,
                mission_id=normalized_mission_id,
            )
            _terminal_activity_log(
                "list-drop-no-mission-id",
                activity_id=activity_id,
                mission_id=normalized_mission_id,
            )
            return []
        try:
            events = _list_mission_activity_run_events(
                db,
                activity_id,
                after_seq=after_seq,
                limit=bounded_limit,
            )
        except Exception as exc:
            _emit_activity_diagnostic(
                "list-activity-events-error",
                activity_id=activity_id,
                mission_id=normalized_mission_id,
                after_seq=after_seq,
                limit=bounded_limit,
                error=f"{type(exc).__name__}: {exc}",
            )
            _terminal_activity_log(
                "list-error",
                activity_id=activity_id,
                mission_id=normalized_mission_id,
                after_seq=after_seq,
                limit=bounded_limit,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []
        result = [
            _project_run_event_for_subscription(
                event,
                activity_id,
                mission_id_value=normalized_mission_id,
            )
            for event in events
            if isinstance(event, dict)
        ]
        _emit_activity_diagnostic(
            "list-activity-events",
            activity_id=activity_id,
            mission_id=normalized_mission_id,
            source="run_events",
            after_seq=after_seq,
            requested_limit=limit,
            limit=bounded_limit,
            raw_count=len([event for event in events if isinstance(event, dict)]),
            matched_count=len(result),
            samples=[_event_summary(event) for event in result[:12]],
        )
        return result
    if is_team_dispatch_activity_id(activity_id):
        _emit_activity_diagnostic(
            "list-activity-events-waiting-team-dispatch-target",
            activity_id=activity_id,
            source="team_dispatch_activity",
            after_seq=after_seq,
            limit=limit,
            reason="target_mission_not_bound",
        )
        _terminal_activity_log(
            "list-waiting-team-dispatch-target",
            activity_id=activity_id,
            source="team_dispatch_activity",
            after_seq=after_seq,
            limit=limit,
            reason="target_mission_not_bound",
        )
        return []
    if db is None:
        _emit_activity_diagnostic(
            "list-activity-events-drop-no-run-event-activity-index",
            activity_id=activity_id,
        )
        _terminal_activity_log(
            "list-drop-no-run-event-activity-index",
            activity_id=activity_id,
        )
        return []
    try:
        events = _list_activity_run_events(db, activity_id, after_seq=after_seq, limit=limit)
    except Exception as exc:
        _emit_activity_diagnostic(
            "list-activity-events-run-event-index-error",
            activity_id=activity_id,
            after_seq=after_seq,
            limit=limit,
            error=f"{type(exc).__name__}: {exc}",
        )
        _terminal_activity_log(
            "list-run-event-index-error",
            activity_id=activity_id,
            after_seq=after_seq,
            limit=limit,
            error=f"{type(exc).__name__}: {exc}",
        )
        return []
    result = [
        event
        for event in events
        if isinstance(event, dict)
        and event_activity_id(event) == activity_id
        and int(event.get("seq") or 0) > int(after_seq or 0)
    ]
    _emit_activity_diagnostic(
        "list-activity-events",
        activity_id=activity_id,
        source="run_events_by_activity",
        after_seq=after_seq,
        limit=limit,
        raw_count=len([event for event in events if isinstance(event, dict)]),
        matched_count=len(result),
        samples=[_event_summary(event) for event in result[:12]],
    )
    return result


def deliver_appended_event(
    mission_id_value: str,
    event: dict[str, Any],
    *,
    lock: Any,
    subscription_ids_by_activity: dict[str, set[str]],
    subscriptions_by_id: dict[str, dict[str, Any]],
    live_status_event_for_subscription: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    deliver_subscription_event: Callable[[str, Any, dict[str, Any]], bool],
) -> None:
    normalized_mission_id = str(mission_id_value or "").strip()
    if not normalized_mission_id or not isinstance(event, dict):
        return
    with lock:
        subscriptions: list[tuple[dict[str, Any], str]] = []
        for activity_id, subscription_ids in list(subscription_ids_by_activity.items()):
            if not activity_id or not subscription_ids:
                continue
            for subscription_id in list(subscription_ids):
                subscription = subscriptions_by_id.get(subscription_id)
                if not isinstance(subscription, dict) or subscription.get("transport") is None:
                    continue
                subscription_mission_id = mission_id_for_activity(
                    activity_id,
                    db=subscription.get("db"),
                )
                if subscription_mission_id != normalized_mission_id:
                    continue
                if not event_matches_activity(
                    event,
                    activity_id,
                    resolved_mission_id=subscription_mission_id,
                ):
                    continue
                subscriptions.append((dict(subscription), activity_id))
    _emit_activity_diagnostic(
        "deliver-appended-event-match",
        mission_id=normalized_mission_id,
        event=_event_summary(event),
        matched_subscription_count=len(subscriptions),
        matched_activity_ids=[activity_id for _, activity_id in subscriptions[:24]],
    )
    _terminal_activity_log(
        "deliver-match",
        mission_id=normalized_mission_id,
        event=_event_summary(event),
        matched_subscription_count=len(subscriptions),
        matched_activity_ids=[activity_id for _, activity_id in subscriptions[:24]],
    )
    for subscription, activity_id in subscriptions:
        transport = subscription.get("transport")
        if transport is None:
            continue
        event_for_transport = _project_run_event_for_subscription(
            event,
            activity_id,
            mission_id_value=normalized_mission_id,
        )
        event_for_transport = live_status_event_for_subscription(subscription, event_for_transport)
        subscription_id = text(subscription.get("id"))
        if deliver_subscription_event(subscription_id, transport, event_for_transport):
            _emit_activity_diagnostic(
                "deliver-appended-event-written",
                mission_id=normalized_mission_id,
                activity_id=activity_id,
                subscription_id=subscription_id,
                event=_event_summary(event_for_transport),
            )
            _terminal_activity_log(
                "deliver-written",
                mission_id=normalized_mission_id,
                activity_id=activity_id,
                subscription_id=subscription_id,
                event=_event_summary(event_for_transport),
            )
        else:
            _terminal_activity_log(
                "deliver-write-failed",
                mission_id=normalized_mission_id,
                activity_id=activity_id,
                subscription_id=subscription_id,
                event=_event_summary(event_for_transport),
            )
