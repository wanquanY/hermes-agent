"""Backfill the messages active flag."""

from __future__ import annotations

import sqlite3

version = 14
description = "messages active backfill"


def apply(cursor: sqlite3.Cursor) -> None:
    try:
        cursor.execute("UPDATE messages SET active = 1 WHERE active IS NULL")
    except sqlite3.OperationalError:
        pass
