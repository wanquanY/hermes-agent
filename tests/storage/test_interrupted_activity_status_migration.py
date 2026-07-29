from __future__ import annotations

from importlib import import_module
import sqlite3


def test_migration_preserves_rows_and_adds_interrupted_terminal_status() -> None:
    migration = import_module(
        "hermes_agent.composition.migrations."
        "0062_distinguish_interrupted_activities"
    )
    connection = sqlite3.connect(":memory:")
    cursor = connection.cursor()
    cursor.executescript(
        """
        CREATE TABLE activities (
            activity_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            parent_activity_id TEXT,
            kind TEXT NOT NULL CHECK (
                kind IN (
                    'chat',
                    'agent_dispatch',
                    'team_dispatch',
                    'member_chat',
                    'mission'
                )
            ),
            target_profile_id TEXT,
            target_team_id TEXT,
            target_mission_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN (
                    'pending',
                    'running',
                    'completed',
                    'failed',
                    'cancelled'
                )
            ),
            prompt_summary TEXT,
            result_summary TEXT,
            result_json TEXT,
            started_at REAL,
            completed_at REAL,
            notify_parent INTEGER NOT NULL DEFAULT 1,
            read_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX idx_activities_conv
            ON activities(conversation_id, status);
        CREATE INDEX idx_activities_parent
            ON activities(parent_activity_id, status);
        CREATE UNIQUE INDEX idx_activities_mission
            ON activities(target_mission_id)
            WHERE kind = 'mission'
              AND COALESCE(target_mission_id, '') != '';
        INSERT INTO activities (
            activity_id,
            conversation_id,
            kind,
            status,
            created_at,
            updated_at
        ) VALUES (
            'activity-existing',
            'conversation-1',
            'agent_dispatch',
            'cancelled',
            1,
            2
        );
        """
    )

    migration.apply(cursor)
    cursor.execute(
        """
        INSERT INTO activities (
            activity_id,
            conversation_id,
            kind,
            status,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "activity-interrupted",
            "conversation-1",
            "agent_dispatch",
            "interrupted",
            3,
            4,
        ),
    )

    assert cursor.execute(
        "SELECT activity_id, status FROM activities ORDER BY activity_id"
    ).fetchall() == [
        ("activity-existing", "cancelled"),
        ("activity-interrupted", "interrupted"),
    ]
    assert {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name LIKE 'idx_activities_%'"
        )
    } == {
        "idx_activities_conv",
        "idx_activities_parent",
        "idx_activities_mission",
    }
