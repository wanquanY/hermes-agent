"""Fold legacy profile versions into latest profile rows."""

from __future__ import annotations

import sqlite3

from hermes_agent.composition.migration_operations import fold_agent_profile_versions

version = 20
description = "agent profile versions to latest profiles"


def apply(cursor: sqlite3.Cursor) -> None:
    fold_agent_profile_versions(cursor)
