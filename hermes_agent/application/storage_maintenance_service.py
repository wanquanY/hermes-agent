"""Lifecycle and space maintenance for persisted sessions."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from hermes_agent.application.session_deletion import SessionDeletionService
from hermes_agent.application.state_metadata_service import StateMetadataService
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.storage.unit_of_work import LockLike, SqliteUnitOfWork

logger = logging.getLogger(__name__)


class StorageMaintenanceService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: LockLike,
        unit_of_work: SqliteUnitOfWork,
        session_repo: SessionRepoImpl,
        deletion: SessionDeletionService,
        metadata: StateMetadataService,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._unit_of_work = unit_of_work
        self._session_repo = session_repo
        self._deletion = deletion
        self._metadata = metadata

    def delete_session(self, session_id: str, sessions_dir: Path | None = None) -> bool:
        result = self._deletion.delete(session_id, sessions_dir=sessions_dir)
        return result.session_deleted

    def prune_sessions(
        self,
        older_than_days: int = 90,
        source: str | None = None,
        sessions_dir: Path | None = None,
    ) -> int:
        cutoff = time.time() - (int(older_than_days or 0) * 86400)
        params: list[Any] = [cutoff]
        source_clause = ""
        if source:
            source_clause = " AND source = ?"
            params.append(str(source))
        rows = self._conn.execute(
            f"SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL{source_clause}",
            tuple(params),
        ).fetchall()
        return sum(
            self.delete_session(str(row["id"]), sessions_dir=sessions_dir)
            for row in rows
            if str(row["id"] or "")
        )

    def prune_empty_ghost_sessions(self, sessions_dir: Path | None = None) -> int:
        rows = self._conn.execute(
            """
            SELECT id FROM sessions
             WHERE COALESCE(message_count, 0) = 0
               AND (ended_at IS NOT NULL OR COALESCE(transient, 0) = 1)
            """
        ).fetchall()
        return sum(
            self.delete_session(str(row["id"]), sessions_dir=sessions_dir)
            for row in rows
        )

    def finalize_orphaned_compression_sessions(self) -> int:
        return self._unit_of_work.execute(
            lambda _conn: self._session_repo.finalize_orphaned_compression_sessions()
        )

    def repair_orphaned_foreign_key_rows(self) -> int:
        """Remove dangling non-authoritative index and cache records."""

        def affected(cursor: sqlite3.Cursor) -> int:
            return max(0, int(cursor.rowcount or 0))

        def repair(conn: sqlite3.Connection) -> int:
            repaired = self._session_repo.repair_orphaned_branch_references()
            repaired += affected(
                conn.execute(
                    """
                    DELETE FROM team_capability_snapshot_bindings
                    WHERE NOT EXISTS (
                        SELECT 1 FROM team_missions m
                        WHERE m.mission_id = team_capability_snapshot_bindings.mission_id
                    )
                       OR NOT EXISTS (
                        SELECT 1 FROM team_capability_snapshots s
                        WHERE s.snapshot_id = team_capability_snapshot_bindings.snapshot_id
                    )
                    """
                )
            )
            return repaired

        return self._unit_of_work.execute(repair)

    def vacuum(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:
                logger.debug("pre-vacuum WAL checkpoint failed: %s", exc)
            self._conn.execute("VACUUM")

    def maybe_auto_prune_and_vacuum(
        self,
        *,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir: Path | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        try:
            last = float(self._metadata.get("last_auto_prune") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last and now - last < max(0, int(min_interval_hours or 0)) * 3600:
            return {
                "skipped": True,
                "pruned": 0,
                "vacuumed": False,
                "reason": "interval",
            }
        pruned = self.prune_sessions(
            older_than_days=max(1, int(retention_days or 90)),
            sessions_dir=sessions_dir,
        )
        vacuumed = False
        if vacuum and pruned:
            self.vacuum()
            vacuumed = True
        self._metadata.set("last_auto_prune", str(now))
        return {
            "skipped": False,
            "pruned": pruned,
            "vacuumed": vacuumed,
        }


__all__ = ["StorageMaintenanceService"]
