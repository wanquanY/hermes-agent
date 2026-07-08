"""Gateway-facing run event read-model accessors."""

from __future__ import annotations

from typing import Any

from hermes_agent.read_models.run_events import RunEventReadModel


def run_event_read_model_for_db(db: Any) -> RunEventReadModel | None:
    conn = getattr(db, "_conn", None)
    if conn is None:
        return None
    return RunEventReadModel(conn)


def list_runtime_events(
    db: Any,
    session_id: str,
    *,
    after_seq: int = 0,
    active_only: bool = False,
    runtime_scope_key: str = "",
    run_id: str = "",
    activity_id: str = "",
    limit: int = 2000,
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    read_model = run_event_read_model_for_db(db)
    if read_model is None:
        return []
    return read_model.list_runtime(
        session_id,
        after_seq=after_seq,
        active_only=active_only,
        active_statuses=("created", "queued", "running", "cancelling"),
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
    read_model = run_event_read_model_for_db(db)
    if read_model is None:
        return []
    return read_model.list_filtered(
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
    read_model = run_event_read_model_for_db(db)
    if read_model is None:
        return []
    return read_model.list_tool_events(
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
    read_model = run_event_read_model_for_db(db)
    if read_model is None:
        return []
    return read_model.list_activity_events(
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
    read_model = run_event_read_model_for_db(db)
    if read_model is None:
        return []
    return read_model.list_mission_activity_events(
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
    "run_event_read_model_for_db",
]
