"""Backfill compressed run event frame/search columns."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 36
description = "run event frame indexes backfill"


@dataclass(frozen=True)
class _RunEventFrameIndexesBackfill:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._backfill_run_event_frame_indexes(cursor)


def create_migration(context: MigrationContext) -> _RunEventFrameIndexesBackfill:
    return _RunEventFrameIndexesBackfill(
        owner=require_owner(context, "0036_run_event_frame_indexes_backfill")
    )
