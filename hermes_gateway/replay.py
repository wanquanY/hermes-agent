"""Gateway transcript replay helpers."""
from __future__ import annotations

from typing import Any, Dict


ASSISTANT_REPLAY_FIELDS: tuple[str, ...] = (
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
    "finish_reason",
)


def build_replay_entry(role: str, content: Any, msg: Dict[str, Any]) -> Dict[str, Any]:
    """Build a replay entry while preserving assistant continuity fields."""
    entry: Dict[str, Any] = {"role": role, "content": content}
    api_content = msg.get("api_content")
    if role in {"user", "assistant"} and isinstance(api_content, str) and api_content:
        entry["api_content"] = api_content
    if role == "assistant":
        for key in ASSISTANT_REPLAY_FIELDS:
            if key not in msg:
                continue
            value = msg.get(key)
            if key == "reasoning_content":
                if value is None:
                    continue
            elif not value:
                continue
            entry[key] = value
    return entry
