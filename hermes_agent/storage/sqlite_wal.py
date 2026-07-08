"""SQLite WAL setup helpers owned by the storage layer."""

from __future__ import annotations

import logging
import sqlite3
import threading

logger = logging.getLogger(__name__)

# SQLite WAL relies on byte-range locks that do not reliably work on network
# filesystems such as NFS, SMB/CIFS, some FUSE mounts, and WSL1.
WAL_INCOMPAT_MARKERS = (
    "locking protocol",
    "not authorized",
    "disk i/o error",
)

wal_fallback_warned_paths: set[str] = set()
_wal_fallback_warned_lock = threading.Lock()


def apply_wal_with_fallback(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
) -> str:
    """Set ``journal_mode=WAL`` on ``conn``, falling back to DELETE on failure."""

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        return "wal"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in WAL_INCOMPAT_MARKERS):
            raise
        _log_wal_fallback_once(db_label, exc)
        conn.execute("PRAGMA journal_mode=DELETE")
        return "delete"


def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    with _wal_fallback_warned_lock:
        if db_label in wal_fallback_warned_paths:
            return
        wal_fallback_warned_paths.add(db_label)
    logger.warning(
        "%s: WAL journal_mode unsupported on this filesystem (%s) - "
        "falling back to journal_mode=DELETE (slower rollback-journal "
        "mode; reduces concurrency but works on NFS/SMB/FUSE). See "
        "https://www.sqlite.org/wal.html for details. This warning "
        "fires once per process per database.",
        db_label,
        exc,
    )


__all__ = [
    "WAL_INCOMPAT_MARKERS",
    "apply_wal_with_fallback",
    "wal_fallback_warned_paths",
]
