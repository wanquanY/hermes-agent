"""Backfill tool event timeline rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 34
description = "tool events backfill"


@dataclass(frozen=True)
class _ToolEventsBackfill:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._backfill_tool_events(cursor)


def create_migration(context: MigrationContext) -> _ToolEventsBackfill:
    return _ToolEventsBackfill(
        owner=require_owner(context, "0034_tool_events_backfill")
    )
