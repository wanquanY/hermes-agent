from __future__ import annotations

import json
import sqlite3

from hermes_agent.composition.migrations import load_migrations


def _migration_59():
    return next(record.migration for record in load_migrations() if record.version == 59)


def test_recovers_segmented_reasoning_without_overwriting_durable_reasoning() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    cursor.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            reasoning TEXT,
            reasoning_content TEXT,
            metadata_json TEXT
        );
        CREATE TABLE run_events (
            session_id TEXT NOT NULL,
            run_id TEXT,
            turn_id TEXT,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT
        );
        """
    )
    turn_id = "turn:run-1"
    messages = [
        (1, "", None, 0),
        (2, None, " ", 1),
        (3, "already durable", None, 2),
    ]
    for message_id, reasoning, reasoning_content, segment_index in messages:
        cursor.execute(
            "INSERT INTO messages VALUES (?, 'session-1', 'assistant', ?, ?, ?)",
            (
                message_id,
                reasoning,
                reasoning_content,
                json.dumps(
                    {
                        "run_id": "run-1",
                        "turn_id": turn_id,
                        "assistant_segment_index": segment_index,
                    }
                ),
            ),
        )
    for seq, segment_index, text in (
        (4, 0, "first segment reasoning"),
        (8, 1, "second segment reasoning"),
        (9, 2, "must not overwrite"),
    ):
        cursor.execute(
            "INSERT INTO run_events VALUES "
            "('session-1', 'run-1', ?, ?, 'reasoning.available', ?)",
            (
                turn_id,
                seq,
                json.dumps(
                    {
                        "client_message_id": (
                            f"turn:run-1:assistant-segment:{segment_index}"
                        ),
                        "text": text,
                    }
                ),
            ),
        )

    _migration_59().apply(cursor)

    rows = cursor.execute(
        "SELECT id, reasoning, metadata_json FROM messages ORDER BY id"
    ).fetchall()
    assert rows[0]["reasoning"] == "first segment reasoning"
    assert rows[1]["reasoning"] == "second segment reasoning"
    assert rows[2]["reasoning"] == "already durable"
    assert json.loads(rows[0]["metadata_json"])["reasoning_recovery"] == {
        "migration_version": 59,
        "source_event_seq": 4,
    }


def test_missing_tables_are_a_safe_noop() -> None:
    connection = sqlite3.connect(":memory:")
    _migration_59().apply(connection.cursor())
