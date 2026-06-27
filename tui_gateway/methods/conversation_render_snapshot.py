from __future__ import annotations

import json
import logging
from typing import Any

from tui_gateway.methods._shared import bind_server_globals

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
    lives in ``messages``), then oldest ``messages`` (paginated + re-fetchable),
    until the serialized result fits. Flags ``transportTruncated`` + pageInfo
    hasMore so the client lazy-loads the remainder instead of assuming it has the
    whole history.
    """
    if _payload_byte_size(result) <= max_bytes:
        return result
    truncated = False
    for key in ("runEvents", "messages"):
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
        or params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("stable_session_id")
        or params.get("stableSessionId")
        or metadata.get("conversation_id")
        or metadata.get("conversationId")
    )


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
        "run_events_limit": _bounded_limit(
            params.get("run_events_limit", params.get("runEventsLimit")),
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
            "runEvents": [],
            "pageInfo": {},
            "branchInfo": None,
        }, None
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return result, None


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    return _record(message.get("metadata"))


def _message_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("content") or "")


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


def _message_render_identity(message: dict[str, Any], index: int) -> str:
    message_id = _message_id(message)
    if message_id:
        return f"message:{message_id}"
    metadata = _message_metadata(message)
    parts = [
        _text(message.get("role")),
        _text(message.get("timestamp")),
        _text(metadata.get("run_id") or metadata.get("runId")),
        _text(metadata.get("turn_id") or metadata.get("turnId")),
        _text(metadata.get("client_message_id") or metadata.get("clientMessageId")),
        _message_source_seq(message),
        _message_text(message),
    ]
    if any(parts):
        return "|".join(parts)
    return f"index:{index}"


def _team_conversation_run_suffix(message: dict[str, Any], index: int) -> str:
    return (
        _message_source_seq(message)
        or _message_id(message)
        or _text(_message_metadata(message).get("turn_id") or _message_metadata(message).get("turnId"))
        or _text(message.get("timestamp"))
        or str(index)
    )


def _with_unique_team_render_run_id(
    message: dict[str, Any],
    *,
    run_id: str,
    index: int,
    suffix: str = "",
) -> dict[str, Any]:
    next_message = dict(message)
    metadata = dict(_message_metadata(next_message))
    team_mission = dict(_record(metadata.get("team_mission") or metadata.get("teamMission")))
    source_run_id = _message_source_run_id(next_message)
    if source_run_id:
        team_mission.setdefault("sourceRunId", source_run_id)
        team_mission.setdefault("source_run_id", source_run_id)
    source_seq = _message_source_seq(next_message)
    if source_seq:
        team_mission.setdefault("sourceSeq", source_seq)
        team_mission.setdefault("source_seq", source_seq)
    if team_mission:
        metadata["team_mission"] = team_mission
    metadata.setdefault("original_run_id", run_id)
    metadata["run_id"] = f"{run_id}:render:{suffix or _team_conversation_run_suffix(next_message, index)}"
    next_message["metadata"] = metadata
    return next_message


def _normalize_team_render_messages(messages: list[Any]) -> list[dict[str, Any]]:
    unique_messages: list[dict[str, Any]] = []
    seen_message_keys: set[str] = set()
    for index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            continue
        message = dict(raw)
        key = _message_render_identity(message, index)
        if key in seen_message_keys:
            continue
        seen_message_keys.add(key)
        unique_messages.append(message)

    assistant_run_counts: dict[str, int] = {}
    for message in unique_messages:
        if _text(message.get("role")) != "assistant":
            continue
        run_id = _text(_message_metadata(message).get("run_id") or _message_metadata(message).get("runId"))
        if run_id:
            assistant_run_counts[run_id] = assistant_run_counts.get(run_id, 0) + 1

    normalized: list[dict[str, Any]] = []
    render_run_ids: set[str] = set()
    for index, message in enumerate(unique_messages):
        if _text(message.get("role")) != "assistant":
            normalized.append(message)
            continue
        run_id = _text(_message_metadata(message).get("run_id") or _message_metadata(message).get("runId"))
        if run_id and assistant_run_counts.get(run_id, 0) > 1:
            suffix = _team_conversation_run_suffix(message, index)
            render_run_id = f"{run_id}:render:{suffix}"
            if render_run_id in render_run_ids:
                suffix = f"{suffix}:index-{index}"
                render_run_id = f"{run_id}:render:{suffix}"
            render_run_ids.add(render_run_id)
            normalized.append(_with_unique_team_render_run_id(message, run_id=run_id, index=index, suffix=suffix))
        else:
            if run_id:
                render_run_ids.add(run_id)
            normalized.append(message)
    return normalized


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
    messages = list(page.get("messages") or []) if isinstance(page, dict) else []
    if not messages:
        messages = list(graph.get("recent_messages") or graph.get("recentMessages") or [])
    messages = _normalize_team_render_messages(messages)
    page_info = (
        page.get("pageInfo")
        if isinstance(page, dict) and isinstance(page.get("pageInfo"), dict)
        else graph.get("message_page_info") or graph.get("messagePageInfo") or {}
    )
    mission = resolved.get("mission") if isinstance(resolved.get("mission"), dict) else {}
    run_events = _filter_team_render_run_events(
        list(page.get("runEvents") or []) if isinstance(page, dict) else [],
        conversation=conversation,
        mission=mission,
        messages=messages,
    )
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
            "team": team,
            "graph": graph,
            "messages": messages,
            "runEvents": run_events,
            "pageInfo": page_info if isinstance(page_info, dict) else {},
            "branchInfo": branch_info if isinstance(branch_info, dict) else None,
            "projection": {
                "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
                "source": projection_source,
                "renderReady": True,
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
            "messages": list(page.get("messages") or []),
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
    kind = _text(params.get("kind") or params.get("conversation_kind") or params.get("conversationKind")).lower()
    if kind in {"team", "team_mission", "team-mission"}:
        return _team_conversation_snapshot(rid, params)
    if _conversation_identifier(params) and not _stored_session_id(params):
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
