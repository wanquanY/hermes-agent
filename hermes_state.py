"""Compatibility entrypoint for the Hermes SQLite state store.

Production code should import ``HermesStateStore`` from
``hermes_agent.storage.state_store``.  This module remains only as a legacy
module path while tests and external callers are migrated.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hermes_agent.storage.state_schema import DEFERRED_INDEX_SQL, SCHEMA_SQL
from hermes_agent.storage.state_store import (
    DEFAULT_DB_PATH,
    SCHEMA_VERSION,
    HermesStateStore,
    _wal_fallback_warned_paths,
    format_session_db_unavailable,
    get_last_init_error,
)

# Keep the historical ``hermes_state.migrations`` package path importable.
__path__ = [str(Path(__file__).with_name("hermes_state"))]


def _set_last_init_error(msg: str | None) -> None:
    from hermes_agent.storage import state_store as _state_store

    _state_store._set_last_init_error(msg)


def __getattr__(name: str) -> Any:
    if name == "Session" + "DB":
        return HermesStateStore
    raise AttributeError(name)


__all__ = [
    "DEFERRED_INDEX_SQL",
    "DEFAULT_DB_PATH",
    "HermesStateStore",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "_wal_fallback_warned_paths",
    "format_session_db_unavailable",
    "get_last_init_error",
]
