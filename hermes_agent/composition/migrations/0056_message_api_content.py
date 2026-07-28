"""Add exact provider-wire content sidecars to conversation messages."""

from __future__ import annotations

import sqlite3

version = 56
description = "add byte-stable provider replay sidecars"


def apply(cursor: sqlite3.Cursor) -> None:
    columns = {
        str(row[1])
        for row in cursor.execute('PRAGMA table_info("messages")').fetchall()
    }
    if "api_content" not in columns:
        cursor.execute("ALTER TABLE messages ADD COLUMN api_content TEXT")
