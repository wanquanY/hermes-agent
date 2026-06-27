# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from .common import *


@method("team_mission.memory.compile")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        result = db.compile_team_mission_memory(
            mission_id=mission_id,
            task_id=str(params.get("task_id") or params.get("taskId") or ""),
            mode=str(params.get("mode") or "final"),
            source_run_ids=_normalize_toolsets(params.get("source_run_ids") or params.get("sourceRunIds")),
            emit_event=not _falsey(params.get("emit_event") if "emit_event" in params else params.get("emitEvent")),
        )
    except Exception as exc:
        return _err(rid, 5008, f"team mission memory compile failed: {exc}")
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


@method("team_mission.memory.pack")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = db.build_team_mission_memory_pack(
        mission_id=mission_id,
        objective=str(params.get("objective") or ""),
        workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""),
        limit=_bounded_limit(params.get("limit"), default=8, maximum=50),
        include_team_scope=_team_memory_include_team_scope(params, {}),
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


@method("team_mission.memory.slice")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    node_id = _node_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    result = db.build_team_mission_memory_slice(
        mission_id=mission_id,
        node_id=node_id,
        objective=str(params.get("objective") or ""),
        limit=_bounded_limit(params.get("limit"), default=5, maximum=50),
        include_team_scope=_team_memory_include_team_scope(params, {}),
    )
    if not result:
        return _err(rid, 4040, "team mission or node not found")
    return _ok(rid, result)


@method("team_mission.memory.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    items = db.list_team_mission_memory_items(
        mission_id=_mission_id_from_params(params),
        conversation_session_id=str(
            params.get("conversation_session_id")
            or params.get("conversationSessionId")
            or ""
        ),
        team_id=str(params.get("team_id") or params.get("teamId") or ""),
        task_id=str(params.get("task_id") or params.get("taskId") or ""),
        kinds=_normalize_toolsets(params.get("kinds") or params.get("kind")),
        statuses=_normalize_toolsets(params.get("statuses") or params.get("status")),
        visibility=_normalize_toolsets(params.get("visibility")),
        include_deleted=bool(params.get("include_deleted") or params.get("includeDeleted")),
        limit=_bounded_limit(params.get("limit"), default=200, maximum=1000),
    )
    return _ok(rid, {"items": items})


@method("team_mission.memory.update")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    memory_id = str(params.get("memory_id") or params.get("memoryId") or params.get("id") or "").strip()
    if not memory_id:
        return _err(rid, 4006, "memory_id required")
    structured_payload = params.get("structured_payload") or params.get("structuredPayload")
    if structured_payload is not None and not isinstance(structured_payload, dict):
        return _err(rid, 4004, "structured_payload must be an object")
    try:
        item = db.update_team_mission_memory_item(
            memory_id,
            content=params.get("content") if "content" in params else None,
            structured_payload=structured_payload,
            visibility=params.get("visibility") if "visibility" in params else None,
            status=params.get("status") if "status" in params else None,
            confidence=float(params["confidence"]) if "confidence" in params else None,
        )
    except Exception as exc:
        return _err(rid, 5008, f"team mission memory update failed: {exc}")
    if not item:
        return _err(rid, 4040, "team mission memory item not found")
    return _ok(rid, {"item": item})


@method("team_mission.memory.delete")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    memory_id = str(params.get("memory_id") or params.get("memoryId") or params.get("id") or "").strip()
    if not memory_id:
        return _err(rid, 4006, "memory_id required")
    item = db.delete_team_mission_memory_item(memory_id)
    if not item:
        return _err(rid, 4040, "team mission memory item not found")
    return _ok(rid, {"item": item})


@method("team_mission.memory.events")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    events = [
        event for event in db.list_team_mission_events(
            mission_id,
            after_seq=after_seq,
            limit=_bounded_limit(params.get("limit"), default=2000, maximum=10000),
        )
        if str((event or {}).get("type") or "").startswith("mission.memory.")
    ]
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "events": events,
            "last_event_seq": max([int(event.get("seq") or 0) for event in events], default=after_seq),
        },
    )

# Export underscore-prefixed helpers for the compatibility facade.
__all__ = [name for name in globals() if not name.startswith("__")]
