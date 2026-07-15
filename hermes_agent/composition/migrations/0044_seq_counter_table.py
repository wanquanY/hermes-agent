"""Add persistent per-session run_events seq counters."""

from __future__ import annotations

import sqlite3
import time

from hermes_agent.domain.seq_allocator import backfill_seq_counter

version = 44
description = "persistent per-session run event seq allocator"


def apply(cursor: sqlite3.Cursor) -> None:
    backfill_seq_counter(cursor.connection, updated_at=time.time())
