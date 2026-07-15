"""Add team mission seq counters and v3 activity projection tables."""

from __future__ import annotations

import sqlite3
import time


version = 46
description = "team_mission_events seq counter and v3 activities"


def apply(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS v3_activities (
            activity_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (
                kind IN (
                    'async_agent_dispatch',
                    'async_team_dispatch',
                    'team_mission_activity',
                    'dispatch_completion'
                )
            ),
            activity_seq INTEGER NOT NULL CHECK (activity_seq >= 1),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
            ),
            target_id TEXT NOT NULL DEFAULT '',
            prompt_summary TEXT,
            result_summary TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL DEFAULT 0,
            completed_at REAL,
            metadata_json TEXT,
            UNIQUE(session_id, activity_seq)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v3_activities_session_seq
            ON v3_activities(session_id, activity_seq)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v3_activities_kind_status_seq
            ON v3_activities(kind, status, activity_seq)
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS team_mission_event_seq_counter (
            mission_id TEXT PRIMARY KEY REFERENCES team_missions(mission_id) ON DELETE CASCADE,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO team_mission_event_seq_counter (mission_id, next_seq, updated_at)
        SELECT
            team_missions.mission_id,
            COALESCE(MAX(team_mission_events.seq), 0) + 1,
            ?
        FROM team_missions
        LEFT JOIN team_mission_events
          ON team_mission_events.mission_id = team_missions.mission_id
        GROUP BY team_missions.mission_id
        ON CONFLICT(mission_id) DO UPDATE SET
            next_seq = MAX(team_mission_event_seq_counter.next_seq, excluded.next_seq),
            updated_at = MAX(team_mission_event_seq_counter.updated_at, excluded.updated_at)
        """,
        (time.time(),),
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_team_mission_event_seq_counter_updated
            ON team_mission_event_seq_counter(updated_at DESC)
        """
    )
