"""Gateway-facing accessors for canonical message history read models."""

from __future__ import annotations

from typing import Any

from hermes_agent.read_models.message_history import MessageHistoryReadModel
from hermes_agent.repositories.message_repo import MessageRepository


def message_history_read_model_for_db(db: Any) -> MessageHistoryReadModel | None:
    conn = getattr(db, "_conn", None)
    if conn is None:
        return None
    return MessageHistoryReadModel(conn)


def message_repository_for_db(db: Any) -> MessageRepository | None:
    conn = getattr(db, "_conn", None)
    if conn is None:
        return None
    return MessageRepository(conn)


def load_conversation_history(
    db: Any,
    session_id: str,
    *,
    include_ancestors: bool = False,
    include_storage_metadata: bool = False,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    read_model = message_history_read_model_for_db(db)
    if read_model is None:
        return []
    return read_model.all_as_conversation(
        session_id,
        include_ancestors=include_ancestors,
        include_storage_metadata=include_storage_metadata,
        include_inactive=include_inactive,
    )


__all__ = [
    "load_conversation_history",
    "message_history_read_model_for_db",
    "message_repository_for_db",
]
