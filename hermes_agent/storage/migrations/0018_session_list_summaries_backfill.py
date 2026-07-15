"""Backfill session list summary columns."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.migration_operations import backfill_session_list_summaries

version = 18
description = "session list summaries backfill"


def apply(cursor: sqlite3.Cursor) -> None:
    backfill_session_list_summaries(cursor)
