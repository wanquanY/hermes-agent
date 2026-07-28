from __future__ import annotations

import json
import sqlite3

from hermes_agent.composition.migrations import load_migrations
from hermes_conversation_message_identity import user_conversation_message_id_for


def _migration():
    return next(record.migration for record in load_migrations() if record.version == 61)


def test_backfills_runtime_owned_user_message_identity() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            conversation_message_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT
        );
        CREATE UNIQUE INDEX idx_messages_conversation_message_id
            ON messages(session_id, conversation_message_id)
            WHERE conversation_message_id != '';
        """
    )
    metadata = {
        "run_id": "run-1",
        "turn_id": "turn-1",
        "client_message_id": "client-message-1",
    }
    connection.execute(
        """
        INSERT INTO messages (
            session_id, role, conversation_message_id, metadata_json
        ) VALUES (?, 'user', '', ?)
        """,
        ("conversation-1", json.dumps(metadata)),
    )

    _migration().apply(connection.cursor())

    row = connection.execute(
        "SELECT conversation_message_id FROM messages"
    ).fetchone()
    assert row["conversation_message_id"] == user_conversation_message_id_for(
        session_id="conversation-1",
        turn_id="turn-1",
        run_id="run-1",
        client_message_id="client-message-1",
    )
