"""Declarative baseline schema migration."""

from __future__ import annotations

import sqlite3

from hermes_agent.composition.migration_operations import reconcile_declared_columns
from hermes_agent.storage.state_schema import SCHEMA_SQL

version = 1
description = "declarative baseline schema"


def apply(cursor: sqlite3.Cursor) -> None:
    cursor.executescript(SCHEMA_SQL)
    reconcile_declared_columns(cursor)
