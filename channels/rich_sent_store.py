"""In-process index for rich outbound Telegram messages.

Telegram reply payloads do not always echo text for rich messages that Hermes
sent earlier. The Telegram adapter records sent rich content here so a later
reply can recover the original text for ``MessageEvent.reply_to_text``.
"""

from __future__ import annotations

import time
from collections import OrderedDict

_MAX_ENTRIES = 2048
_TTL_SECONDS = 24 * 60 * 60
_StoreKey = tuple[str, str]
_entries: "OrderedDict[_StoreKey, tuple[float, str]]" = OrderedDict()


def _prune(now: float) -> None:
    expired_before = now - _TTL_SECONDS
    while _entries:
        _key, (created_at, _content) = next(iter(_entries.items()))
        if created_at >= expired_before and len(_entries) <= _MAX_ENTRIES:
            break
        _entries.popitem(last=False)


def record(chat_id: str, message_id: str, content: str) -> None:
    """Record rich outbound content by Telegram chat/message id."""
    key = (str(chat_id), str(message_id))
    now = time.time()
    _entries.pop(key, None)
    _entries[key] = (now, str(content or ""))
    _prune(now)


def lookup(chat_id: str, message_id: str) -> str | None:
    """Return previously recorded content, or ``None`` if absent/expired."""
    key = (str(chat_id), str(message_id))
    now = time.time()
    item = _entries.get(key)
    if item is None:
        _prune(now)
        return None
    created_at, content = item
    if created_at < now - _TTL_SECONDS:
        _entries.pop(key, None)
        return None
    _entries.move_to_end(key)
    return content or None


def clear() -> None:
    """Clear the in-process index. Intended for tests."""
    _entries.clear()
