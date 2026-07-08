from __future__ import annotations

import json
from typing import Any

from agent.dovie_diagnostics import emit_dovie_diagnostic
from hermes_agent.domain.run_event_codec import decode_run_event_row
from hermes_team_mission.runtime.team_transcript_writer import is_node_transcript_message


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int = 0, maximum: int = 5000) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _cursor_value(value: Any) -> int:
    """Parse a cursor (after_seq/before_seq/after_id/before_id) value.

    Returns 0 for missing/invalid input, which means "no cursor" (current
    behaviour) — keeping the call site backward compatible.
    """
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


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


def _node_activity_id(mission_id: str, node_id: str) -> str:
    mission_id = _text(mission_id)
    node_id = _text(node_id)
    return f"act-node:{mission_id}:{node_id}" if mission_id and node_id else ""


def _metadata_activity_id(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    run_context = metadata.get("run_context") if isinstance(metadata.get("run_context"), dict) else {}
    return _text(
        metadata.get("activity_id")
        or metadata.get("activityId")
        or run_context.get("activity_id")
        or run_context.get("activityId")
    )


def _metadata_node_id(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    team_mission = metadata.get("team_mission") if isinstance(metadata.get("team_mission"), dict) else {}
    return _text(
        metadata.get("node_id")
        or metadata.get("nodeId")
        or team_mission.get("node_id")
        or team_mission.get("nodeId")
    )


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
    row_id = int(_row_value(row, "id", 0) or 0)
    message = {
        "id": row_id,
        "role": role,
        "message_id": _text(_row_value(row, "platform_message_id", "")) or str(_row_value(row, "id", "")),
        "timestamp": float(_row_value(row, "timestamp", 0) or 0),
        "text": str(content or ""),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    conversation_message_id = _text(_row_value(row, "conversation_message_id", ""))
    if conversation_message_id:
        message["conversation_message_id"] = conversation_message_id
    participant_id = _text(_row_value(row, "participant_id", ""))
    if participant_id:
        message["participant_id"] = participant_id
    tool_call_id = _text(_row_value(row, "tool_call_id", ""))
    if tool_call_id:
        message["tool_call_id"] = tool_call_id
    tool_calls = _row_value(row, "tool_calls", "")
    if tool_calls:
        parsed_tool_calls = _json_loads(tool_calls, [])
        if isinstance(parsed_tool_calls, list):
            message["tool_calls"] = parsed_tool_calls
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


def _message_matches_node_filter(message: dict[str, Any], *, activity_id: str, node_id: str) -> bool:
    if not is_node_transcript_message(message):
        return False
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if activity_id and _metadata_activity_id(metadata) != activity_id:
        return False
    if node_id:
        message_node_id = _metadata_node_id(metadata)
        if message_node_id and message_node_id != node_id:
            return False
    return True


def _fetch_recent_messages(
    db: Any,
    session_id: str,
    *,
    limit: int,
    activity_id: str = "",
    node_id: str = "",
    after_id: int = 0,
    before_id: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    if not session_id or not hasattr(db, "_conn"):
        return [], 0
    bounded_limit = _bounded_int(limit, default=50, minimum=1, maximum=500)
    after_id = _cursor_value(after_id)
    before_id = _cursor_value(before_id)
    with db._lock:
        active_column = db._conn.execute("PRAGMA table_info(messages)").fetchall()
        has_active = any(_text(_row_value(row, "name")) == "active" for row in active_column)
        active_clause = "AND active = 1" if has_active else ""
        if activity_id:
            # activity_id branch: full scan + Python-side node filter, then slice.
            cursor_clause = ""
            cursor_params: list[Any] = []
            if after_id > 0:
                cursor_clause = "AND id > ?"
                cursor_params.append(after_id)
            elif before_id > 0:
                cursor_clause = "AND id < ?"
                cursor_params.append(before_id)
            rows = db._conn.execute(
                f"""
                SELECT *
                FROM messages
                WHERE session_id = ?
                  {active_clause}
                  {cursor_clause}
                  AND metadata_json LIKE ?
                ORDER BY id ASC
                """,
                (session_id, *cursor_params, f"%{activity_id}%"),
            ).fetchall()
            filtered = [
                message
                for message in (_message_from_row(db, row) for row in rows)
                if _message_matches_node_filter(message, activity_id=activity_id, node_id=node_id)
            ]
            if after_id > 0:
                # forward cursor: oldest-first, take the first N (earliest new)
                return filtered[:bounded_limit], len(filtered)
            if before_id > 0:
                # backward cursor: ASC order, take the newest N under the cursor
                return filtered[-bounded_limit:], len(filtered)
            return filtered[-bounded_limit:], len(filtered)
        total = db._conn.execute(
            f"SELECT count(*) AS count FROM messages WHERE session_id = ? {active_clause}",
            (session_id,),
        ).fetchone()
        if after_id > 0:
            # forward cursor: only messages with id > after_id, oldest first
            rows = db._conn.execute(
                f"""
                SELECT *
                FROM messages
                WHERE session_id = ? {active_clause}
                  AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, after_id, bounded_limit),
            ).fetchall()
        elif before_id > 0:
            # backward cursor: messages with id < before_id, newest first, then reverse to ASC
            rows = db._conn.execute(
                f"""
                SELECT * FROM (
                    SELECT *
                    FROM messages
                    WHERE session_id = ? {active_clause}
                      AND id < ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (session_id, before_id, bounded_limit),
            ).fetchall()
        else:
            # default: recent N by id DESC, then reverse to ASC
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
    event = decode_run_event_row(row)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
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
    activity_id: str = "",
    after_seq: int = 0,
    before_seq: int = 0,
) -> list[dict[str, Any]]:
    if not session_id or not hasattr(db, "_conn"):
        return []
    bounded_limit = _bounded_int(limit, default=0, minimum=0, maximum=5000)
    if bounded_limit <= 0:
        return []
    after_seq = _cursor_value(after_seq)
    before_seq = _cursor_value(before_seq)
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
    normalized_activity_id = _text(activity_id)
    if normalized_activity_id:
        clauses.append("activity_id = ?")
        params.append(normalized_activity_id)
    where_clause = "\n                  AND ".join(clauses)
    with db._lock:
        if after_seq > 0:
            # forward cursor: seq > after_seq, oldest first (no DESC subquery)
            params.append(after_seq)
            params.append(bounded_limit)
            rows = db._conn.execute(
                f"""
                SELECT *
                FROM run_events
                WHERE {where_clause}
                  AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        elif before_seq > 0:
            # backward cursor: seq < before_seq, newest first, then reverse to ASC
            params.append(before_seq)
            params.append(bounded_limit)
            rows = db._conn.execute(
                f"""
                SELECT * FROM (
                    SELECT *
                    FROM run_events
                    WHERE {where_clause}
                      AND seq < ?
                    ORDER BY seq DESC
                    LIMIT ?
                ) ORDER BY seq ASC
                """,
                tuple(params),
            ).fetchall()
        else:
            # default: recent N by seq DESC, then reverse to ASC
            params.append(bounded_limit)
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
    # Cursor params: after_seq/before_seq (run_events.seq domain) and
    # after_id/before_id (messages.id domain). Default 0 = current behaviour
    # (recent N). camelCase aliases for frontend convenience.
    requested_after_seq = _cursor_value(
        params.get("after_seq") or params.get("afterSeq")
    )
    requested_before_seq = _cursor_value(
        params.get("before_seq") or params.get("beforeSeq")
    )
    requested_after_id = _cursor_value(
        params.get("after_id") or params.get("afterId")
    )
    requested_before_id = _cursor_value(
        params.get("before_id") or params.get("beforeId")
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
    resolved_node_id = _text(binding.get("node_id")) or node_id
    binding_metadata = _json_loads(binding.get("metadata_json") or binding.get("metadata"), {})
    if not isinstance(binding_metadata, dict):
        binding_metadata = {}
    node_activity_id = (
        _text(params.get("activity_id") or params.get("activityId"))
        or _text(binding_metadata.get("activity_id") or binding_metadata.get("activityId"))
        or _node_activity_id(mission_id, resolved_node_id)
    )
    mission_metadata: dict[str, Any] = {}
    if isinstance(graph, dict):
        mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
        mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    candidate_session_ids: list[str] = []

    def _add_candidate(value: Any) -> None:
        candidate = _text(value)
        if candidate and candidate not in candidate_session_ids:
            candidate_session_ids.append(candidate)

    _add_candidate(requested_session_id)
    _add_candidate(binding.get("session_id"))
    _add_candidate(binding.get("runtime_session_id"))
    _add_candidate(conversation_session_id)
    _add_candidate(mission_metadata.get("conversation_session_id") or mission_metadata.get("conversationSessionId"))
    _add_candidate(mission_metadata.get("stable_session_id") or mission_metadata.get("stableSessionId"))
    run_id = _text(binding.get("run_id") or params.get("run_id") or params.get("runId"))
    include_control_events = bool(params.get("include_control_events") or params.get("includeControlEvents"))
    session_id = ""
    messages: list[dict[str, Any]] = []
    total_count = 0
    for candidate_session_id in candidate_session_ids:
        candidate_messages, candidate_total_count = _fetch_recent_messages(
            db,
            candidate_session_id,
            limit=requested_limit,
            activity_id=node_activity_id,
            node_id=resolved_node_id,
            after_id=requested_after_id,
            before_id=requested_before_id,
        )
        if candidate_messages:
            session_id = candidate_session_id
            messages = candidate_messages
            total_count = candidate_total_count
            break
    run_events: list[dict[str, Any]] = []
    if not session_id and include_run_events:
        for candidate_session_id in candidate_session_ids:
            candidate_run_events = _fetch_recent_run_events(
                db,
                candidate_session_id,
                run_id=run_id,
                limit=requested_run_events_limit,
                include_control_events=include_control_events,
                event_types=run_event_types,
                exclude_event_types=exclude_run_event_types,
                activity_id=node_activity_id,
                after_seq=requested_after_seq,
                before_seq=requested_before_seq,
            )
            if candidate_run_events:
                session_id = candidate_session_id
                run_events = candidate_run_events
                break
    if not session_id and candidate_session_ids:
        session_id = candidate_session_ids[0]
    _trace_history(
        "resolved",
        mission_id=mission_id,
        graph_found=bool(graph),
        graph_node_count=len(graph.get("nodes") or []) if isinstance(graph, dict) else 0,
        graph_binding_count=len(graph.get("run_bindings") or []) if isinstance(graph, dict) else 0,
        binding_found=bool(binding),
        node_id=node_id,
        resolved_node_id=resolved_node_id,
        activity_id=node_activity_id,
        requested_session_id=requested_session_id,
        resolved_session_id=session_id,
        candidate_session_ids=candidate_session_ids,
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
            "page_info": {
                "total_count": 0,
                "has_more_before": False,
                "has_more_after": False,
                "last_run_event_seq": 0,
                "last_message_id": 0,
            },
            "source": {"session_id": "", "runtime_session_id": "", "runtime_scope_key": "", "run_id": ""},
        }

    if not messages:
        messages, total_count = _fetch_recent_messages(
            db,
            session_id,
            limit=requested_limit,
            activity_id=node_activity_id,
            node_id=resolved_node_id,
            after_id=requested_after_id,
            before_id=requested_before_id,
        )
    if include_run_events and not run_events:
        run_events = _fetch_recent_run_events(
            db,
            session_id,
            run_id=run_id,
            limit=requested_run_events_limit,
            include_control_events=include_control_events,
            event_types=run_event_types,
            exclude_event_types=exclude_run_event_types,
            activity_id=node_activity_id,
            after_seq=requested_after_seq,
            before_seq=requested_before_seq,
        )
    last_run_event_seq = max((int(event.get("seq") or 0) for event in run_events), default=0)
    last_message_id = max((int(message.get("id") or 0) for message in messages), default=0)
    page_info = {
        "total_count": total_count,
        "has_more_before": total_count > len(messages),
        "has_more_after": False,
        "last_run_event_seq": last_run_event_seq,
        "last_message_id": last_message_id,
    }
    source = {
        "session_id": session_id,
        "runtime_session_id": _text(binding.get("runtime_session_id")),
        "runtime_scope_key": _text(binding.get("runtime_scope_key")),
        "run_id": _text(binding.get("run_id")),
        "node_id": resolved_node_id,
        "activity_id": node_activity_id,
    }
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
        "tool_events": [],
        "page_info": page_info,
        "source": source,
    }
