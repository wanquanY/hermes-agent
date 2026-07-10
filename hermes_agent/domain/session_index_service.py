"""Application service for the user-visible session index projection."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any

from hermes_agent.domain.session_index_reconciler import SessionIndexReconciler
from hermes_agent.read_models.session_index import SessionIndexQuery, SessionIndexReadModel
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class SessionIndexService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        repository: SessionRepoImpl,
        unit_of_work: SqliteUnitOfWork,
        repair_team_runtime_scope: Callable[[sqlite3.Connection], int] | None = None,
    ) -> None:
        self._conn = conn
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._read_model = SessionIndexReadModel(conn)
        self._repair_team_runtime_scope = repair_team_runtime_scope

    def upsert(self, *, session_id: str, **fields: Any) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id required for session_index upsert")
        now = time.time()
        source = str(fields.get("source") or "unknown")
        session_kind = str(fields.get("session_kind") or "hermes_session")
        conversation_kind = str(fields.get("conversation_kind") or "").strip().lower()
        if conversation_kind not in {"direct", "team"}:
            conversation_kind = (
                "team"
                if source == "team_mission" or session_kind == "team_mission"
                else "direct"
            )
        values = {
            "session_id": stable,
            "owner_agent_profile_id": str(fields.get("owner_agent_profile_id") or ""),
            "owner_profile_version_id": str(fields.get("owner_profile_version_id") or ""),
            "runtime_scope_key": str(fields.get("runtime_scope_key") or ""),
            "title": str(fields.get("title") or ""),
            "preview": str(fields.get("preview") or ""),
            "source": source,
            "transient": 1 if fields.get("transient") else 0,
            "session_kind": session_kind,
            "conversation_kind": conversation_kind,
            "status": str(fields.get("status") or "idle"),
            "running": 1 if fields.get("running") else 0,
            "waiting_approval": 1 if fields.get("waiting_approval") else 0,
            "active_run_id": str(fields.get("active_run_id") or ""),
            "active_execution_session_id": str(
                fields.get("active_execution_session_id") or ""
            ),
            "pending_approval_count": int(fields.get("pending_approval_count") or 0),
            "team_id": str(fields.get("team_id") or ""),
            "mission_id": str(fields.get("mission_id") or ""),
            "conversation_id": str(fields.get("conversation_id") or ""),
            "message_count": int(fields.get("message_count") or 0),
            "started_at": float(
                now if fields.get("started_at") is None else fields["started_at"]
            ),
            "updated_at": float(
                now if fields.get("updated_at") is None else fields["updated_at"]
            ),
            "last_activity": fields.get("last_activity"),
        }
        return self._unit_of_work.execute(
            lambda _conn: self._repository.upsert_session_index(values)
        )

    def delete(self, session_id: str) -> int:
        return int(
            self._unit_of_work.execute(
                lambda _conn: self._repository.delete_index(session_id)
            )
        )

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._read_model.get(session_id)

    def repair_terminal_active_runs(self) -> int:
        """Clear stale active-run state from the durable session projection."""
        return int(
            self._unit_of_work.execute(
                lambda conn: SessionIndexReconciler(conn).repair_terminal_active_runs()
            )
        )

    def list(
        self,
        *,
        limit: int = 200,
        cursor: dict[str, Any] | None = None,
        include_transient: bool = False,
        conversation_kind: str | None = None,
    ) -> dict[str, Any]:
        def repair(conn: sqlite3.Connection) -> None:
            SessionIndexReconciler(conn).repair_terminal_active_runs()
            if self._repair_team_runtime_scope is not None:
                self._repair_team_runtime_scope(conn)

        self._unit_of_work.execute(repair)
        return self._read_model.list(
            SessionIndexQuery(
                limit=limit,
                cursor=cursor,
                include_transient=include_transient,
                conversation_kind=conversation_kind,
            )
        )

    def reconcile(self, *, exclude_sources: list[str] | None = None) -> dict[str, Any]:
        return self._unit_of_work.execute(
            lambda conn: SessionIndexReconciler(conn).reconcile(
                exclude_sources=exclude_sources
            )
        )


__all__ = ["SessionIndexService"]
