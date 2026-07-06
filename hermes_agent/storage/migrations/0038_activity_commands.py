"""Create durable Activity Command intent storage."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 38
description = "activity commands"


@dataclass(frozen=True)
class _ActivityCommands:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._migrate_activity_commands(cursor)


def create_migration(context: MigrationContext) -> _ActivityCommands:
    return _ActivityCommands(
        owner=require_owner(context, "0038_activity_commands")
    )
