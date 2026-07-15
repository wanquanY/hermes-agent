"""Add durable transcript speaker identity."""

from __future__ import annotations

import sqlite3

from hermes_agent.composition.migration_operations import migrate_messages_participant_id

version = 30
description = "messages participant id"


def apply(cursor: sqlite3.Cursor) -> None:
    migrate_messages_participant_id(cursor)
