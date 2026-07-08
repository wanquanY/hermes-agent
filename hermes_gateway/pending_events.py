"""Pending follow-up queue helpers for gateway turns."""

from __future__ import annotations

from channels.platforms.base_models import MessageEvent


def dequeue_pending_event(adapter, session_key: str) -> MessageEvent | None:
    """Consume and return the full pending event for a session."""
    return adapter.get_pending_message(session_key)
