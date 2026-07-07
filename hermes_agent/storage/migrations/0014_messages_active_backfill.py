"""Backfill the messages active flag."""

from __future__ import annotations

import logging
import sqlite3

version = 14
description = "messages active backfill"


_logger = logging.getLogger(__name__)


def apply(cursor: sqlite3.Cursor) -> None:
    try:
        cursor.execute("UPDATE messages SET active = 1 WHERE active IS NULL")
    except sqlite3.OperationalError as exc:
        # Legacy DBs may not yet have an ``active`` column at migration time;
        # a later reconcile pass adds it. Tolerated but recorded.
        _logger.debug("migration 0014 UPDATE skipped (column absent): %s", exc)
