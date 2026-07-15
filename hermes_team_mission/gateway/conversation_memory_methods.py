# ruff: noqa: F401,F403,F405,F821,ARG001
"""Canonical Conversation memory and context-summary gateway contract."""

from __future__ import annotations

from .common import *


def _actor(db, params: dict) -> tuple[str, str, str]:
    conversation_session_id = str(
        params.get("conversation_session_id") or params.get("conversationSessionId") or ""
    ).strip()
    participant_id = str(
        params.get("participant_id") or params.get("participantId") or ""
    ).strip()
    participant = (
        db.participants.get_participant(conversation_session_id, participant_id)
        if conversation_session_id and participant_id
        else {}
    ) or {}
    return conversation_session_id, participant_id, str(participant.get("role") or "").strip()


@method("conversation.memory.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    conversation_session_id, participant_id, actor_role = _actor(db, params)
    if not conversation_session_id or not participant_id:
        return _err(rid, 4006, "conversation_session_id and participant_id required")
    if not actor_role:
        return _err(rid, 4030, "participant is not active in this conversation")
    statuses = tuple(_normalize_toolsets(params.get("statuses") or params.get("status"))) or ("committed",)
    resolved = db.conversation_memory.resolve_visible(
        MemoryAccessContext(
            conversation_session_id=conversation_session_id,
            actor_participant_id=participant_id,
            actor_role=actor_role,
            activity_id=str(params.get("activity_id") or params.get("activityId") or "").strip(),
            node_id=str(params.get("node_id") or params.get("nodeId") or "").strip(),
            profile_id=str(params.get("profile_id") or params.get("profileId") or "").strip(),
        ),
        statuses=statuses,
        limit=_bounded_limit(params.get("limit"), default=100, maximum=1000),
    )
    return _ok(rid, resolved)


@method("conversation.memory.propose")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    conversation_session_id, participant_id, actor_role = _actor(db, params)
    if not conversation_session_id or not participant_id or not actor_role:
        return _err(rid, 4030, "active conversation participant required")
    owner_kind = str(params.get("owner_kind") or params.get("ownerKind") or "participant").strip()
    owner_id = str(params.get("owner_id") or params.get("ownerId") or participant_id).strip()
    activity_id = str(params.get("activity_id") or params.get("activityId") or "").strip()
    node_id = str(params.get("node_id") or params.get("nodeId") or "").strip()
    if owner_kind == "participant" and owner_id != participant_id:
        return _err(rid, 4030, "a participant may only propose its own private memory")
    if owner_kind in {"conversation", "activity", "node"} and actor_role != "leader":
        return _err(rid, 4030, "only the Leader may propose shared or activity memory")
    if owner_kind == "conversation":
        visibility = {"kind": "conversation"}
    elif owner_kind == "activity":
        visibility = {"kind": "activity", "activity_id": activity_id or owner_id}
    elif owner_kind == "node":
        visibility = {"kind": "node", "activity_id": activity_id, "node_id": node_id or owner_id}
    else:
        visibility = {"kind": "private", "participant_id": participant_id}
    try:
        item = db.conversation_memory.create_item(
            conversation_session_id=conversation_session_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            participant_id=participant_id if owner_kind == "participant" else "",
            activity_id=activity_id,
            node_id=node_id,
            kind=str(params.get("kind") or "fact"),
            content=str(params.get("content") or ""),
            structured_payload=params.get("structured_payload") or params.get("structuredPayload") or {},
            visibility=visibility,
            provenance=params.get("provenance") or {},
            confidence=float(params.get("confidence") or 0.5),
            status="proposed",
            memory_id=str(params.get("memory_id") or params.get("memoryId") or ""),
        )
    except (ValueError, RuntimeError) as exc:
        return _err(rid, 4004, str(exc))
    return _ok(rid, {"item": item})


@method("conversation.memory.commit")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    conversation_session_id, _, actor_role = _actor(db, params)
    if actor_role != "leader":
        return _err(rid, 4030, "only the Leader may commit shared memory")
    memory_id = str(params.get("memory_id") or params.get("memoryId") or "").strip()
    item = db.conversation_memory.get_item(memory_id)
    if not item or item.get("conversation_session_id") != conversation_session_id:
        return _err(rid, 4040, "conversation memory item not found")
    if item.get("owner_kind") == "participant":
        return _err(rid, 4030, "private participant memory cannot be promoted to shared memory")
    try:
        committed = db.conversation_memory.transition_item(
            memory_id,
            expected_revision=int(params.get("expected_revision") or params.get("expectedRevision") or item.get("revision") or 0),
            status="committed",
        )
    except RuntimeError as exc:
        return _err(rid, 4090, str(exc))
    return _ok(rid, {"item": committed})


@method("conversation.memory.invalidate")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    conversation_session_id, participant_id, actor_role = _actor(db, params)
    memory_id = str(params.get("memory_id") or params.get("memoryId") or "").strip()
    item = db.conversation_memory.get_item(memory_id)
    if not item or item.get("conversation_session_id") != conversation_session_id:
        return _err(rid, 4040, "conversation memory item not found")
    if actor_role != "leader" and not (
        item.get("owner_kind") == "participant" and item.get("owner_id") == participant_id
    ):
        return _err(rid, 4030, "memory invalidation is not authorized for this participant")
    try:
        invalidated = db.conversation_memory.transition_item(
            memory_id,
            expected_revision=int(params.get("expected_revision") or params.get("expectedRevision") or item.get("revision") or 0),
            status="invalidated",
        )
    except RuntimeError as exc:
        return _err(rid, 4090, str(exc))
    return _ok(rid, {"item": invalidated})


@method("conversation.context.summary.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    conversation_session_id, participant_id, actor_role = _actor(db, params)
    if not conversation_session_id or not participant_id or not actor_role:
        return _err(rid, 4030, "active conversation participant required")
    activity_id = str(params.get("activity_id") or params.get("activityId") or "").strip()
    node_id = str(params.get("node_id") or params.get("nodeId") or "").strip()
    attempt_id = str(params.get("attempt_id") or params.get("attemptId") or "").strip()
    result = {
        "actor": db.conversation_memory.latest_actor_summary(
            conversation_session_id, participant_id
        )
    }
    if activity_id:
        activity = db.conversation_memory.latest_activity_summary(activity_id)
        result["activity"] = (
            activity
            if activity.get("conversation_session_id") == conversation_session_id
            else {}
        )
    if activity_id and node_id and attempt_id:
        node = db.conversation_memory.latest_node_attempt_summary(
            activity_id, node_id, attempt_id
        )
        result["node"] = (
            node if node.get("conversation_session_id") == conversation_session_id else {}
        )
    return _ok(rid, result)
