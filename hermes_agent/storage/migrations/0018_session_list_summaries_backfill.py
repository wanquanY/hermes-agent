"""Backfill session list summary columns."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 18
description = "session list summaries backfill"


@dataclass(frozen=True)
class _SessionListSummariesBackfill:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._backfill_session_list_summaries(cursor)


def create_migration(context: MigrationContext) -> _SessionListSummariesBackfill:
    return _SessionListSummariesBackfill(
        owner=require_owner(context, "0018_session_list_summaries_backfill")
    )
