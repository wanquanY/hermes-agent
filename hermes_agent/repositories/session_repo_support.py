"""Shared sanitization and SQLite helpers for the Session aggregate."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from hermes_agent.domain.text_safety import scrub_lone_surrogates
from hermes_agent.repositories.base import RepositoryConnection

MAX_SESSION_TITLE_LENGTH = 100


def sanitize_session_title(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
        "",
        scrub_lone_surrogates(str(title)),
    )
    cleaned = re.sub(
        r"[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_SESSION_TITLE_LENGTH:
        raise ValueError(
            f"Title too long ({len(cleaned)} chars, max {MAX_SESSION_TITLE_LENGTH})"
        )
    return cleaned


def sanitize_title(title: str) -> str:
    return sanitize_session_title(title) or ""


def table_columns(conn: RepositoryConnection, table_name: str) -> set[str]:
    try:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
    except Exception:
        return set()


def table_exists(conn: RepositoryConnection, table_name: str) -> bool:
    return bool(table_columns(conn, table_name))


def column_exists(
    conn: RepositoryConnection, table_name: str, column_name: str
) -> bool:
    return column_name in table_columns(conn, table_name)


def affected(cursor: Any) -> int:
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def row_text(row: Any, key: str, index: int) -> str:
    if isinstance(row, sqlite3.Row):
        return str(row[key] or "")
    return str(row[index] or "")


def row_int(row: Any, key: str, index: int) -> int:
    if isinstance(row, sqlite3.Row):
        return int(row[key] or 0)
    return int(row[index] or 0)


def row_any(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, sqlite3.Row):
        try:
            return row[key]
        except (KeyError, IndexError):
            return default
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


__all__ = [
    "affected",
    "column_exists",
    "row_any",
    "row_int",
    "row_text",
    "sanitize_session_title",
    "sanitize_title",
    "table_columns",
    "table_exists",
]
