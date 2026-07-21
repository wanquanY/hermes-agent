"""Persist session Git placement for project grouping and remote gateways."""

from __future__ import annotations

import sqlite3


version = 58
description = "persist session git branch and common repository root"


def _columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    return {
        str(row[1])
        for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
    }


def apply(cursor: sqlite3.Cursor) -> None:
    columns = _columns(cursor, "sessions")
    if not columns:
        return
    if "git_branch" not in columns:
        cursor.execute(
            "ALTER TABLE sessions ADD COLUMN git_branch TEXT NOT NULL DEFAULT ''"
        )
    if "git_repo_root" not in columns:
        cursor.execute(
            "ALTER TABLE sessions ADD COLUMN git_repo_root TEXT NOT NULL DEFAULT ''"
        )


__all__ = ["apply", "description", "version"]
