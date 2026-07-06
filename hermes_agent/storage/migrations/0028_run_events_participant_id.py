"""Add durable speaker identity to run events."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 28
description = "run events participant id"


@dataclass(frozen=True)
class _RunEventsParticipantId:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._migrate_run_events_participant_id(cursor)


def create_migration(context: MigrationContext) -> _RunEventsParticipantId:
    return _RunEventsParticipantId(
        owner=require_owner(context, "0028_run_events_participant_id")
    )
