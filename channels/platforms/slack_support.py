"""Shared Slack adapter support types."""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field
from typing import Optional

# ContextVar carrying the user_id of the slash-command invoker.
_slash_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_slash_user_id", default=None,
)


@dataclass
class _ThreadContextCache:
    """Cache entry for fetched thread context."""

    content: str
    fetched_at: float = field(default_factory=time.monotonic)
    message_count: int = 0
    parent_text: str = ""
