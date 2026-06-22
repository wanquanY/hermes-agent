from __future__ import annotations

import json
from typing import Any

from agent.dovie_diagnostics import emit_dovie_diagnostic


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int = 0, maximum: int = 5000) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _text_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    return {_text(item) for item in items if _text(item)}


def _param_text_set(params: dict[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        values.update(_text_set(params.get(key)))
    return values


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return fallback
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return fallback if parsed is None else parsed


def _row_value(row: Any, key: str, fallback: Any = "") -> Any:
    if row is None:
        return fallback
    try:
        return row[key]
    except Exception:
        return fallback


def _trace_history(stage: str, **fields: Any) -> None:
    emit_dovie_diagnostic("[team-mission-node-history]", {"stage": stage, **fields})


def _binding_matches(binding: dict[str, Any], *, node_id: str, session_id: str) -> bool:
    if node_id and _text(binding.get("node_id")) != node_id:
        return False
    if session_id and session_id not in {
        _text(binding.get("session_id")),
        _text(binding.get("runtime_session_id")),
    }:
        return False
    return True


def _select_binding(graph: dict[str, Any], *, node_id: str, session_id: str) -> dict[str, Any]:
    bindings = [
        dict(binding)
        for binding in graph.get("run_bindings") or []
        if isinstance(binding, dict) and _binding_matches(binding, node_id=node_id, session_id=session_id)
    ]
    if not bindings:
        return {}
    return sorted(
        bindings,
        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
        reverse=True,
    )[0]


def _conversation_from_params(db: Any, params: dict[str, Any]) -> dict[str, Any]:
    conversation_id = _text(
        params.get("conversation_id")
        or params.get("conversationId")
    )
    conversation_session_id = _text(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("stable_team_session_id")
        or params.get("stableTeamSessionId")
    )
    if conversation_id and hasattr(db, "get_team_mission_conversation"):
        conversation = db.get_team_mission_conversation(conversation_id)
        if conversation:
            return conversation
    if conversation_session_id and hasattr(db, "get_team_mission_conversation_by_session"):
        conversation = db.get_team_mission_conversation_by_session(conversation_session_id)
        if conversation:
            return conversation
    if conversation_id and hasattr(db, "resolve_team_mission_conversation"):
        resolved = db.resolve_team_mission_conversation(conversation_id)
        conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
        if isinstance(conversation, dict) and conversation:
            return conversation
    return {}


def _resolve_graph(db: Any, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    requested_mission_id = _text(params.get("mission_id") or params.get("missionId"))
    if requested_mission_id:
        graph = db.get_team_mission_graph(requested_mission_id)
        if graph:
            return requested_mission_id, graph

    conversation = _conversation_from_params(db, params)
    active_mission_id = _text(conversation.get("active_mission_id") or conversation.get("activeMissionId"))
    if active_mission_id:
        graph = db.get_team_mission_graph(active_mission_id)
        if graph:
            return active_mission_id, graph

    return requested_mission_id, {}


def _message_from_row(db: Any, row: Any) -> dict[str, Any]:
    content = _row_value(row, "content", "")
    decoder = getattr(db, "_decode_content", None)
    if callable(decoder):
        try:
            content = decoder(content)
        except Exception:
            pass
    role = _text(_row_value(row, "role", "assistant"))
    if role not in {"assistant", "system", "tool", "user"}:
        role = "assistant"
    metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
    message = {
        "role": role,
        "message_id": _text(_row_value(row, "platform_message_id", "")) or str(_row_value(row, "id", "")),
        "timestamp": float(_row_value(row, "timestamp", 0) or 0),
        "text": str(content or ""),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    tool_call_id = _text(_row_value(row, "tool_call_id", ""))
    if tool_call_id:
        message["tool_call_id"] = tool_call_id
    reasoning = _text(
        _row_value(row, "reasoning", "")
        or _row_value(row, "reasoning_content", "")
        or _row_value(row, "reasoning_details", "")
    )
    if reasoning:
        message["reasoning"] = reasoning
    if role == "tool":
        name = _text(_row_value(row, "tool_name", ""))
        if name:
            message["name"] = name
        if message["text"]:
            message["result_text"] = message["text"]
    return message


def _fetch_recent_messages(db: Any, session_id: str, *, limit: int) -> tuple[list[dict[str, Any]], int]:
    if not session_id or not hasattr(db, "_conn"):
        return [], 0
    bounded_limit = _bounded_int(limit, default=50, minimum=1, maximum=500)
    with db._lock:
        active_column = db._conn.execute("PRAGMA table_info(messages)").fetchall()
        has_active = any(_text(_row_value(row, "name")) == "active" for row in active_column)
        active_clause = "AND active = 1" if has_active else ""
        total = db._conn.execute(
            f"SELECT count(*) AS count FROM messages WHERE session_id = ? {active_clause}",
            (session_id,),
        ).fetchone()
        rows = db._conn.execute(
            f"""
            SELECT * FROM (
                SELECT *
                FROM messages
                WHERE session_id = ? {active_clause}
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (session_id, bounded_limit),
        ).fetchall()
    return [_message_from_row(db, row) for row in rows], int(_row_value(total, "count", len(rows)) or len(rows))


def _event_from_row(row: Any, session_id: str) -> dict[str, Any]:
    event = _json_loads(_row_value(row, "event_json", ""), {})
    payload = _json_loads(_row_value(row, "payload_json", ""), {})
    if not isinstance(event, dict):
        event = {}
    if not isinstance(payload, dict):
        payload = {}
    event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else payload
    return {
        **event,
        "type": _text(event.get("type") or _row_value(row, "event_type", "")),
        "session_id": _text(event.get("session_id") or _row_value(row, "runtime_session_id", "")),
        "stored_session_id": _text(event.get("stored_session_id")) or session_id,
        "runtime_session_id": _text(event.get("runtime_session_id") or _row_value(row, "runtime_session_id", "")),
        "runtime_scope_key": _text(event.get("runtime_scope_key") or _row_value(row, "runtime_scope_key", "")),
        "run_id": _text(event.get("run_id") or _row_value(row, "run_id", "")),
        "turn_id": _text(event.get("turn_id") or _row_value(row, "turn_id", "")),
        "seq": int(event.get("seq") or _row_value(row, "seq", 0) or 0),
        "payload": event_payload,
    }


def _fetch_recent_run_events(
    db: Any,
    session_id: str,
    *,
    run_id: str = "",
    limit: int,
    include_control_events: bool = False,
    event_types: set[str] | None = None,
    exclude_event_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not session_id or not hasattr(db, "_conn"):
        return []
    bounded_limit = _bounded_int(limit, default=0, minimum=0, maximum=5000)
    if bounded_limit <= 0:
        return []
    normalized_run_id = _text(run_id)
    include_types = sorted(event_types or set())
    exclude_types = sorted(exclude_event_types or set())
    clauses = [
        "session_id = ?",
        "(? = '' OR run_id = ?)",
        "(? = 1 OR event_type NOT LIKE 'mission.%')",
    ]
    params: list[Any] = [session_id, normalized_run_id, normalized_run_id, 1 if include_control_events else 0]
    if include_types:
        clauses.append(f"event_type IN ({','.join('?' for _ in include_types)})")
        params.extend(include_types)
    if exclude_types:
        clauses.append(f"event_type NOT IN ({','.join('?' for _ in exclude_types)})")
        params.extend(exclude_types)
    params.append(bounded_limit)
    where_clause = "\n                  AND ".join(clauses)
    with db._lock:
        rows = db._conn.execute(
            f"""
            SELECT * FROM (
                SELECT *
                FROM run_events
                WHERE {where_clause}
                ORDER BY seq DESC
                LIMIT ?
            ) ORDER BY seq ASC
            """,
            tuple(params),
        ).fetchall()
    return [_event_from_row(row, session_id) for row in rows]


def get_team_mission_node_runtime_history(db: Any, params: dict[str, Any]) -> dict[str, Any]:
    node_id = _text(params.get("node_id") or params.get("nodeId"))
    requested_session_id = _text(
        params.get("session_id")
        or params.get("sessionId")
        or params.get("stored_session_id")
        or params.get("storedSessionId")
    )
    if not node_id and not requested_session_id:
        return {"error": "node_id or session_id required", "code": 4006}

    conversation_id = _text(params.get("conversation_id") or params.get("conversationId"))
    conversation_session_id = _text(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("stable_team_session_id")
        or params.get("stableTeamSessionId")
    )
    include_run_events = bool(params.get("include_run_events") or params.get("includeRunEvents"))
    run_event_types = _param_text_set(
        params,
        "run_event_types",
        "runEventTypes",
        "include_run_event_types",
        "includeRunEventTypes",
    )
    exclude_run_event_types = _param_text_set(
        params,
        "exclude_run_event_types",
        "excludeRunEventTypes",
    )
    requested_limit = _bounded_int(params.get("limit"), default=50, minimum=1, maximum=500)
    requested_run_events_limit = _bounded_int(
        params.get("run_events_limit") or params.get("runEventsLimit"),
        default=0 if not include_run_events else 200,
        minimum=0,
        maximum=5000,
    )
    _trace_history(
        "request",
        mission_id=_text(params.get("mission_id") or params.get("missionId")),
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        node_id=node_id,
        requested_session_id=requested_session_id,
        include_run_events=include_run_events,
        limit=requested_limit,
        run_events_limit=requested_run_events_limit,
        run_event_types=sorted(run_event_types),
        exclude_run_event_types=sorted(exclude_run_event_types),
    )

    mission_id, graph = _resolve_graph(db, params)
    if not graph and not requested_session_id:
        _trace_history(
            "mission-not-found",
            mission_id=mission_id,
            node_id=node_id,
            requested_session_id=requested_session_id,
        )
        return {"error": "team mission not found", "code": 4040}
    binding = _select_binding(graph, node_id=node_id, session_id=requested_session_id) if graph else {}
    session_id = _text(binding.get("session_id")) or requested_session_id
    _trace_history(
        "resolved",
        mission_id=mission_id,
        graph_found=bool(graph),
        graph_node_count=len(graph.get("nodes") or []) if isinstance(graph, dict) else 0,
        graph_binding_count=len(graph.get("run_bindings") or []) if isinstance(graph, dict) else 0,
        binding_found=bool(binding),
        node_id=node_id,
        resolved_node_id=_text(binding.get("node_id")) or node_id,
        requested_session_id=requested_session_id,
        resolved_session_id=session_id,
        run_id=_text(binding.get("run_id")),
        runtime_session_id=_text(binding.get("runtime_session_id")),
        runtime_scope_key=_text(binding.get("runtime_scope_key")),
    )
    if not session_id:
        _trace_history("no-session", mission_id=mission_id, node_id=node_id)
        return {
            "mission_id": mission_id,
            "node_id": node_id,
            "messages": [],
            "run_events": [],
            "page_info": {"total_count": 0, "has_more_before": False, "has_more_after": False},
            "source": {"session_id": "", "runtime_session_id": "", "runtime_scope_key": "", "run_id": ""},
        }

    messages, total_count = _fetch_recent_messages(
        db,
        session_id,
        limit=requested_limit,
    )
    include_control_events = bool(params.get("include_control_events") or params.get("includeControlEvents"))
    run_id = _text(binding.get("run_id") or params.get("run_id") or params.get("runId"))
    run_events = _fetch_recent_run_events(
        db,
        session_id,
        run_id=run_id,
        limit=requested_run_events_limit,
        include_control_events=include_control_events,
        event_types=run_event_types,
        exclude_event_types=exclude_run_event_types,
    ) if include_run_events else []
    resolved_node_id = _text(binding.get("node_id")) or node_id
    _trace_history(
        "result",
        mission_id=mission_id,
        node_id=resolved_node_id,
        session_id=session_id,
        run_id=run_id,
        message_count=len(messages),
        total_count=total_count,
        run_event_count=len(run_events),
        include_control_events=include_control_events,
        run_event_types=sorted(run_event_types),
        exclude_run_event_types=sorted(exclude_run_event_types),
    )
    return {
        "mission_id": mission_id,
        "node_id": resolved_node_id,
        "messages": messages,
        "run_events": run_events,
        "page_info": {
            "total_count": total_count,
            "has_more_before": total_count > len(messages),
            "has_more_after": False,
        },
        "source": {
            "session_id": session_id,
            "runtime_session_id": _text(binding.get("runtime_session_id")),
            "runtime_scope_key": _text(binding.get("runtime_scope_key")),
            "run_id": _text(binding.get("run_id")),
            "node_id": resolved_node_id,
        },
    }
