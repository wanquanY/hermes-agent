"""Add terminal_seq / terminal_degraded columns to runs (Phase C spec §7.2)."""

from __future__ import annotations

import sqlite3

version = 45
description = "runs.terminal_seq + runs.terminal_degraded for RunStateMachine.terminate_run"


_COLUMNS = (
    ("terminal_seq", "INTEGER NOT NULL DEFAULT 0"),
    ("terminal_degraded", "INTEGER NOT NULL DEFAULT 0"),
    ("terminal_cause", "TEXT NOT NULL DEFAULT ''"),
)


def apply(cursor: sqlite3.Cursor) -> None:
    for column, ddl in _COLUMNS:
        try:
            cursor.execute(f"ALTER TABLE runs ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_terminal_degraded
            ON runs(session_id, run_id)
            WHERE terminal_degraded = 1
        """
    )
