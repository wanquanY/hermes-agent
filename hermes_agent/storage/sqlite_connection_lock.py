"""Shared per-connection locks for sqlite handles used across gateway owners."""

from __future__ import annotations

import sqlite3
import threading

_LOCKS: dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for_connection(conn: sqlite3.Connection) -> threading.RLock:
    key = id(conn)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


__all__ = ["lock_for_connection"]
