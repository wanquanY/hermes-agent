"""Guard legacy projected columns until the read-model migration lands."""

from __future__ import annotations

import sqlite3

version = 43
description = "projected run_event columns audit guard"

REQUIRED_COLUMNS = {
    "projected_message_id",
    "projected_tool_event_id",
}


def apply(cursor: sqlite3.Cursor) -> None:
    columns = {
        str(row[1])
        for row in cursor.execute('PRAGMA table_info("run_events")').fetchall()
    }
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise RuntimeError(
            "run_events projected columns are still consumed; missing: "
            + ", ".join(missing)
        )
