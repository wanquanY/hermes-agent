"""Gateway-facing run event read-model accessors."""

from __future__ import annotations

from typing import Any

def list_runtime_events(
    db: Any,
    session_id: str,
    *,
    after_seq: int = 0,
    before_seq: int = 0,
    active_only: bool = False,
    runtime_scope_key: str = "",
    run_id: str = "",
    activity_id: str = "",
    limit: int = 2000,
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    return db.runs.list_events(
        session_id,
        after_seq=after_seq,
        before_seq=before_seq,
        active_only=active_only,
        runtime_scope_key=runtime_scope_key,
        run_id=run_id,
        activity_id=activity_id,
        limit=limit,
        include_internal=include_internal,
    )


def list_filtered_events(
    db: Any,
    session_id: str,
    *,
    after_seq: int = 0,
    runtime_scope_key: str = "",
    event_type_prefix: str = "",
    event_types: tuple[str, ...] | list[str] | None = None,
    payload_contains: str = "",
    limit: int = 2000,
) -> list[dict[str, Any]]:
    return db.runs.list_filtered_events(
        session_id,
        after_seq=after_seq,
        runtime_scope_key=runtime_scope_key,
        event_type_prefix=event_type_prefix,
        event_types=event_types,
        payload_contains=payload_contains,
        limit=limit,
    )


def list_tool_events(
    db: Any,
    session_id: str,
    *,
    after_seq: int = 0,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    return db.runs.list_tool_events(
        session_id,
        after_seq=after_seq,
        limit=limit,
    )


def list_activity_events(
    db: Any,
    activity_id: str,
    *,
    after_seq: int = 0,
    limit: int = 2000,
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    return db.runs.list_events_by_activity(
        activity_id,
        after_seq=after_seq,
        limit=limit,
        include_internal=include_internal,
    )


def list_mission_activity_events(
    db: Any,
    mission_id: str,
    *,
    after_seq: int = 0,
    limit: int = 2000,
    include_internal: bool = False,
    reverse: bool = False,
) -> list[dict[str, Any]]:
    return db.runs.list_events_by_mission_activity(
        mission_id,
        after_seq=after_seq,
        limit=limit,
        include_internal=include_internal,
        reverse=reverse,
    )


__all__ = [
    "list_activity_events",
    "list_filtered_events",
    "list_mission_activity_events",
    "list_runtime_events",
    "list_tool_events",
]
