"""Run aggregate orchestration for repository-backed state."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict
from typing import Any, Callable

from hermes_agent.domain.run_event_retention_service import RunEventRetentionService
from hermes_agent.domain.run_lifecycle import (
    DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS,
    DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS,
    orphaned_active_run_decision,
)
from hermes_agent.domain.run_state_machine import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES
from hermes_agent.domain.session_runtime_state import session_runtime_state_from_row
from hermes_agent.domain.run_terminator import TerminateCause, terminate_run
from hermes_agent.read_models.run_events import RunEventReadModel
from hermes_agent.repositories.run_repo import RunRepoImpl
from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionRunProjection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


logger = logging.getLogger(__name__)


class RunService:
    """Coordinates Run repository writes and Session index projection."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        unit_of_work: SqliteUnitOfWork,
        sessions: SessionRepoImpl,
    ) -> None:
        self._conn = conn
        self._unit_of_work = unit_of_work
        self._sessions = sessions
        self._repository = RunRepoImpl(conn)
        self._events = RunEventReadModel(conn)
        self.retention = RunEventRetentionService(conn, unit_of_work)
        self._event_listener_lock = threading.RLock()
        self._event_listeners: dict[str, Callable[[dict[str, Any]], None]] = {}

    def append_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        participant_id: str = "",
        activity_id: str = "",
    ) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")

        def operation(_conn: sqlite3.Connection) -> dict[str, Any]:
            self._sessions.ensure_runtime_session(
                stable,
                started_at=float((event or {}).get("timestamp") or time.time()),
            )
            saved = self._repository.append_runtime_event(
                stable,
                event,
                participant_id=participant_id,
                activity_id=activity_id,
            )
            if str(saved.get("type") or "") == "session.info":
                self._sessions.project_runtime_state_event(saved)
            self._project_saved(saved)
            return saved

        saved = self._unit_of_work.execute(operation)
        normalized_run_id = str((saved or {}).get("run_id") or "").strip()
        persisted_run = self._repository.get_run(normalized_run_id) if normalized_run_id else None
        self.retention.maintain_after_append(
            session_id=stable,
            run_id=normalized_run_id,
            seq=int((saved or {}).get("seq") or 0),
            terminal_status=persisted_run.status if persisted_run is not None else None,
        )
        self._notify_event_appended(saved)
        return saved

    def register_event_listener(
        self,
        listener_id: str,
        listener: Callable[[dict[str, Any]], None],
    ) -> None:
        stable = str(listener_id or "").strip()
        if not stable:
            raise ValueError("listener_id is required")
        if not callable(listener):
            raise TypeError("listener must be callable")
        with self._event_listener_lock:
            self._event_listeners[stable] = listener

    def unregister_event_listener(self, listener_id: str) -> bool:
        stable = str(listener_id or "").strip()
        if not stable:
            return False
        with self._event_listener_lock:
            return self._event_listeners.pop(stable, None) is not None

    def _notify_event_appended(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict) or not event:
            return
        with self._event_listener_lock:
            listeners = tuple(self._event_listeners.items())
        for listener_id, listener in listeners:
            try:
                listener(copy.deepcopy(event))
            except Exception:
                logger.exception(
                    "run event listener failed listener_id=%s session_id=%s seq=%s",
                    listener_id,
                    event.get("session_id"),
                    event.get("seq"),
                )

    def upsert(
        self,
        *,
        run_id: str,
        session_id: str,
        runtime_scope_key: str = "",
        turn_id: str = "",
        execution_session_id: str = "",
        status: str = "running",
        started_at: float | None = None,
        updated_at: float | None = None,
        completed_at: float | None = None,
        last_seq: int = 0,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        if not stable or not normalized_run_id:
            return {}

        def operation(_conn: sqlite3.Connection) -> dict[str, Any]:
            self._sessions.ensure_runtime_session(stable, started_at=started_at)
            run = self._repository.upsert_materialized_state(
                run_id=normalized_run_id,
                session_id=stable,
                runtime_scope_key=runtime_scope_key,
                turn_id=turn_id,
                execution_session_id=execution_session_id,
                status=status,
                started_at=started_at,
                updated_at=updated_at,
                completed_at=completed_at,
                last_seq=last_seq,
                error=error,
                metadata=metadata,
            )
            self._project(run)
            return asdict(run)

        return self._unit_of_work.execute(operation)

    def reserve_if_idle(
        self,
        *,
        run_id: str,
        session_id: str,
        runtime_scope_key: str = "",
        turn_id: str = "",
        execution_session_id: str = "",
        status: str = "queued",
        started_at: float | None = None,
        updated_at: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        if not stable or not normalized_run_id:
            return {"run": None, "conflict": None, "created": False}

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = self._repository.get_run(normalized_run_id)
            if existing is not None:
                return {"run": asdict(existing), "conflict": None, "created": False}
            placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
            conflict = conn.execute(
                f"""
                SELECT run_id FROM runs
                WHERE session_id = ? AND status IN ({placeholders})
                ORDER BY updated_at DESC, started_at DESC LIMIT 1
                """,
                (stable, *sorted(ACTIVE_RUN_STATUSES)),
            ).fetchone()
            if conflict is not None:
                active = self._repository.get_run(str(conflict["run_id"] or ""))
                return {
                    "run": None,
                    "conflict": asdict(active) if active is not None else None,
                    "created": False,
                }
            run = self._repository.upsert_materialized_state(
                run_id=normalized_run_id,
                session_id=stable,
                runtime_scope_key=runtime_scope_key or stable,
                turn_id=turn_id,
                execution_session_id=execution_session_id,
                status=status,
                started_at=started_at,
                updated_at=updated_at,
                metadata=metadata,
            )
            self._project(run)
            return {"run": asdict(run), "conflict": None, "created": True}

        return self._unit_of_work.execute(operation)

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self._repository.get_run(run_id)
        return asdict(run) if run is not None else None

    def runtime_state(self, session_id: str) -> dict[str, Any]:
        """Return the latest projected runtime state for a conversation session."""
        stable = str(session_id or "").strip()
        if not stable:
            return {}
        row = self._conn.execute(
            "SELECT * FROM session_runtime_state WHERE session_id = ?",
            (stable,),
        ).fetchone()
        return session_runtime_state_from_row(row)

    def list(
        self,
        session_id: str = "",
        *,
        runtime_scope_key: str = "",
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if str(session_id or "").strip():
            clauses.append("session_id = ?")
            params.append(str(session_id).strip())
        if str(runtime_scope_key or "").strip():
            clauses.append("runtime_scope_key = ?")
            params.append(str(runtime_scope_key).strip())
        normalized_statuses = [
            str(value).strip() for value in (statuses or []) if str(value).strip()
        ]
        if normalized_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in normalized_statuses)})")
            params.extend(normalized_statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT run_id FROM runs {where} ORDER BY updated_at DESC, started_at DESC LIMIT ?",
            (*params, max(1, min(int(limit or 200), 2000))),
        ).fetchall()
        runs = [self._repository.get_run(str(row["run_id"] or "")) for row in rows]
        return [asdict(run) for run in runs if run is not None]

    def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int = 0,
        active_only: bool = False,
        runtime_scope_key: str = "",
        run_id: str = "",
        activity_id: str = "",
        event_types: tuple[str, ...] = (),
        exclude_event_types: tuple[str, ...] = (),
        exclude_event_type_prefixes: tuple[str, ...] = (),
        limit: int = 2000,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        return self._events.list_runtime(
            session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            active_only=active_only,
            active_statuses=tuple(ACTIVE_RUN_STATUSES),
            runtime_scope_key=runtime_scope_key,
            run_id=run_id,
            activity_id=activity_id,
            event_types=event_types,
            exclude_event_types=exclude_event_types,
            exclude_event_type_prefixes=exclude_event_type_prefixes,
            limit=limit,
            include_internal=include_internal,
        )

    def list_events_by_run_ids(
        self,
        run_ids: list[str],
        *,
        limit_per_run: int = 2000,
        include_internal: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        return self._events.list_by_run_ids(
            run_ids,
            limit_per_run=limit_per_run,
            include_internal=include_internal,
        )

    def latest_event_for_run(
        self,
        run_id: str,
        *,
        include_internal: bool = True,
    ) -> dict[str, Any] | None:
        return self._events.latest_for_run(
            run_id,
            include_internal=include_internal,
        )

    def list_filtered_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        runtime_scope_key: str = "",
        event_type_prefix: str = "",
        event_types: tuple[str, ...] | list[str] | None = None,
        payload_contains: str = "",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        return self._events.list_filtered(
            session_id,
            after_seq=after_seq,
            runtime_scope_key=runtime_scope_key,
            event_type_prefix=event_type_prefix,
            event_types=event_types,
            payload_contains=payload_contains,
            limit=limit,
        )

    def has_event_source(self, session_id: str, **query: Any) -> bool:
        return self._events.has_source(session_id, **query)

    def has_event_frame(self, session_id: str, **query: Any) -> bool:
        return self._events.has_frame(session_id, **query)

    def list_events_by_activity(
        self,
        activity_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        return self._events.list_activity_events(
            activity_id,
            after_seq=after_seq,
            limit=limit,
            include_internal=include_internal,
        )

    def list_tool_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        return self._events.list_tool_events(
            session_id,
            after_seq=after_seq,
            limit=limit,
        )

    def interaction_anchor_seq(self, session_id: str, request_id: str) -> int:
        return self._events.interaction_anchor_seq(
            session_id,
            request_id,
        )

    def list_events_by_mission_activity(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
        include_internal: bool = False,
        reverse: bool = False,
    ) -> list[dict[str, Any]]:
        return self._events.list_mission_activity_events(
            mission_id,
            after_seq=after_seq,
            limit=limit,
            include_internal=include_internal,
            reverse=reverse,
        )

    def terminate(
        self,
        *,
        run_id: str,
        session_id: str,
        target_status: str,
        cause: str | TerminateCause,
        turn_id: str = "",
        activity_id: str = "",
        message: str = "",
        runtime_scope_key: str = "",
        execution_session_id: str = "",
        payload_extra: dict[str, Any] | None = None,
    ) -> Any:
        stable = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        if not stable or not normalized_run_id:
            raise ValueError("run_id and session_id are required")
        resolved_cause = cause if isinstance(cause, TerminateCause) else TerminateCause(str(cause))

        def operation(conn: sqlite3.Connection) -> Any:
            self._sessions.ensure_runtime_session(stable)
            if self._repository.get_run(normalized_run_id) is None:
                self._repository.upsert_materialized_state(
                    run_id=normalized_run_id,
                    session_id=stable,
                    runtime_scope_key=runtime_scope_key or stable,
                    turn_id=turn_id,
                    execution_session_id=execution_session_id,
                    status="running",
                )
            result = terminate_run(
                conn,
                run_id=normalized_run_id,
                session_id=stable,
                target_status=target_status,
                cause=resolved_cause,
                turn_id=turn_id,
                activity_id=activity_id,
                message=message,
                payload_extra=payload_extra,
            )
            run = self._repository.get_run(normalized_run_id)
            if run is not None:
                self._project(run)
            return result

        result = self._unit_of_work.execute(operation)
        terminal_status = str(target_status or "") if str(target_status or "") in TERMINAL_RUN_STATUSES else None
        self.retention.maintain_after_append(
            session_id=stable,
            run_id=normalized_run_id,
            seq=int(getattr(result, "terminal_seq", 0) or 0),
            terminal_status=terminal_status,
        )
        return result

    def next_event_seq(self, session_id: str, fallback_seq: int = 0) -> int:
        row = self._conn.execute(
            "SELECT next_seq FROM seq_counter WHERE session_id = ?",
            (str(session_id or ""),),
        ).fetchone()
        return max(int(row["next_seq"] or 0) if row else 0, int(fallback_seq or 0))

    def session_status(self, session_id: str) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        empty = {
            "running": False,
            "active_run_id": "",
            "active_turn_id": "",
            "active_execution_session_id": "",
            "runtime_scope_key": "",
            "run_started_at": 0,
            "run_updated_at": 0,
            "last_event_seq": 0,
        }
        if not stable:
            return empty
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        row = self._conn.execute(
            f"""
            SELECT run_id FROM runs
            WHERE session_id = ? AND status IN ({placeholders})
            ORDER BY updated_at DESC, started_at DESC LIMIT 1
            """,
            (stable, *sorted(ACTIVE_RUN_STATUSES)),
        ).fetchone()
        run = self._repository.get_run(str(row["run_id"] or "")) if row is not None else None
        last = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM run_events WHERE session_id = ?",
            (stable,),
        ).fetchone()
        if run is None:
            return {**empty, "last_event_seq": int(last["last_seq"] or 0) if last else 0}
        return {
            "running": True,
            "active_run_id": run.run_id,
            "active_turn_id": run.turn_id,
            "active_execution_session_id": run.execution_session_id,
            "runtime_scope_key": run.runtime_scope_key,
            "run_started_at": run.started_at,
            "run_updated_at": run.updated_at,
            "last_event_seq": int(last["last_seq"] or 0) if last else 0,
        }

    def fail_orphaned(
        self,
        *,
        live_runtime_ids: set[str] | None = None,
        live_execution_session_ids: set[str] | None = None,
        current_pid: int | None = None,
        current_gateway_instance_id: str = "",
        stale_after_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS,
        owner_dead_grace_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS,
        reason: str = "runtime owner is no longer available",
    ) -> int:
        live_ids = live_runtime_ids if live_runtime_ids is not None else live_execution_session_ids
        normalized_live_ids = {
            str(value or "").strip() for value in (live_ids or set()) if str(value or "").strip()
        }
        now = time.time()
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)

        def operation(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE status IN ({placeholders})",
                tuple(sorted(ACTIVE_RUN_STATUSES)),
            ).fetchall()
            failed = 0
            for row in rows:
                should_fail, decision = orphaned_active_run_decision(
                    row,
                    now=now,
                    live_runtime_ids=normalized_live_ids,
                    current_pid=current_pid,
                    current_gateway_instance_id=current_gateway_instance_id,
                    stale_after_seconds=stale_after_seconds,
                    owner_dead_grace_seconds=owner_dead_grace_seconds,
                )
                if not should_fail:
                    continue
                metadata = _json_object(row["metadata_json"])
                metadata.update({"recovery_reason": reason, "recovery_decision": decision})
                run_id = str(row["run_id"] or "")
                self._repository.set_terminal(
                    run_id,
                    str(row["session_id"] or ""),
                    "failed",
                    cause=TerminateCause.WORKER_CRASHED,
                )
                self._repository.update_metadata(run_id, metadata)
                recovered = self._repository.get_run(run_id)
                if recovered is not None:
                    self._project(recovered)
                failed += 1
            return failed

        return self._unit_of_work.execute(operation)

    def _project_saved(self, saved: dict[str, Any]) -> None:
        run_id = str((saved or {}).get("run_id") or "").strip()
        if not run_id:
            return
        run = self._repository.get_run(run_id)
        if run is not None:
            self._project(run)

    def _project(self, run: Any) -> None:
        self._sessions.project_run_state(
            SessionRunProjection(
                session_id=run.session_id,
                run_id=run.run_id,
                execution_session_id=run.execution_session_id,
                runtime_scope_key=run.runtime_scope_key,
                status=run.status,
                updated_at=run.updated_at,
            )
        )


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["RunService"]
