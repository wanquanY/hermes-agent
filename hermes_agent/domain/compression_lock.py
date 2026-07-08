"""In-process compression lock adapter for desktop runtime."""

from __future__ import annotations

import time
import threading
from typing import Any

_compression_locks: dict[str, dict[str, Any]] = {}
_compression_locks_lock = threading.Lock()


def try_acquire_compression_lock(
    _store: Any,
    session_id: str,
    holder: str,
    ttl_seconds: float = 300.0,
) -> bool:
    if not session_id:
        return False
    now = time.time()
    with _compression_locks_lock:
        existing = _compression_locks.get(session_id)
        if existing and float(existing["expires_at"]) > now:
            return existing["holder"] == holder
        _compression_locks[session_id] = {
            "holder": holder,
            "expires_at": now + ttl_seconds,
        }
        return True


def release_compression_lock(_store: Any, session_id: str, holder: str) -> None:
    if not session_id:
        return
    with _compression_locks_lock:
        existing = _compression_locks.get(session_id)
        if existing and existing["holder"] == holder:
            del _compression_locks[session_id]


def get_compression_lock_holder(_store: Any, session_id: str) -> str | None:
    if not session_id:
        return None
    now = time.time()
    with _compression_locks_lock:
        existing = _compression_locks.get(session_id)
        if not existing:
            return None
        if float(existing["expires_at"]) <= now:
            del _compression_locks[session_id]
            return None
        return str(existing["holder"] or "")


def install_compression_lock_methods(store_cls: type[Any]) -> None:
    store_cls.try_acquire_compression_lock = try_acquire_compression_lock
    store_cls.release_compression_lock = release_compression_lock
    store_cls.get_compression_lock_holder = get_compression_lock_holder
