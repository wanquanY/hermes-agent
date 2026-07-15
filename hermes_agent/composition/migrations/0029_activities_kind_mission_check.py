"""Rebuild activities kind CHECK to accept mission rows."""

from __future__ import annotations

import sqlite3

from hermes_agent.composition.migration_operations import migrate_activities_kind_mission_check

version = 29
description = "activities kind mission check"


def apply(cursor: sqlite3.Cursor) -> None:
    migrate_activities_kind_mission_check(cursor)
