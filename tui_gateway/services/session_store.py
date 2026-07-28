from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services.storage_maintenance import run_startup_storage_maintenance


@dataclass(frozen=True)
class SessionStoreResult:
    db: Any | None
    default_db: Any | None
    default_error: str | None


def resolve_home_path(value: Any, *, fallback: Any) -> Path:
    try:
        raw = value() if callable(value) else value
        return Path(raw).expanduser().resolve()
    except Exception:
        return Path(fallback).expanduser().resolve()


def get_session_db_for_home(
    *,
    active_home: Path,
    default_home: Path,
    default_db: Any | None,
    default_error: str | None,
    db_by_home: dict[str, Any],
    db_error_by_home: dict[str, str],
    logger: logging.Logger,
    session_db_factory: Callable[..., Any] | None = None,
    create_if_missing: bool = True,
) -> SessionStoreResult:
    if session_db_factory is None:
        session_db_factory = open_cli_session_store

    if active_home == default_home:
        if default_db is not None:
            return SessionStoreResult(default_db, default_db, default_error)
        if not create_if_missing and not (default_home / "state.db").exists():
            return SessionStoreResult(None, default_db, default_error)
        try:
            # Always pin the connection to the home selected by the caller.
            # Request handling may have an active profile ContextVar, and the
            # default factory otherwise consults get_hermes_home() again. That
            # second resolution used to redirect the canonical control-plane
            # connection into profiles/<id>/state.db even though both homes
            # passed above were the process root.
            db = session_db_factory(db_path=default_home / "state.db")
            _run_startup_run_event_maintenance(db, logger, profile_home=default_home)
            return SessionStoreResult(db, db, None)
        except Exception as exc:
            error = str(exc)
            logger.warning(
                "TUI session store unavailable — continuing without state.db features: %s",
                exc,
            )
            return SessionStoreResult(None, None, error)

    home_key = str(active_home)
    db = db_by_home.get(home_key)
    if db is not None:
        return SessionStoreResult(db, default_db, default_error)
    if not create_if_missing and not (active_home / "state.db").exists():
        return SessionStoreResult(None, default_db, default_error)

    try:
        db = session_db_factory(db_path=active_home / "state.db")
        _run_startup_run_event_maintenance(db, logger, profile_home=active_home)
        db_by_home[home_key] = db
        db_error_by_home.pop(home_key, None)
        return SessionStoreResult(db, default_db, default_error)
    except Exception as exc:
        db_error_by_home[home_key] = str(exc)
        logger.warning(
            "Profile session store unavailable for %s — continuing without state.db features: %s",
            active_home,
            exc,
        )
        return SessionStoreResult(None, default_db, default_error)


def db_unavailable_detail(
    *,
    active_home: Path | None,
    default_error: str | None,
    db_error_by_home: dict[str, str],
) -> str:
    active_key = str(active_home) if active_home is not None else ""
    return (
        db_error_by_home.get(active_key)
        or default_error
        or "state.db unavailable"
    )


def _run_startup_run_event_maintenance(
    db: Any,
    logger: logging.Logger,
    *,
    profile_home: str | Path = "",
) -> None:
    try:
        result = run_startup_storage_maintenance(
            db,
            profile_home=profile_home,
            logger=logger,
        )
        deleted_events = int(result.get("deleted_events") or 0) if isinstance(result, dict) else 0
        if isinstance(result, dict) and not result.get("skipped") and deleted_events > 0:
            logger.info(
                "TUI session store run-event maintenance compacted %s event row(s)",
                deleted_events,
            )
    except Exception as exc:
        logger.debug("TUI session store run-event maintenance skipped: %s", exc)
