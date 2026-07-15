"""Session deletion lifecycle service.

The service coordinates destructive cleanup across aggregate roots:
session rows/sidebar index/branch lineage through ``SessionRepo``,
transcript rows through ``MessageRepo``, and optional on-disk transcript files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from hermes_agent.repositories.base import RepositoryConnection
from hermes_agent.repositories.message_repo import MessageRepo, MessageRepoImpl
from hermes_agent.repositories.session_repo import SessionRepo, SessionRepoImpl
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionDeletionResult:
    session_deleted: bool
    index_deleted: bool


class SessionDeletionService:
    """SQLite-backed session deletion service."""

    def __init__(
        self,
        conn: RepositoryConnection,
        message_repo: MessageRepo | None = None,
        session_repo: SessionRepo | None = None,
        unit_of_work: SqliteUnitOfWork | None = None,
    ) -> None:
        self._conn = conn
        self._messages = message_repo if message_repo is not None else MessageRepoImpl(conn)
        self._sessions = session_repo if session_repo is not None else SessionRepoImpl(conn)
        self._unit_of_work = unit_of_work or SqliteUnitOfWork(
            conn,
            lock_for_connection(conn),
        )

    def delete(self, session_id: str, *, sessions_dir: Path | None = None) -> SessionDeletionResult:
        stable = str(session_id or "").strip()
        if not stable:
            return SessionDeletionResult(session_deleted=False, index_deleted=False)
        def operation(_conn):
            existed = self._sessions.exists(stable)
            if existed:
                self._sessions.orphan_child_references(stable)
                self._sessions.delete_branch_references(stable)
                self._messages.delete_by_session(stable)
                self._sessions.delete_row(stable)
            index_deleted = self._sessions.delete_index(stable)
            return SessionDeletionResult(
                session_deleted=existed,
                index_deleted=index_deleted,
            )

        result = self._unit_of_work.execute(operation)
        if result.session_deleted:
            _remove_session_files(sessions_dir, stable)
        return result


def _remove_session_files(sessions_dir: Path | None, session_id: str) -> None:
    if sessions_dir is None:
        return
    for suffix in (".json", ".jsonl"):
        path = sessions_dir / f"{session_id}{suffix}"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("failed to remove session file %s: %s", path, exc)
    try:
        for path in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("failed to remove request dump %s: %s", path, exc)
    except OSError as exc:
        logger.debug("failed to enumerate request dumps for %s: %s", session_id, exc)


__all__ = ["SessionDeletionResult", "SessionDeletionService"]
