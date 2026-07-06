"""Backfill latest-only session runtime state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 33
description = "session runtime state backfill"


@dataclass(frozen=True)
class _SessionRuntimeStateBackfill:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._backfill_session_runtime_state(cursor)


def create_migration(context: MigrationContext) -> _SessionRuntimeStateBackfill:
    return _SessionRuntimeStateBackfill(
        owner=require_owner(context, "0033_session_runtime_state_backfill")
    )
