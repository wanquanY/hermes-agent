"""Backfill tool event timeline rows."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.migration_operations import backfill_tool_events

version = 34
description = "tool events backfill"


def apply(cursor: sqlite3.Cursor) -> None:
    backfill_tool_events(cursor)
