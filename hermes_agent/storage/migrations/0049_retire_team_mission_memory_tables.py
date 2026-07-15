"""Retire the former Mission-specific memory fact source after backfill."""

from __future__ import annotations

import sqlite3


version = 49
description = "retire duplicate team mission memory tables"


def apply(cursor: sqlite3.Cursor) -> None:
    # Migration 0048 copies every legacy row into ConversationMemory first.
    # From this version onward there is exactly one durable memory fact source.
    cursor.execute("DROP TABLE IF EXISTS team_mission_memory_edges")
    cursor.execute("DROP TABLE IF EXISTS team_mission_memory_items")


__all__ = ["apply", "description", "version"]
