"""Create session system prompt cache storage."""

from __future__ import annotations

import sqlite3

from hermes_agent.composition.migration_operations import migrate_session_system_prompts

version = 31
description = "session system prompts"


def apply(cursor: sqlite3.Cursor) -> None:
    migrate_session_system_prompts(cursor)
