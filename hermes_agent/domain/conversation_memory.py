"""Participant-aware conversation memory contracts and policy validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


OWNER_KINDS = frozenset({"profile", "participant", "conversation", "activity", "node"})
MEMORY_KINDS = frozenset(
    {
        "fact",
        "preference",
        "decision",
        "commitment",
        "constraint",
        "risk",
        "open_question",
        "artifact",
        "summary",
    }
)
MEMORY_STATUSES = frozenset({"proposed", "committed", "superseded", "invalidated"})
VISIBILITY_KINDS = frozenset(
    {"private", "conversation", "activity", "node", "participants", "leader_only"}
)


def text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class MemoryAccessContext:
    conversation_session_id: str
    actor_participant_id: str
    actor_role: str
    activity_id: str = ""
    node_id: str = ""
    profile_id: str = ""

    def __post_init__(self) -> None:
        if not text(self.conversation_session_id):
            raise ValueError("conversation_session_id required")
        if not text(self.actor_participant_id):
            raise ValueError("actor_participant_id required")


def normalize_visibility(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    kind = text(raw.get("kind") or "conversation").lower()
    if kind not in VISIBILITY_KINDS:
        raise ValueError(f"unsupported memory visibility: {kind}")
    result: dict[str, Any] = {"kind": kind}
    if kind == "private":
        participant_id = text(raw.get("participant_id") or raw.get("participantId"))
        if not participant_id:
            raise ValueError("private visibility requires participant_id")
        result["participant_id"] = participant_id
    elif kind == "activity":
        activity_id = text(raw.get("activity_id") or raw.get("activityId"))
        if not activity_id:
            raise ValueError("activity visibility requires activity_id")
        result["activity_id"] = activity_id
    elif kind == "node":
        node_id = text(raw.get("node_id") or raw.get("nodeId"))
        if not node_id:
            raise ValueError("node visibility requires node_id")
        result["node_id"] = node_id
    elif kind == "participants":
        values = raw.get("participant_ids") or raw.get("participantIds") or []
        participant_ids = sorted({text(item) for item in values if text(item)})
        if not participant_ids:
            raise ValueError("participants visibility requires participant_ids")
        result["participant_ids"] = participant_ids
    return result


def validate_memory_identity(
    *,
    owner_kind: str,
    owner_id: str,
    kind: str,
    status: str,
) -> None:
    if text(owner_kind) not in OWNER_KINDS:
        raise ValueError(f"unsupported memory owner_kind: {owner_kind}")
    if not text(owner_id):
        raise ValueError("memory owner_id required")
    if text(kind) not in MEMORY_KINDS:
        raise ValueError(f"unsupported memory kind: {kind}")
    if text(status) not in MEMORY_STATUSES:
        raise ValueError(f"unsupported memory status: {status}")


def visibility_allows(visibility: Mapping[str, Any], context: MemoryAccessContext) -> bool:
    normalized = normalize_visibility(visibility)
    kind = normalized["kind"]
    if kind == "conversation":
        return True
    if kind == "private":
        return normalized["participant_id"] == context.actor_participant_id
    if kind == "leader_only":
        return text(context.actor_role).lower() == "leader"
    if kind == "activity":
        return bool(context.activity_id) and normalized["activity_id"] == context.activity_id
    if kind == "node":
        return bool(context.node_id) and normalized["node_id"] == context.node_id
    if kind == "participants":
        return context.actor_participant_id in normalized["participant_ids"]
    return False


def memory_conflict_sets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return explicit semantic conflicts without guessing from similarity."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        payload = item.get("structured_payload")
        payload = payload if isinstance(payload, dict) else {}
        semantic_key = text(
            payload.get("conflict_key")
            or payload.get("conflictKey")
            or payload.get("subject")
            or payload.get("key")
        )
        if not semantic_key:
            continue
        key = f"{text(item.get('kind'))}:{semantic_key}"
        groups.setdefault(key, []).append(item)
    conflicts: list[dict[str, Any]] = []
    for key, candidates in sorted(groups.items()):
        contents = {" ".join(text(item.get("content")).split()).casefold() for item in candidates}
        if len(contents) <= 1:
            continue
        conflicts.append(
            {
                "conflict_key": key,
                "memory_item_ids": [
                    text(item.get("memory_id"))
                    for item in candidates
                    if text(item.get("memory_id"))
                ],
                "candidates": candidates,
            }
        )
    return conflicts


__all__ = [
    "MEMORY_KINDS",
    "MEMORY_STATUSES",
    "MemoryAccessContext",
    "OWNER_KINDS",
    "VISIBILITY_KINDS",
    "normalize_visibility",
    "memory_conflict_sets",
    "validate_memory_identity",
    "visibility_allows",
]
