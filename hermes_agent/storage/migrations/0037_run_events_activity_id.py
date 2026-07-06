"""Add activity_id to run events for Activity Runtime."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 37
description = "run events activity id"


@dataclass(frozen=True)
class _RunEventsActivityId:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._migrate_run_events_activity_id(cursor)


def create_migration(context: MigrationContext) -> _RunEventsActivityId:
    return _RunEventsActivityId(
        owner=require_owner(context, "0037_run_events_activity_id")
    )
