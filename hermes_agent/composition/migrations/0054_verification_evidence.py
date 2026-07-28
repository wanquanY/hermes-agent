"""Create workspace verification evidence and aggregate state."""

from __future__ import annotations

import sqlite3


version = 54
description = "persist workspace verification evidence and freshness generations"


def apply(cursor: sqlite3.Cursor) -> None:
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS verification_workspace_state (
            scope_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            edit_generation INTEGER NOT NULL DEFAULT 0 CHECK (edit_generation >= 0),
            last_verified_generation INTEGER NOT NULL DEFAULT -1,
            last_event_id INTEGER,
            changed_paths_json TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (scope_id, workspace_root)
        );

        CREATE TABLE IF NOT EXISTS verification_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            edit_generation INTEGER NOT NULL CHECK (edit_generation >= 0),
            command TEXT NOT NULL,
            canonical_command TEXT NOT NULL,
            kind TEXT NOT NULL,
            evidence_scope TEXT NOT NULL CHECK (evidence_scope IN ('targeted', 'full')),
            status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
            exit_code INTEGER NOT NULL,
            cwd TEXT NOT NULL,
            output_summary TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_verification_evidence_scope_root
            ON verification_evidence(scope_id, workspace_root, id DESC);
        """
    )


__all__ = ["apply", "description", "version"]
