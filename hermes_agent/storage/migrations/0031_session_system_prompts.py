"""Create session system prompt cache storage."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 31
description = "session system prompts"


@dataclass(frozen=True)
class _SessionSystemPrompts:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._migrate_session_system_prompts(cursor)


def create_migration(context: MigrationContext) -> _SessionSystemPrompts:
    return _SessionSystemPrompts(
        owner=require_owner(context, "0031_session_system_prompts")
    )
