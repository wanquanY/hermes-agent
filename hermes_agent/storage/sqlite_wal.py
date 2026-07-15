"""SQLite WAL setup helpers owned by the storage layer."""

from __future__ import annotations

import logging
import sqlite3
import sys
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


def _apply_macos_checkpoint_barrier(conn: sqlite3.Connection) -> None:
    """Use the macOS full-fsync barrier at WAL checkpoint boundaries.

    ``fsync`` alone does not provide the ordering guarantee SQLite's WAL
    checkpoint protocol expects on macOS. ``checkpoint_fullfsync`` asks SQLite
    to issue ``F_FULLFSYNC`` for the main-database checkpoint without paying
    the cost on every WAL append. The pragma is intentionally best-effort so a
    connection can still open on older SQLite builds.
    """

    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA checkpoint_fullfsync=1")
    except sqlite3.OperationalError:
        logger.warning(
            "SQLite checkpoint_fullfsync could not be enabled on macOS",
            exc_info=True,
        )


def _enforce_macos_synchronous_full(conn: sqlite3.Connection) -> None:
    """Enforce durable SQLite commits on macOS.

    This must run for both newly-enabled and already-active WAL databases.
    Otherwise a later connection can silently return to ``NORMAL`` and expose
    the main database to partial checkpoint writes during forced shutdown.
    """

    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA synchronous=FULL")
    except sqlite3.OperationalError:
        logger.warning(
            "SQLite synchronous=FULL could not be enabled on macOS",
            exc_info=True,
        )


def apply_wal_with_fallback(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
) -> str:
    """Enable WAL with a safe fallback and platform durability policy."""

    try:
        current_mode = conn.execute("PRAGMA journal_mode").fetchone()
        if current_mode and str(current_mode[0]).lower() == "wal":
            _apply_macos_checkpoint_barrier(conn)
            _enforce_macos_synchronous_full(conn)
            return "wal"
    except sqlite3.OperationalError as exc:
        # The set-pragma below remains the authoritative operation. A failed
        # read-only probe must not turn an otherwise usable database into an
        # initialization failure.
        logger.debug(
            "%s: SQLite journal_mode probe failed before WAL setup: %s",
            db_label,
            exc,
        )

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return "wal"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in WAL_INCOMPAT_MARKERS):
            raise
        _log_wal_fallback_once(db_label, exc)
        conn.execute("PRAGMA journal_mode=DELETE")
        _enforce_macos_synchronous_full(conn)
        return "delete"


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
    busy_timeout_ms: int = 5000,
    foreign_keys: bool = True,
    enable_wal: bool = True,
) -> str:
    """Apply the canonical Hermes SQLite connection policy.

    Connection factories own this policy. Repositories and application
    services must not scatter or override durability pragmas after bootstrap.
    Returns the active journal mode when WAL setup is requested.
    """

    timeout_ms = max(0, int(busy_timeout_ms))
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    if enable_wal:
        return apply_wal_with_fallback(conn, db_label=db_label)
    _enforce_macos_synchronous_full(conn)
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else ""


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
    "configure_sqlite_connection",
    "wal_fallback_warned_paths",
]
