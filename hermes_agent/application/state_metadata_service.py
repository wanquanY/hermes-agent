"""Typed access to repository state metadata."""

from __future__ import annotations

import sqlite3

from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class StateMetadataService:
    def __init__(self, conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork) -> None:
        self._conn = conn
        self._unit_of_work = unit_of_work

    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (str(key),),
        ).fetchone()
        return str(row["value"]) if row else None

    def set(self, key: str, value: str) -> None:
        self._unit_of_work.execute(
            lambda conn: conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
        )


__all__ = ["StateMetadataService"]
