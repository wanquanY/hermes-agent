"""Rebuild FTS tables with inline tool metadata coverage."""

from __future__ import annotations

import logging
import sqlite3

from hermes_agent.storage.fts_schema import FTS_SQL
from hermes_agent.storage.fts_schema import FTS_TRIGRAM_SQL

version = 11
description = "fts reindex"


_logger = logging.getLogger(__name__)


def apply(cursor: sqlite3.Cursor) -> None:
    # v11: re-index FTS5 tables to cover tool_name + tool_calls and
    # switch from external-content to inline mode. Existing DBs have
    # old-schema FTS tables and triggers that IF NOT EXISTS won't
    # overwrite, so we drop them explicitly and let the post-migration
    # existence checks (below) recreate them from FTS_SQL /
    # FTS_TRIGRAM_SQL, then backfill every message row. Fixes #16751.
    for _trig in (
        "messages_fts_insert",
        "messages_fts_delete",
        "messages_fts_update",
        "messages_fts_trigram_insert",
        "messages_fts_trigram_delete",
        "messages_fts_trigram_update",
    ):
        try:
            cursor.execute(f"DROP TRIGGER IF EXISTS {_trig}")
        except sqlite3.OperationalError as exc:
            _logger.debug(
                "migration 0011 DROP TRIGGER %s skipped (legacy schema tolerance): %s",
                _trig,
                exc,
            )
    for _tbl in ("messages_fts", "messages_fts_trigram"):
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {_tbl}")
        except sqlite3.OperationalError as exc:
            _logger.debug(
                "migration 0011 DROP TABLE %s skipped (legacy schema tolerance): %s",
                _tbl,
                exc,
            )
    # Recreate virtual tables + triggers with the new inline-mode
    # schema that indexes content || tool_name || tool_calls.
    cursor.executescript(FTS_SQL)
    cursor.executescript(FTS_TRIGRAM_SQL)
    # Backfill both indexes from every existing messages row.
    cursor.execute(
        "INSERT INTO messages_fts(rowid, content) "
        "SELECT id, "
        "COALESCE(content, '') || ' ' || "
        "COALESCE(tool_name, '') || ' ' || "
        "COALESCE(tool_calls, '') "
        "FROM messages"
    )
    cursor.execute(
        "INSERT INTO messages_fts_trigram(rowid, content) "
        "SELECT id, "
        "COALESCE(content, '') || ' ' || "
        "COALESCE(tool_name, '') || ' ' || "
        "COALESCE(tool_calls, '') "
        "FROM messages"
    )
