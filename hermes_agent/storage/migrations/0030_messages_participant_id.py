"""Add durable transcript speaker identity."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 30
description = "messages participant id"


@dataclass(frozen=True)
class _MessagesParticipantId:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._migrate_messages_participant_id(cursor)


def create_migration(context: MigrationContext) -> _MessagesParticipantId:
    return _MessagesParticipantId(
        owner=require_owner(context, "0030_messages_participant_id")
    )
