"""SQLite FTS schema owned by the storage layer."""

from __future__ import annotations

import sqlite3

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

# Trigram FTS5 table for CJK substring search. The default unicode61 tokenizer
# splits CJK characters into individual tokens, breaking phrase matching.
FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""


def ensure_message_fts(conn: sqlite3.Connection) -> None:
    """Ensure FTS tables/triggers exist and match the canonical message rows."""

    cursor = conn.cursor()
    cursor.executescript(FTS_SQL)
    cursor.executescript(FTS_TRIGRAM_SQL)
    message_count = int(cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    for table in ("messages_fts", "messages_fts_trigram"):
        indexed_count = int(cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if indexed_count == message_count:
            continue
        cursor.execute(f"DELETE FROM {table}")
        cursor.execute(
            f"""
            INSERT INTO {table}(rowid, content)
            SELECT id,
                   COALESCE(content, '') || ' ' ||
                   COALESCE(tool_name, '') || ' ' ||
                   COALESCE(tool_calls, '')
            FROM messages
            """
        )


def is_fts_write_corruption_error(exc: sqlite3.DatabaseError) -> bool:
    """Return whether a message write failed through corrupt FTS5 shadows."""
    message = str(exc).lower()
    return (
        "database disk image is malformed" in message
        or ("fts5" in message and "corrupt" in message)
    )


def rebuild_message_fts(conn: sqlite3.Connection) -> int:
    """Recreate both indexes from canonical ``messages`` rows.

    These are ordinary content-bearing FTS tables, not external-content
    tables, so FTS5's special ``rebuild`` command is not applicable. Dropping
    and recreating the derived tables is the deterministic equivalent and
    never mutates canonical transcript rows.
    """
    cursor = conn.cursor()
    for trigger in (
        "messages_fts_insert",
        "messages_fts_delete",
        "messages_fts_update",
        "messages_fts_trigram_insert",
        "messages_fts_trigram_delete",
        "messages_fts_trigram_update",
    ):
        cursor.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    rebuilt = 0
    for table in ("messages_fts", "messages_fts_trigram"):
        row = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is not None:
            cursor.execute(f"DROP TABLE {table}")
            rebuilt += 1
    cursor.executescript(FTS_SQL)
    cursor.executescript(FTS_TRIGRAM_SQL)
    cursor.execute(
        """
        INSERT INTO messages_fts(rowid, content)
        SELECT id,
               COALESCE(content, '') || ' ' ||
               COALESCE(tool_name, '') || ' ' ||
               COALESCE(tool_calls, '')
        FROM messages
        """
    )
    cursor.execute(
        """
        INSERT INTO messages_fts_trigram(rowid, content)
        SELECT id,
               COALESCE(content, '') || ' ' ||
               COALESCE(tool_name, '') || ' ' ||
               COALESCE(tool_calls, '')
        FROM messages
        """
    )
    return rebuilt


__all__ = [
    "FTS_SQL",
    "FTS_TRIGRAM_SQL",
    "ensure_message_fts",
    "is_fts_write_corruption_error",
    "rebuild_message_fts",
]
