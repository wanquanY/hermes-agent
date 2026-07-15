"""Compact legacy duplicate Team Mission event JSON storage."""

from __future__ import annotations

import logging
import sqlite3

from hermes_team_mission.state.schema import compact_team_mission_event_json_storage

version = 39
description = "team mission event json storage"

_logger = logging.getLogger(__name__)


def apply(cursor: sqlite3.Cursor) -> None:
    compact_team_mission_event_json_storage(cursor, _logger)
