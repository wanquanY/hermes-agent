"""Declarative baseline schema migration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 1
description = "declarative baseline schema"


@dataclass(frozen=True)
class _DeclarativeBaseline:
    owner: object
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        from hermes_state import SCHEMA_SQL

        cursor.executescript(SCHEMA_SQL)
        reconcile_columns = getattr(self.owner, "_reconcile_columns")
        reconcile_columns(cursor)


def create_migration(context: MigrationContext) -> _DeclarativeBaseline:
    return _DeclarativeBaseline(
        owner=require_owner(context, "0001_declarative_baseline")
    )

