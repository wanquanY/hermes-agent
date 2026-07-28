"""Persist the worker/profile dimensions of canonical run identity."""

from __future__ import annotations

import sqlite3


version = 52
description = "persist canonical run worker and agent profile identity"


def _columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    return {
        str(row[1])
        for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
    }


def apply(cursor: sqlite3.Cursor) -> None:
    columns = _columns(cursor, "runs")
    if not columns:
        return
    if "worker_id" not in columns:
        cursor.execute(
            "ALTER TABLE runs ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''"
        )
    if "agent_profile_id" not in columns:
        cursor.execute(
            "ALTER TABLE runs ADD COLUMN agent_profile_id TEXT NOT NULL DEFAULT ''"
        )


__all__ = ["apply", "description", "version"]
