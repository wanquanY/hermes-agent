"""Process-wide health state for the canonical session store bootstrap."""

from __future__ import annotations

import threading

from hermes_agent.storage.sqlite_wal import WAL_INCOMPAT_MARKERS


_last_init_error: str | None = None
_last_init_error_lock = threading.Lock()


def set_last_init_error(message: str | None) -> None:
    """Record or explicitly clear the latest session-store bootstrap error."""
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = message


def get_last_init_error() -> str | None:
    with _last_init_error_lock:
        return _last_init_error


def format_session_db_unavailable(
    prefix: str = "Session database not available",
) -> str:
    cause = get_last_init_error()
    if not cause:
        return f"{prefix}."
    hint = ""
    if any(marker in cause.lower() for marker in WAL_INCOMPAT_MARKERS):
        hint = " (state.db may be on NFS/SMB/FUSE - see https://www.sqlite.org/wal.html)"
    return f"{prefix}: {cause}{hint}."


__all__ = [
    "format_session_db_unavailable",
    "get_last_init_error",
    "set_last_init_error",
]
