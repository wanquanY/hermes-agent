#!/usr/bin/env python3
"""Compatibility composition point for the Hermes SQLite state store."""

from pathlib import Path

from hermes_agent.application.state_facade.message_facade import MessageStateFacadeMixin
from hermes_agent.application.state_facade.run_facade import RunStateMixin
from hermes_agent.application.state_facade.session_facade import SessionStateFacadeMixin
from hermes_agent.application.state_facade.storage_engine_facade import (
    DEFAULT_DB_PATH,
    SCHEMA_VERSION,
    StorageEngineMixin,
    _set_last_init_error,
    _wal_fallback_warned_paths,
    format_session_db_unavailable,
    get_last_init_error,
)
from hermes_agent.domain.session_handoff_state import SessionHandoffStateMixin
from hermes_agent.storage.state_maintenance import StateMaintenanceMixin
from hermes_team_mission.state.session_mixin import TeamMissionStateMixin

__path__ = [str(Path(__file__).with_name("hermes_state"))]


class HermesStateStore(
    StorageEngineMixin,
    TeamMissionStateMixin,
    SessionStateFacadeMixin,
    MessageStateFacadeMixin,
    RunStateMixin,
    StateMaintenanceMixin,
    SessionHandoffStateMixin,
):
    pass


__all__ = ["DEFAULT_DB_PATH", "HermesStateStore", "SCHEMA_VERSION", "_set_last_init_error", "_wal_fallback_warned_paths", "format_session_db_unavailable", "get_last_init_error"]
