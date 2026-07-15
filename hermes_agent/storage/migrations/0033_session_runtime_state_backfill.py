"""Backfill latest-only session runtime state."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.migration_operations import backfill_session_runtime_state

version = 33
description = "session runtime state backfill"


def apply(cursor: sqlite3.Cursor) -> None:
    backfill_session_runtime_state(cursor)
