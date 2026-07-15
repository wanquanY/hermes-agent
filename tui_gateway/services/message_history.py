"""Gateway-facing accessors for canonical message history read models."""

from __future__ import annotations

from typing import Any

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


__all__ = [
    "load_conversation_history",
]
