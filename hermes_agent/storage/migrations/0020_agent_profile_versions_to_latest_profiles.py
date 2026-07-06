"""Fold legacy profile versions into latest profile rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hermes_agent.storage.migrations.base import MigrationContext
from hermes_agent.storage.migrations.base import require_owner

version = 20
description = "agent profile versions to latest profiles"


@dataclass(frozen=True)
class _AgentProfileVersionsToLatestProfiles:
    owner: Any
    version: int = version
    description: str = description

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self.owner._migrate_agent_profile_versions_to_latest_profiles(cursor)


def create_migration(context: MigrationContext) -> _AgentProfileVersionsToLatestProfiles:
    return _AgentProfileVersionsToLatestProfiles(
        owner=require_owner(context, "0020_agent_profile_versions_to_latest_profiles")
    )
