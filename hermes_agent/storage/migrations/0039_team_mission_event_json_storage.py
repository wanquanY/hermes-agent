"""Compact legacy duplicate Team Mission event JSON storage."""

from __future__ import annotations

import sqlite3

version = 39
description = "team mission event json storage"


def apply(cursor: sqlite3.Cursor) -> None:
    from hermes_state import compact_team_mission_event_json_storage
    from hermes_state import logger

    compact_team_mission_event_json_storage(cursor, logger)
