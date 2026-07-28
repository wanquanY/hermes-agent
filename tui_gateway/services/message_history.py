"""Gateway-facing accessors for canonical message history read models."""

from __future__ import annotations

from typing import Any

from hermes_agent.domain.transcript_visibility import (
    is_public_transcript_message,
)


def filter_public_conversation_history(
    messages: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """Filter an in-memory runtime buffer through the public transcript policy."""

    return [
        message
        for message in messages
        if is_public_transcript_message(message)
    ]


def load_conversation_history(
    db: Any,
    session_id: str,
    *,
    include_ancestors: bool = False,
    include_storage_metadata: bool = False,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    return db.messages.all_as_conversation(
        session_id,
        include_ancestors=include_ancestors,
        include_storage_metadata=include_storage_metadata,
        include_inactive=include_inactive,
    )


def load_runtime_conversation_history(
    db: Any,
    session_id: str,
    *,
    include_ancestors: bool = False,
    include_storage_metadata: bool = False,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """Load model-facing history, including durable internal context."""

    return db.messages.runtime_as_conversation(
        session_id,
        include_ancestors=include_ancestors,
        include_storage_metadata=include_storage_metadata,
        include_inactive=include_inactive,
    )


__all__ = [
    "filter_public_conversation_history",
    "load_conversation_history",
    "load_runtime_conversation_history",
]
