"""Create the session-scoped compression and stream stability aggregate."""

from __future__ import annotations

import sqlite3


version = 53
description = "persist session compression verdicts and stream stale circuit state"


def apply(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS session_runtime_stability (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            compression_ineffective_count INTEGER NOT NULL DEFAULT 0
                CHECK (compression_ineffective_count >= 0),
            compression_fallback_streak INTEGER NOT NULL DEFAULT 0
                CHECK (compression_fallback_streak >= 0),
            compression_verdict_pending INTEGER NOT NULL DEFAULT 0
                CHECK (compression_verdict_pending IN (0, 1)),
            compression_failure_cooldown_until REAL NOT NULL DEFAULT 0,
            compression_failure_error TEXT NOT NULL DEFAULT '',
            stream_stale_failures INTEGER NOT NULL DEFAULT 0
                CHECK (stream_stale_failures >= 0),
            stream_stale_retry_after REAL NOT NULL DEFAULT 0,
            stream_stale_route_hash TEXT NOT NULL DEFAULT '',
            stream_stale_last_error TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0
        )
        """
    )


__all__ = ["apply", "description", "version"]
