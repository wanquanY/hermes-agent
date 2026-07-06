"""Rebuild activities kind CHECK to accept mission rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 29
description = "activities kind mission check"


@dataclass(frozen=True)
class _ActivitiesKindMissionCheck:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._migrate_activities_kind_mission_check(cursor)


def create_migration(context: MigrationContext) -> _ActivitiesKindMissionCheck:
    return _ActivitiesKindMissionCheck(
        owner=require_owner(context, "0029_activities_kind_mission_check")
    )
