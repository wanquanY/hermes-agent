"""Phase M — drop ``run_events.runtime_source_seq`` (spec §12 Phase M).

Blocked on the frontend Phase H3 landing (v3.1 §8.3 — DEBT-2). While
``capabilities.deprecations`` does NOT yet include ``"runtimeSourceSeq"``,
the frontend still relies on ``runtimeSourceSeq`` as a fallback anchor, so
dropping the column would silently break replay hydration.

When frontend Phase H3 lands:

1. ``timeline_contract_capabilities()['deprecations']`` will start returning
   ``["runtimeSourceSeq"]`` (backend advertises the deprecation).
2. Frontend consumes ``capabilities.deprecations.includes("runtimeSourceSeq")``
   and removes its fallback path.
3. This migration is activated by removing the ``FrozenMigrationError`` raise
   below.
4. The concrete SQL below (index drop + ALTER TABLE ... DROP COLUMN) then
   runs on next startup and permanently retires the column.

SQLite ADD/DROP COLUMN is supported from 3.35+; the harness on macOS ships
3.43+.
"""

from __future__ import annotations

import logging
import sqlite3

version = 47
description = "drop runtime_source_seq — DEBT-2, pending frontend Phase H3"


_logger = logging.getLogger(__name__)


class FrozenMigrationError(RuntimeError):
    """Raised while the migration is deliberately parked awaiting frontend H3."""


# Flip this to ``False`` in the same PR that removes the frontend fallback and
# advertises ``"runtimeSourceSeq"`` in ``timeline_contract_capabilities``.
PENDING_FRONTEND_H3 = True


def apply(cursor: sqlite3.Cursor) -> None:
    if PENDING_FRONTEND_H3:
        raise FrozenMigrationError(
            "0047_drop_runtime_source_seq is parked — frontend Phase H3 must "
            "land first (spec §12 Phase M). Do not enable this migration "
            "without confirming the frontend runtimeSourceSeq fallback is "
            "removed and the handshake advertises the deprecation."
        )

    _drop_dependencies(cursor)
    _drop_column(cursor)


def _drop_dependencies(cursor: sqlite3.Cursor) -> None:
    """Drop the covering index that references ``runtime_source_seq``."""
    try:
        cursor.execute("DROP INDEX IF EXISTS idx_run_events_runtime_source_seq")
    except sqlite3.OperationalError as exc:
        _logger.debug(
            "migration 0047 index drop skipped (already gone): %s", exc
        )


def _drop_column(cursor: sqlite3.Cursor) -> None:
    """Drop ``run_events.runtime_source_seq`` — requires SQLite 3.35+."""
    try:
        cursor.execute("ALTER TABLE run_events DROP COLUMN runtime_source_seq")
    except sqlite3.OperationalError as exc:
        # Older SQLite (<3.35) needs the CREATE NEW → INSERT SELECT → DROP →
        # RENAME dance. Defer that fallback until we actually hit it.
        raise RuntimeError(
            "sqlite version does not support DROP COLUMN — implement the "
            "table-rebuild fallback before shipping Phase M activation"
        ) from exc
