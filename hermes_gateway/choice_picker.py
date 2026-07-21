"""Shared finite-choice picker dispatch for gateway commands."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from channels.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


ChoiceCallback = Callable[[str, str], Awaitable[str]]


async def try_send_choice_picker(
    runner: Any,
    event: MessageEvent,
    *,
    session_key: str,
    title: str,
    choices: list[dict[str, Any]],
    on_choice_selected: ChoiceCallback,
) -> bool:
    """Send through an adapter's native picker and fall back on failure."""
    adapter = (getattr(runner, "adapters", None) or {}).get(event.source.platform)
    if adapter is None or getattr(type(adapter), "send_choice_picker", None) is None:
        return False

    try:
        metadata = runner._thread_metadata_for_source(
            event.source,
            runner._reply_anchor_for_event(event),
        )
        metadata = dict(metadata or {})
        if event.source.user_id:
            metadata.setdefault("requester_user_id", str(event.source.user_id))
        result = await adapter.send_choice_picker(
            chat_id=event.source.chat_id,
            title=title,
            choices=choices,
            session_key=session_key,
            on_choice_selected=on_choice_selected,
            metadata=metadata or None,
        )
        return bool(getattr(result, "success", False))
    except Exception as exc:
        logger.warning("send_choice_picker failed, falling back to text: %s", exc)
        return False
