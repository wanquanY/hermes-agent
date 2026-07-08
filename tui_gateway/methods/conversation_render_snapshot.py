from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from hermes_team_mission.runtime.team_transcript_writer import main_transcript_message_decision
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.run_events import list_mission_activity_events

_server = bind_server_globals(globals())
logger = logging.getLogger(__name__)

_SNAPSHOT_SCHEMA_VERSION = "2026-06-16"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on"}


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


# The desktop WebSocket client rejects a single frame larger than ~4 MiB with
# close code 1009 ("message too big"), which aborts the render request AND
# tears down the gateway connection. The render bundles messages + runEvents +
# graph into ONE frame, so a long-running conversation can blow past the limit.
# Keep the serialized result safely under it (headroom for the JSON-RPC
# envelope + WS framing).
_RENDER_MAX_BYTES = 3_500_000
_RENDER_NON_STRUCTURAL_RUN_EVENT_TYPES = {
    "message.delta",
    "reasoning.delta",
    "thinking.delta",
    "tool.progress",
    "tool.generating",
    "subagent.output_delta",
    "subagent.reasoning_delta",
    "subagent.thinking",
    "subagent.progress",
    "agent_profile_test.output_delta",
    "agent_profile_test.thinking",
}
_TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}


def _payload_byte_size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _is_structural_run_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    return _text(event.get("type")) not in _RENDER_NON_STRUCTURAL_RUN_EVENT_TYPES


def _structural_run_events(events: list[Any]) -> list[dict[str, Any]]:
    return [dict(event) for event in events if _is_structural_run_event(event)]


def _run_ids_from_render_messages(messages: list[dict[str, Any]]) -> list[str]:
    run_ids = sorted(item for item in _covered_render_run_ids(messages) if item)
    return run_ids


def _mark_transport_truncated(result: dict[str, Any]) -> None:
    result["transportTruncated"] = True
    page_info = result.get("pageInfo")
    if isinstance(page_info, dict):
        page_info["hasMore"] = True
    projection = result.get("projection")
    if isinstance(projection, dict):
        projection["transportTruncated"] = True


def _cap_list_tail(
    result: dict[str, Any],
    container: dict[str, Any],
    key: str,
    *,
    max_bytes: int,
) -> bool:
    items = container.get(key)
    if not isinstance(items, list) or not items:
        return False
    if _payload_byte_size(result) <= max_bytes:
        return False

    original_count = len(items)
    container[key] = []
    base_size = _payload_byte_size(result)
    budget = max(0, max_bytes - base_size)
    kept: list[Any] = []
    used = 0
    for item in reversed(items):
        item_size = _payload_byte_size(item) + 8  # JSON array comma/bracket headroom.
        if item_size > budget - used:
            continue
        kept.append(item)
        used += item_size
    kept.reverse()
    container[key] = kept
    while container[key] and _payload_byte_size(result) > max_bytes:
        container[key] = container[key][1:]
    return len(container[key]) < original_count


def _cap_render_result(result: dict[str, Any], *, max_bytes: int = _RENDER_MAX_BYTES) -> dict[str, Any]:
    """Trim a render result so its WS frame can't trip close code 1009.

    Drops the recoverable collections newest-kept: oldest ``runEvents`` first
    (live deltas re-arrive via the events subscription; finished-run text already
    lives in ``messages``), then ``toolEvents`` and oldest ``messages``
    (paginated + re-fetchable),
    until the serialized result fits. Flags ``transportTruncated`` + pageInfo
    hasMore so the client lazy-loads the remainder instead of assuming it has the
    whole history.
    """
    if _payload_byte_size(result) <= max_bytes:
        return result
    truncated = False
    for key in ("runEvents", "toolEvents", "messages"):
        truncated = _cap_list_tail(result, result, key, max_bytes=max_bytes) or truncated
        if _payload_byte_size(result) <= max_bytes:
            break
    graph = result.get("graph")
    if isinstance(graph, dict):
        for key in ("recent_messages", "recentMessages", "task_frames", "taskFrames"):
            truncated = _cap_list_tail(result, graph, key, max_bytes=max_bytes) or truncated
            if _payload_byte_size(result) <= max_bytes:
                break
    if truncated:
        _mark_transport_truncated(result)
        # The truncation marker itself adds bytes. If the payload was exactly at
        # the cap, trim one more recoverable item deterministically.
        while _payload_byte_size(result) > max_bytes:
            changed = False
            for container, key in (
                (result, "runEvents"),
                (result, "toolEvents"),
                (result, "messages"),
                (graph, "recent_messages") if isinstance(graph, dict) else ({}, ""),
                (graph, "task_frames") if isinstance(graph, dict) else ({}, ""),
            ):
                if isinstance(container, dict) and isinstance(container.get(key), list) and container[key]:
                    container[key] = container[key][1:]
                    changed = True
                    break
            if not changed:
                break
    return result


def _conversation_identifier(params: dict[str, Any]) -> str:
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    return _text(
        params.get("identifier")
        or params.get("conversation_id")
        or params.get("conversationId")
        or params.get("mission_id")
        or params.get("missionId")
        or params.get("session_id")
        or params.get("sessionId")
        or params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("stable_session_id")
        or params.get("stableSessionId")
        or metadata.get("conversation_id")
        or metadata.get("conversationId")
    )


def _conversation_kind(params: dict[str, Any]) -> str:
    kind = _text(
        params.get("conversation_kind")
        or params.get("conversationKind")
        or params.get("kind")
    ).lower()
    if kind in {"team", "team_mission", "team-mission"}:
        return "team"
    if kind in {"direct", "ordinary", "hermes_session"}:
        return "direct"
    return ""


def _route_kind_from_session_index(db: Any, session_id: str) -> str:
    session_id = _text(session_id)
    if not session_id:
        return ""
    getter = getattr(db, "get_session_index", None)
    if not callable(getter):
        return ""
    try:
        row = getter(session_id) or {}
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot route kind lookup skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return ""
    return _conversation_kind(row)


def _route_kind_from_team_conversation(db: Any, identifier: str) -> str:
    identifier = _text(identifier)
    resolver = getattr(db, "resolve_team_mission_conversation", None)
    if not identifier or not callable(resolver):
        return ""
    try:
        resolved = resolver(identifier) or {}
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot team route lookup skipped identifier=%s: %s",
            identifier,
            exc,
        )
        return ""
    conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
    if isinstance(conversation, dict) and _text(
        conversation.get("conversation_id") or conversation.get("conversationId")
    ):
        return "team"
    return ""


def _route_conversation_kind(params: dict[str, Any]) -> str:
    explicit_kind = _conversation_kind(params)
    if explicit_kind:
        return explicit_kind
    db = _get_db()
    if db is None:
        return "direct"

    session_id = _stored_session_id(params)
    identifier = _conversation_identifier(params)
    for candidate in dict.fromkeys([session_id, identifier]):
        kind = _route_kind_from_session_index(db, candidate)
        if kind:
            return kind

    # Compatibility for callers that only pass the team conversation id. This
    # detects the conversation row itself, not active_mission_id.
    for candidate in dict.fromkeys([identifier, session_id]):
        kind = _route_kind_from_team_conversation(db, candidate)
        if kind:
            return kind
    return "direct"


def _stored_session_id(params: dict[str, Any]) -> str:
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    return _text(
        params.get("session_id")
        or params.get("sessionId")
        or params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("stable_session_id")
        or params.get("stableSessionId")
        or params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or metadata.get("stable_session_id")
        or metadata.get("stableSessionId")
    )


def _message_params(params: dict[str, Any], session_id: str) -> dict[str, Any]:
    include_run_events = _truthy(
        params.get("include_run_events", params.get("includeRunEvents")),
        default=True,
    )
    return {
        **params,
        "session_id": session_id,
        "direction": _text(params.get("direction")) or "tail",
        "limit": _bounded_limit(params.get("limit"), default=50, maximum=200),
        "include_ancestors": _truthy(
            params.get("include_ancestors", params.get("includeAncestors")),
            default=True,
        ),
        "include_run_events": include_run_events,
        "include_tool_events": _truthy(
            params.get("include_tool_events", params.get("includeToolEvents")),
            default=False,
        ),
        "run_events_limit": _bounded_limit(
            params.get("run_events_limit", params.get("runEventsLimit")),
            default=2000,
            maximum=5000,
        ),
        "tool_events_limit": _bounded_limit(
            params.get("tool_events_limit", params.get("toolEventsLimit")),
            default=2000,
            maximum=5000,
        ),
    }


def _messages_page(
    session_id: str,
    params: dict[str, Any],
    *,
    required: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not session_id:
        if required:
            return None, _err("conversation-render-snapshot", 4006, "session_id required")
        return {
            "messages": [],
            "toolEvents": [],
            "runEvents": [],
            "pageInfo": {},
            "branchInfo": None,
        }, None
    handler = _methods.get("session.messages")
    if not callable(handler):
        return None, _err("conversation-render-snapshot", 5008, "session.messages unavailable")
    response = handler("conversation-render-snapshot-messages", _message_params(params, session_id))
    if not isinstance(response, dict):
        return None, _err("conversation-render-snapshot", 5008, "session.messages returned invalid response")
    if response.get("error"):
        if required:
            return None, response
        return {
            "messages": [],
            "toolEvents": [],
            "runEvents": [],
            "pageInfo": {},
            "branchInfo": None,
        }, None
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return result, None


def _participants_for_session(session_id: str) -> list[dict[str, Any]]:
    session_id = _text(session_id)
    if not session_id:
        return []
    try:
        db = _get_db()
        lister = getattr(db, "list_conversation_participants", None) if db is not None else None
        if not callable(lister):
            return []
        participants = lister(session_id) or []
        return [dict(item) for item in participants if isinstance(item, dict)]
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot participants hydrate skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return []


def _mission_activities_for_session(session_id: str) -> list[dict[str, Any]]:
    session_id = _text(session_id)
    if not session_id:
        return []
    try:
        db = _get_db()
        lister = getattr(db, "list_active_mission_activities", None) if db is not None else None
        if not callable(lister):
            return []
        activities = lister(session_id) or []
        return [dict(item) for item in activities if isinstance(item, dict)]
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot mission activities hydrate skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return []


def _sqlite_scalar(db: Any, sql: str, params: tuple[Any, ...]) -> Any:
    conn = getattr(db, "_conn", None)
    lock = getattr(db, "_lock", None)
    if conn is None or lock is None:
        return None
    with lock:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    try:
        return row[0]
    except Exception:
        return None


def _int_value(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _run_event_activity_last_seq(db: Any, activity_id: str) -> int:
    activity_id = _text(activity_id)
    if not activity_id:
        return 0
    if activity_id.startswith("chat:"):
        return _run_event_session_last_seq(db, activity_id.removeprefix("chat:"))
    if activity_id.startswith("act-member_chat:"):
        parts = activity_id.split(":")
        if len(parts) >= 2:
            return _run_event_session_last_seq(db, parts[1])
    return 0


def _run_event_session_last_seq(db: Any, session_id: str) -> int:
    session_id = _text(session_id)
    if not session_id:
        return 0
    return _int_value(_sqlite_scalar(
        db,
        "SELECT next_seq - 1 FROM seq_counter WHERE session_id = ?",
        (session_id,),
    ))


def _mission_activity_last_seq(db: Any, mission_id: str) -> int:
    mission_id = _text(mission_id)
    if not mission_id:
        return 0
    try:
        events = list_mission_activity_events(db, mission_id, limit=1, reverse=True)
        return max((int(event.get("seq") or 0) for event in events if isinstance(event, dict)), default=0)
    except Exception:
        return 0


def _activity_watermark(
    *,
    activity_id: str,
    last_seq: int,
    status: str,
    terminal: bool,
    replay_policy: str,
    source: str,
) -> dict[str, Any]:
    normalized_activity_id = _text(activity_id)
    normalized_status = _text(status) or ("completed" if terminal else "idle")
    normalized_policy = _text(replay_policy) or ("cursor_only" if terminal else "replay_live")
    normalized_source = _text(source)
    seq = max(0, int(last_seq or 0))
    return {
        "activity_id": normalized_activity_id,
        "activityId": normalized_activity_id,
        "last_seq": seq,
        "lastSeq": seq,
        "status": normalized_status,
        "terminal": bool(terminal),
        "replay_policy": normalized_policy,
        "replayPolicy": normalized_policy,
        "source": normalized_source,
    }


def _mission_status_for_watermark(conversation: dict[str, Any], mission: dict[str, Any], is_running: bool) -> str:
    mission_status = _text(
        mission.get("status")
        or conversation.get("mission_status")
        or conversation.get("missionStatus")
        or conversation.get("run_state")
        or conversation.get("runState")
        or conversation.get("activity_state")
        or conversation.get("activityState")
    ).lower()
    if is_running:
        return "running"
    if mission_status in _TERMINAL_MISSION_STATUSES or mission_status == "waiting_approval":
        return mission_status
    return "idle"


def _team_member_ids(team: dict[str, Any], participants: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    member_lists = (
        team.get("members"),
        team.get("teamMembers"),
        team.get("team_members"),
        team.get("agent_team_members"),
    )
    for members in member_lists:
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            member_id = _text(
                member.get("member_id")
                or member.get("memberId")
                or member.get("id")
                or member.get("participant_id")
                or member.get("participantId")
            )
            role = _text(member.get("role")).lower()
            if member_id and role not in {"lead", "leader"}:
                ids.append(member_id)
    if not ids:
        for participant in participants:
            member_id = _text(
                participant.get("member_id")
                or participant.get("memberId")
                or participant.get("participant_id")
                or participant.get("participantId")
                or participant.get("id")
            )
            role = _text(participant.get("role")).lower()
            if member_id and role not in {"lead", "leader"}:
                ids.append(member_id)
    return list(dict.fromkeys(ids))


def _team_activity_watermarks(
    *,
    session_id: str,
    conversation: dict[str, Any],
    mission: dict[str, Any],
    team: dict[str, Any],
    participants: list[dict[str, Any]],
    mission_activities: list[dict[str, Any]],
    is_running: bool,
) -> list[dict[str, Any]]:
    db = _get_db()
    if db is None:
        return []
    normalized_session_id = _text(session_id)
    status = _mission_status_for_watermark(conversation, mission, is_running)
    terminal = status in _TERMINAL_MISSION_STATUSES or (not is_running and status == "idle")
    replay_policy = "replay_live" if is_running else "cursor_only"
    watermarks: list[dict[str, Any]] = []

    chat_activity_id = f"chat:{normalized_session_id}" if normalized_session_id else ""
    if chat_activity_id:
        watermarks.append(_activity_watermark(
            activity_id=chat_activity_id,
            last_seq=max(
                _run_event_activity_last_seq(db, chat_activity_id),
                _run_event_session_last_seq(db, normalized_session_id),
            ),
            status=status,
            terminal=terminal,
            replay_policy=replay_policy,
            source="run_events",
        ))

    mission_ids = []
    mission_id = _text(mission.get("mission_id") or mission.get("missionId"))
    if mission_id:
        mission_ids.append(mission_id)
    for activity in mission_activities:
        if not isinstance(activity, dict):
            continue
        activity_mission_id = _text(
            activity.get("target_mission_id")
            or activity.get("targetMissionId")
            or activity.get("mission_id")
            or activity.get("missionId")
        )
        if activity_mission_id:
            mission_ids.append(activity_mission_id)
    for item in dict.fromkeys(mission_ids):
        watermarks.append(_activity_watermark(
            activity_id=f"mission:{item}",
            last_seq=_mission_activity_last_seq(db, item),
            status=status if item == mission_id else "completed",
            terminal=terminal if item == mission_id else True,
            replay_policy="cursor_only" if (terminal or item != mission_id) else "replay_live",
            source="run_events",
        ))

    for member_id in _team_member_ids(team, participants):
        activity_id = f"act-member_chat:{normalized_session_id}:{member_id}" if normalized_session_id else ""
        if not activity_id:
            continue
        watermarks.append(_activity_watermark(
            activity_id=activity_id,
            last_seq=_run_event_activity_last_seq(db, activity_id),
            status=status,
            terminal=terminal,
            replay_policy=replay_policy,
            source="run_events",
        ))

    deduped: dict[str, dict[str, Any]] = {}
    for watermark in watermarks:
        activity_id = _text(watermark.get("activity_id"))
        if activity_id:
            deduped[activity_id] = watermark
    return list(deduped.values())


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    return _record(message.get("metadata"))


def _message_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("content") or "")


def _diagnostic_text_summary(value: Any) -> dict[str, Any]:
    text = str(value or "")
    return {
        "len": len(text),
        "sha1": hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12],
        "preview": text[:80].replace("\n", "\\n"),
    }


def _diagnostic_message_summary(index: int, message: dict[str, Any]) -> dict[str, Any]:
    metadata = _message_metadata(message)
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    decision = main_transcript_message_decision(message)
    return {
        "idx": index,
        "role": _text(message.get("role")),
        "message_id": _message_id(message),
        "conversation_message_id": _text(
            message.get("conversation_message_id") or message.get("conversationMessageId")
        ),
        "participant_id": _text(message.get("participant_id") or message.get("participantId")),
        "run_id": _text(metadata.get("run_id") or metadata.get("runId")),
        "turn_id": _text(metadata.get("turn_id") or metadata.get("turnId")),
        "activity_id": _text(metadata.get("activity_id") or metadata.get("activityId")),
        "activity_kind": _text(metadata.get("activity_kind") or metadata.get("activityKind")),
        "runtime_activity_kind": _text(metadata.get("runtime_activity_kind") or metadata.get("runtimeActivityKind")),
        "transcript_activity_kind": _text(
            metadata.get("transcript_activity_kind") or metadata.get("transcriptActivityKind")
        ),
        "team_mission_kind": _text(team_mission.get("kind")),
        "team_mission_source_node_id": _text(team_mission.get("source_node_id") or team_mission.get("sourceNodeId")),
        "decision": decision,
        "text": _diagnostic_text_summary(_message_text(message)),
    }


def _emit_team_render_diagnostic(stage: str, **fields: Any) -> None:
    try:
        from agent.dovie_diagnostics import emit_dovie_diagnostic

        emit_dovie_diagnostic("[dovie-team-render-debug]", {"stage": stage, **fields})
    except Exception:
        pass


def _message_id(message: dict[str, Any]) -> str:
    return _text(message.get("message_id") or message.get("messageId") or message.get("id"))


def _message_source_seq(message: dict[str, Any]) -> str:
    metadata = _message_metadata(message)
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    return _text(
        metadata.get("source_seq")
        or metadata.get("sourceSeq")
        or team_mission.get("source_seq")
        or team_mission.get("sourceSeq")
    )


def _message_source_run_id(message: dict[str, Any]) -> str:
    metadata = _message_metadata(message)
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    return _text(
        metadata.get("source_run_id")
        or metadata.get("sourceRunId")
        or team_mission.get("source_run_id")
        or team_mission.get("sourceRunId")
    )


def _message_participant_id(message: dict[str, Any]) -> str:
    metadata = _message_metadata(message)
    team_mission = _record(
        message.get("teamMission")
        or message.get("team_mission")
        or metadata.get("team_mission")
        or metadata.get("teamMission")
    )
    return _text(
        message.get("participant_id")
        or message.get("participantId")
        or metadata.get("participant_id")
        or metadata.get("participantId")
        or team_mission.get("participant_id")
        or team_mission.get("participantId")
    )


def _event_participant_id(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    payload = _record(event.get("payload"))
    return _text(
        event.get("participant_id")
        or event.get("participantId")
        or payload.get("participant_id")
        or payload.get("participantId")
    )


def _run_event_participant_index(run_events: list[Any]) -> dict[str, str]:
    indexed: dict[str, str] = {}
    for event in run_events:
        if not isinstance(event, dict):
            continue
        participant_id = _event_participant_id(event)
        if not participant_id:
            continue
        payload = _record(event.get("payload"))
        run_id = _event_run_id(event)
        seq = _text(event.get("seq") or payload.get("seq"))
        source_seq = _text(event.get("source_seq") or event.get("sourceSeq") or payload.get("source_seq") or payload.get("sourceSeq"))
        message_id = _text(payload.get("message_id") or payload.get("messageId"))
        for key in (
            f"run:{run_id}" if run_id else "",
            f"run-seq:{run_id}:{seq}" if run_id and seq else "",
            f"run-seq:{run_id}:{source_seq}" if run_id and source_seq else "",
            f"message:{message_id}" if message_id else "",
        ):
            if key:
                indexed.setdefault(key, participant_id)
    return indexed


def _participant_id_for_message_from_events(
    message: dict[str, Any],
    event_participants: dict[str, str],
) -> str:
    message_id = _message_id(message)
    run_id = _text(_message_metadata(message).get("run_id") or _message_metadata(message).get("runId"))
    source_run_id = _message_source_run_id(message)
    source_seq = _message_source_seq(message)
    for key in (
        f"message:{message_id}" if message_id else "",
        f"run-seq:{source_run_id}:{source_seq}" if source_run_id and source_seq else "",
        f"run-seq:{run_id}:{source_seq}" if run_id and source_seq else "",
        f"run:{source_run_id}" if source_run_id else "",
        f"run:{run_id}" if run_id else "",
    ):
        if key and event_participants.get(key):
            return event_participants[key]
    return ""


def _with_message_participant_id(message: dict[str, Any], participant_id: str) -> dict[str, Any]:
    participant_id = _text(participant_id) or _message_participant_id(message)
    if not participant_id:
        return message
    next_message = dict(message)
    next_message["participant_id"] = participant_id
    next_message["participantId"] = participant_id
    metadata = dict(_message_metadata(next_message))
    metadata["participant_id"] = participant_id
    metadata["participantId"] = participant_id
    team_mission = dict(_record(
        next_message.get("teamMission")
        or next_message.get("team_mission")
        or metadata.get("team_mission")
        or metadata.get("teamMission")
    ))
    team_mission["participant_id"] = participant_id
    team_mission["participantId"] = participant_id
    metadata["team_mission"] = team_mission
    metadata["teamMission"] = team_mission
    next_message["team_mission"] = team_mission
    next_message["teamMission"] = team_mission
    next_message["metadata"] = metadata
    return next_message


def _covered_render_run_ids(messages: list[dict[str, Any]]) -> set[str]:
    covered: set[str] = set()
    for message in messages:
        metadata = _message_metadata(message)
        for value in (
            metadata.get("run_id"),
            metadata.get("runId"),
            metadata.get("original_run_id"),
            _message_source_run_id(message),
        ):
            text = _text(value)
            if text:
                covered.add(text)
    return covered


def _event_run_id(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    payload = _record(event.get("payload"))
    return _text(event.get("run_id") or payload.get("run_id") or payload.get("runId"))


def _team_snapshot_active_run_ids(conversation: dict[str, Any], mission: dict[str, Any]) -> set[str]:
    active = {
        _text(conversation.get("active_run_id") or conversation.get("activeRunId")),
        _text(mission.get("active_run_id") or mission.get("activeRunId")),
    }
    return {item for item in active if item}


def _filter_team_render_run_events(
    run_events: list[Any],
    *,
    conversation: dict[str, Any],
    mission: dict[str, Any],
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_events = _structural_run_events(run_events)
    active_run_ids = _team_snapshot_active_run_ids(conversation, mission)
    running = bool(conversation.get("running") or conversation.get("active_run_id") or conversation.get("activeRunId"))
    if not running and not active_run_ids:
        return []
    covered_run_ids = _covered_render_run_ids(messages)
    filtered: list[dict[str, Any]] = []
    for event in normalized_events:
        run_id = _event_run_id(event)
        if active_run_ids and run_id not in active_run_ids:
            continue
        if run_id and run_id in covered_run_ids and run_id not in active_run_ids:
            continue
        filtered.append(event)
    return filtered


def _team_conversation_status_projection(conversation_id: str) -> dict[str, Any]:
    conversation_id = _text(conversation_id)
    if not conversation_id:
        return {}
    try:
        db = _get_db()
        projector = getattr(db, "get_team_mission_conversation_status_projection", None) if db is not None else None
        if not callable(projector):
            return {}
        projection = projector(conversation_id) or {}
        return dict(projection) if isinstance(projection, dict) else {}
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot status projection skipped conversation_id=%s: %s",
            conversation_id,
            exc,
        )
        return {}


def _team_conversation_is_running(
    *,
    conversation: dict[str, Any],
    mission: dict[str, Any],
    status_projection: dict[str, Any] | None = None,
) -> bool:
    conversation_id = _text(conversation.get("conversation_id") or conversation.get("conversationId"))
    projection = status_projection if isinstance(status_projection, dict) else {}
    if not projection:
        projection = _team_conversation_status_projection(conversation_id)
    if projection:
        return bool(projection.get("running")) or _text(
            projection.get("projected_state")
            or projection.get("run_state")
            or projection.get("runState")
            or projection.get("activity_state")
            or projection.get("activityState")
        ).lower() == "running"
    return bool(
        conversation.get("running")
        or conversation.get("active_run_id")
        or conversation.get("activeRunId")
        or mission.get("active_run_id")
        or mission.get("activeRunId")
    )


def _filter_main_transcript_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        dict(message)
        for message in messages
        if isinstance(message, dict) and main_transcript_message_decision(message).get("include")
    ]


def _team_page_info_for_visible_messages(
    page_info: Any,
    *,
    raw_messages: list[Any],
    visible_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    info = dict(page_info) if isinstance(page_info, dict) else {}
    raw_count = len([message for message in raw_messages if isinstance(message, dict)])
    visible_count = len(visible_messages)
    if raw_count == visible_count:
        return info
    for key in ("totalCount", "total_count", "returnedCount", "returned_count"):
        if key in info:
            info[key] = visible_count
    info["filteredByTranscriptActivity"] = True
    info["visibleCount"] = visible_count
    return info


def _team_graph_with_visible_messages(
    graph: dict[str, Any],
    *,
    messages: list[dict[str, Any]],
    page_info: dict[str, Any],
) -> dict[str, Any]:
    next_graph = dict(graph) if isinstance(graph, dict) else {}
    visible_messages = [dict(message) for message in messages]
    next_graph["recent_messages"] = visible_messages
    next_graph["recentMessages"] = visible_messages
    next_graph["message_page_info"] = dict(page_info)
    next_graph["messagePageInfo"] = dict(page_info)
    last_message = dict(visible_messages[-1]) if visible_messages else {}
    next_graph["last_message"] = last_message
    next_graph["lastMessage"] = last_message
    return next_graph


def _team_conversation_snapshot(
    rid: Any,
    params: dict[str, Any],
    *,
    projection_source: str = "conversation.render_snapshot",
) -> dict[str, Any]:
    identifier = _conversation_identifier(params)
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    handler = _methods.get("team_mission.conversation.resolve")
    if not callable(handler):
        return _err(rid, 5008, "team_mission.conversation.resolve unavailable")
    response = handler(
        "conversation-render-snapshot-resolve",
        {
            **params,
            "identifier": identifier,
        },
    )
    if not isinstance(response, dict):
        return _err(rid, 5008, "team_mission.conversation.resolve returned invalid response")
    if response.get("error"):
        return response
    resolved = response.get("result") if isinstance(response.get("result"), dict) else {}
    conversation = resolved.get("conversation") if isinstance(resolved.get("conversation"), dict) else {}
    if not _text(conversation.get("conversation_id") or conversation.get("conversationId")):
        logger.error(
            "team_mission.conversation.resolve returned conversation without canonical id: identifier=%s",
            identifier,
        )
        return _err(rid, 5008, "team_mission resolve returned conversation without canonical id")
    graph = resolved.get("graph") if isinstance(resolved.get("graph"), dict) else {}
    team = resolved.get("team") if isinstance(resolved.get("team"), dict) else {}
    graph_conversation = graph.get("conversation") if isinstance(graph.get("conversation"), dict) else {}
    session_id = _text(
        conversation.get("stable_session_id")
        or conversation.get("stableSessionId")
        or graph_conversation.get("stable_session_id")
        or graph_conversation.get("stableSessionId")
        or _stored_session_id(params)
    )
    page, error = _messages_page(session_id, params, required=False)
    if error:
        return error
    raw_messages = list(page.get("messages") or []) if isinstance(page, dict) else []
    messages = _filter_main_transcript_messages(raw_messages)
    raw_message_summaries = [
        _diagnostic_message_summary(index, message)
        for index, message in enumerate(raw_messages)
        if isinstance(message, dict)
    ]
    filtered_message_summaries = [
        summary for summary in raw_message_summaries
        if not _record(summary.get("decision")).get("include")
    ]
    raw_run_events = list(page.get("runEvents") or []) if isinstance(page, dict) else []
    event_participants = _run_event_participant_index(raw_run_events)
    if event_participants:
        messages = [
            _with_message_participant_id(
                message,
                _participant_id_for_message_from_events(message, event_participants),
            )
            for message in messages
        ]
    tool_events = list(page.get("toolEvents") or []) if isinstance(page, dict) else []
    page_info = (
        page.get("pageInfo")
        if isinstance(page, dict) and isinstance(page.get("pageInfo"), dict)
        else graph.get("message_page_info") or graph.get("messagePageInfo") or {}
    )
    page_info = _team_page_info_for_visible_messages(
        page_info,
        raw_messages=raw_messages,
        visible_messages=messages,
    )
    graph = _team_graph_with_visible_messages(
        graph,
        messages=messages,
        page_info=page_info,
    )
    mission = resolved.get("mission") if isinstance(resolved.get("mission"), dict) else {}
    mission_present = bool(_text(mission.get("mission_id") or mission.get("missionId")))
    if not mission_present:
        mission = {}
    conversation_id = _text(conversation.get("conversation_id") or conversation.get("conversationId"))
    status_projection = _team_conversation_status_projection(conversation_id)
    is_running = _team_conversation_is_running(
        conversation=conversation,
        mission=mission,
        status_projection=status_projection,
    )
    participants = _participants_for_session(session_id)
    mission_activities = _mission_activities_for_session(session_id)
    activity_watermarks = _team_activity_watermarks(
        session_id=session_id,
        conversation=conversation,
        mission=mission,
        team=team,
        participants=participants,
        mission_activities=mission_activities,
        is_running=is_running,
    )
    _emit_team_render_diagnostic(
        "team-conversation-snapshot-filter",
        request_id=str(rid),
        identifier=identifier,
        conversation_id=_text(conversation.get("conversation_id") or conversation.get("conversationId")),
        session_id=session_id,
        mission_id=_text(mission.get("mission_id") or mission.get("missionId")),
        raw_message_count=len(raw_message_summaries),
        visible_message_count=len(messages),
        filtered_message_count=len(filtered_message_summaries),
        raw_run_event_count=len(raw_run_events),
        tool_event_count=len(tool_events),
        is_running=is_running,
        filtered_samples=filtered_message_summaries[:16],
        raw_samples=raw_message_summaries[:24],
    )
    if is_running:
        run_events = _filter_team_render_run_events(
            raw_run_events,
            conversation=conversation,
            mission=mission,
            messages=messages,
        )
    else:
        run_events = []
    branch_info = page.get("branchInfo") if isinstance(page, dict) else None
    return _ok(
        rid,
        _cap_render_result({
            "kind": "team_mission",
            "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
            "renderReady": True,
            "stable_session_id": session_id,
            "stored_session_id": session_id,
            "session_id": session_id,
            "conversation": conversation,
            "mission": mission,
            "missions": mission_activities,
            "missionPresent": mission_present,
            "mission_present": mission_present,
            "team": team,
            "graph": graph,
            "participants": participants,
            "messages": messages,
            "toolEvents": tool_events,
            "runEvents": run_events,
            "activityWatermarks": activity_watermarks,
            "activity_watermarks": activity_watermarks,
            "pageInfo": page_info if isinstance(page_info, dict) else {},
            "branchInfo": branch_info if isinstance(branch_info, dict) else None,
            "projection": {
                **status_projection,
                "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
                "source": projection_source,
                "renderReady": True,
                "activityWatermarks": activity_watermarks,
                "activity_watermarks": activity_watermarks,
                "visibleWindow": {
                    "direction": _text(params.get("direction")) or "tail",
                    "limit": _bounded_limit(params.get("limit"), default=50, maximum=200),
                },
            },
        }),
    )


def _ordinary_conversation_snapshot(rid: Any, params: dict[str, Any]) -> dict[str, Any]:
    session_id = _stored_session_id(params) or _conversation_identifier(params)
    page, error = _messages_page(session_id, params, required=True)
    if error:
        return error
    page = page or {}
    return _ok(
        rid,
        _cap_render_result({
            "kind": "ordinary",
            "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
            "renderReady": True,
            "stable_session_id": session_id,
            "stored_session_id": session_id,
            "session_id": session_id,
            "participants": _participants_for_session(session_id),
            "messages": list(page.get("messages") or []),
            "toolEvents": list(page.get("toolEvents") or []),
            "runEvents": _structural_run_events(list(page.get("runEvents") or [])),
            "pageInfo": page.get("pageInfo") if isinstance(page.get("pageInfo"), dict) else {},
            "branchInfo": page.get("branchInfo") if isinstance(page.get("branchInfo"), dict) else None,
            "projection": {
                "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
                "source": "conversation.render_snapshot",
                "renderReady": True,
                "visibleWindow": {
                    "direction": _text(params.get("direction")) or "tail",
                    "limit": _bounded_limit(params.get("limit"), default=50, maximum=200),
                },
            },
        }),
    )


@method("conversation.render_snapshot")
def _(rid, params: dict) -> dict:
    params = params if isinstance(params, dict) else {}
    if _route_conversation_kind(params) == "team":
        return _team_conversation_snapshot(rid, params)
    return _ordinary_conversation_snapshot(rid, params)


@method("team_mission.conversation.render")
def _(rid, params: dict) -> dict:
    params = params if isinstance(params, dict) else {}
    return _team_conversation_snapshot(
        rid,
        {
            **params,
            "kind": "team_mission",
        },
        projection_source="team_mission.conversation.render",
    )
