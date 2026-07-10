"""Add activity_id to run events for Activity Runtime."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.migration_operations import migrate_run_events_activity_id

version = 37
description = "run events activity id"


def apply(cursor: sqlite3.Cursor) -> None:
    migrate_run_events_activity_id(cursor)
