"""Immutable parent identity captured for delegated child event routing."""

from __future__ import annotations

from typing import Any


def capture_parent_event_origin(parent_agent: Any) -> dict[str, str]:
    """Freeze the parent turn identity before a child can outlive that turn."""

    def string_attr(owner: Any, name: str) -> str:
        value = getattr(owner, name, "")
        return value.strip() if isinstance(value, str) else ""

    origin = {
        key: value
        for key, value in {
            "run_id": string_attr(parent_agent, "_hermes_active_run_id"),
            "turn_id": string_attr(parent_agent, "_hermes_active_turn_id"),
            "client_message_id": string_attr(
                parent_agent, "_hermes_active_client_message_id"
            ),
            "runtime_scope_key": string_attr(
                parent_agent, "_hermes_active_runtime_scope_key"
            ),
        }.items()
        if value
    }
    run_context = getattr(parent_agent, "run_context", None) or getattr(
        parent_agent, "_run_context", None
    )
    activity_id = string_attr(run_context, "activity_id") if run_context else ""
    if activity_id:
        origin["activity_id"] = activity_id
    return origin
