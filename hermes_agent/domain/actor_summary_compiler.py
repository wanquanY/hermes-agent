"""Deterministic, attribution-preserving ActorContextSummary compiler."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(message: dict[str, Any]) -> dict[str, Any]:
    value = message.get("metadata")
    return value if isinstance(value, dict) else {}


def _participant_id(message: dict[str, Any]) -> str:
    metadata = _metadata(message)
    return _text(
        message.get("participant_id")
        or metadata.get("participant_id")
        or metadata.get("participantId")
    )


def _source_event_id(message: dict[str, Any]) -> str:
    metadata = _metadata(message)
    return _text(
        message.get("event_id")
        or metadata.get("event_id")
        or metadata.get("message_id")
        or message.get("message_id")
        or metadata.get("persist_message_key")
    )


def _activity_id(message: dict[str, Any]) -> str:
    metadata = _metadata(message)
    return _text(
        message.get("activity_id")
        or metadata.get("activity_id")
        or metadata.get("activityId")
    )


def _excerpt(message: dict[str, Any], limit: int = 800) -> str:
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    normalized = content.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def compile_actor_context_summary(
    messages: list[dict[str, Any]],
    *,
    actor_participant_id: str,
) -> dict[str, Any]:
    """Compile speaker-safe excerpts without inventing semantic ownership.

    Higher-level memory curation may later extract decisions or commitments,
    but the compression boundary first preserves exact author attribution and
    provenance deterministically.
    """
    actor = _text(actor_participant_id)
    actor_statements: list[dict[str, Any]] = []
    user_requirements: list[dict[str, Any]] = []
    others: dict[str, list[dict[str, Any]]] = defaultdict(list)
    activities: dict[str, list[str]] = defaultdict(list)
    source_event_ids: list[str] = []
    seen_sources: set[str] = set()

    for index, message in enumerate(messages or [], start=1):
        if not isinstance(message, dict):
            continue
        role = _text(message.get("role"))
        if role not in {"user", "assistant"}:
            continue
        excerpt = _excerpt(message)
        if not excerpt:
            continue
        participant_id = _participant_id(message)
        source_event_id = _source_event_id(message)
        if source_event_id and source_event_id not in seen_sources:
            seen_sources.add(source_event_id)
            source_event_ids.append(source_event_id)
        record = {
            "participant_id": participant_id,
            "role": role,
            "excerpt": excerpt,
            "source_event_ids": [source_event_id] if source_event_id else [],
            "ordinal": index,
        }
        if role == "user":
            user_requirements.append(record)
        elif participant_id and participant_id == actor:
            actor_statements.append(record)
        else:
            others[participant_id or "unknown"].append(record)
        activity_id = _activity_id(message)
        if activity_id and source_event_id:
            activities[activity_id].append(source_event_id)

    return {
        "actor_participant_id": actor,
        "user_requirements": user_requirements[-20:],
        "actor_statements": actor_statements[-20:],
        "other_participant_statements": [
            {
                "participant_id": participant_id,
                "statements": records[-20:],
                "source_event_ids": [
                    source
                    for record in records[-20:]
                    for source in record["source_event_ids"]
                ],
            }
            for participant_id, records in sorted(others.items())
        ],
        "activity_summaries": [
            {"activity_id": activity_id, "source_event_ids": list(dict.fromkeys(sources))}
            for activity_id, sources in sorted(activities.items())
        ],
        "source_event_ids": source_event_ids,
    }


__all__ = ["compile_actor_context_summary"]
