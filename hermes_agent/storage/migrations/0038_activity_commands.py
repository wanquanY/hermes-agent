"""Create durable Activity Command intent storage."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.migration_operations import migrate_activity_commands

version = 38
description = "activity commands"


def apply(cursor: sqlite3.Cursor) -> None:
    migrate_activity_commands(cursor)
