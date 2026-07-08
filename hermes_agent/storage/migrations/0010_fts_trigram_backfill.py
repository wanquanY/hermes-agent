"""Backfill trigram FTS rows for existing messages."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.fts_schema import FTS_TRIGRAM_SQL

version = 10
description = "fts trigram backfill"


def apply(cursor: sqlite3.Cursor) -> None:
    # v10: trigram FTS5 table for CJK/substring search. The
    # virtual table + triggers are created unconditionally via
    # FTS_TRIGRAM_SQL below, but existing rows need a one-time
    # backfill into the FTS index.
    try:
        cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
        _fts_trigram_exists = True
    except sqlite3.OperationalError:
        _fts_trigram_exists = False
    if not _fts_trigram_exists:
        cursor.executescript(FTS_TRIGRAM_SQL)
        cursor.execute(
            "INSERT INTO messages_fts_trigram(rowid, content) "
            "SELECT id, content FROM messages WHERE content IS NOT NULL"
        )
