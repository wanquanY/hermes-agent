"""Add durable speaker identity to run events."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.migration_operations import migrate_run_events_participant_id

version = 28
description = "run events participant id"


def apply(cursor: sqlite3.Cursor) -> None:
    migrate_run_events_participant_id(cursor)
