"""Backfill compressed run event frame/search columns."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.migration_operations import backfill_run_event_frame_indexes

version = 36
description = "run event frame indexes backfill"


def apply(cursor: sqlite3.Cursor) -> None:
    backfill_run_event_frame_indexes(cursor)
