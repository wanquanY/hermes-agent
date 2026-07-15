"""Composition factory for the default session recall read model."""

from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.session_repository_db import (
    connect_session_repository_db,
)
from hermes_agent.read_models.session_recall import SessionRecallReadModel


def open_default_session_recall(
    db_path: Path | str | None = None,
) -> SessionRecallReadModel:
    return SessionRecallReadModel(connect_session_repository_db(db_path))


__all__ = ["open_default_session_recall"]
