"""Persist interaction lifecycle metadata on internal run_events."""

from __future__ import annotations

import sqlite3

version = 42
description = "interaction lifecycle internal run_events"


def apply(cursor: sqlite3.Cursor) -> None:
    columns = {
        str(row[1])
        for row in cursor.execute('PRAGMA table_info("run_events")').fetchall()
    }
    additions = (
        ("interaction_request_id", "TEXT"),
        ("interaction_kind", "TEXT"),
        ("interaction_status", "TEXT"),
        ("anchor_seq", "INTEGER NOT NULL DEFAULT 0"),
    )
    for name, ddl in additions:
        if name not in columns:
            cursor.execute(f'ALTER TABLE run_events ADD COLUMN {name} {ddl}')

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_events_interaction_request
            ON run_events(interaction_request_id, seq)
            WHERE interaction_request_id IS NOT NULL AND interaction_request_id != ''
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_events_interaction_pending
            ON run_events(session_id, interaction_status, seq)
            WHERE interaction_request_id IS NOT NULL AND interaction_request_id != ''
        """
    )
