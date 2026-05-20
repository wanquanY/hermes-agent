from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ACTIVE_RUN_STATUSES = {
    "queued",
    "starting",
    "running",
    "waiting_approval",
    "cancelling",
    "finalizing",
}
TERMINAL_RUN_STATUSES = {"completed", "failed", "interrupted", "cancelled"}
DEFAULT_RUN_EVENT_RETENTION_DAYS = 14
DEFAULT_RUN_EVENT_MAX_PER_SESSION = 5000
RUN_EVENT_PRUNE_INTERVAL_EVENTS = 500


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _sql_status_literals(statuses: set[str]) -> str:
    return ",".join("'" + status.replace("'", "''") + "'" for status in sorted(statuses))


def _event_run_id(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(event.get("run_id") or payload.get("run_id") or "").strip()


def _event_turn_id(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(event.get("turn_id") or payload.get("turn_id") or "").strip()


def _event_runtime_scope_key(event: Dict[str, Any], fallback: str = "") -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        event.get("runtime_scope_key")
        or payload.get("runtime_scope_key")
        or fallback
        or ""
    ).strip()


def _event_status(event_type: str, payload: Dict[str, Any]) -> str | None:
    if event_type == "error":
        return "failed"
    if event_type != "message.complete":
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status == "interrupted":
        return "interrupted"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"error", "failed"}:
        return "failed"
    return "completed"


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _pid_is_alive(pid: int, current_pid: int | None = None) -> bool:
    if pid <= 0:
        return False
    if current_pid is not None and pid == current_pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


class SessionDBRunMixin:
    """Persistent run state and append-only event log for gateway sessions."""

    def _run_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        run = dict(row)
        metadata = _json_loads(run.pop("metadata_json", None), {})
        run["metadata"] = metadata if isinstance(metadata, dict) else {}
        if not run.get("runtime_scope_key"):
            run["runtime_scope_key"] = run.get("session_id") or ""
        return run

    def next_run_event_seq(self, session_id: str, fallback_seq: int = 0) -> int:
        stable = str(session_id or "").strip()
        if not stable:
            return int(fallback_seq or 0)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM run_events WHERE session_id = ?",
                (stable,),
            ).fetchone()
        persisted_next = int(_row_value(row, "last_seq", 0) or 0) + 1
        return max(persisted_next, int(fallback_seq or 0))

    def upsert_run(
        self,
        *,
        run_id: str,
        session_id: str,
        runtime_scope_key: str = "",
        turn_id: str = "",
        runtime_session_id: str = "",
        status: str = "running",
        started_at: float | None = None,
        updated_at: float | None = None,
        completed_at: float | None = None,
        last_seq: int = 0,
        error: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        session_id = str(session_id or "").strip()
        if not run_id or not session_id:
            return {}
        now = time.time()
        started = float(started_at or now)
        updated = float(updated_at or now)
        normalized_status = str(status or "running").strip() or "running"
        normalized_scope = str(runtime_scope_key or session_id).strip()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, session_id, runtime_scope_key, turn_id, runtime_session_id, status,
                        started_at, updated_at, completed_at, last_seq, error,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        session_id,
                        normalized_scope,
                        turn_id,
                        runtime_session_id,
                        normalized_status,
                        started,
                        updated,
                        completed_at,
                        int(last_seq or 0),
                        error,
                        _json_dumps(metadata or {}),
                    ),
                )
            else:
                existing_status = str(existing["status"] or "")
                next_status = normalized_status
                if existing_status in TERMINAL_RUN_STATUSES and normalized_status not in TERMINAL_RUN_STATUSES:
                    next_status = existing_status
                next_completed_at = completed_at
                if next_completed_at is None:
                    next_completed_at = existing["completed_at"]
                if next_status in TERMINAL_RUN_STATUSES and next_completed_at is None:
                    next_completed_at = updated
                merged_metadata = _json_loads(existing["metadata_json"], {})
                if isinstance(metadata, dict):
                    merged_metadata.update(metadata)
                conn.execute(
                    """
                    UPDATE runs
                    SET session_id = ?,
                        runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                        turn_id = COALESCE(NULLIF(?, ''), turn_id),
                        runtime_session_id = COALESCE(NULLIF(?, ''), runtime_session_id),
                        status = ?,
                        updated_at = ?,
                        completed_at = ?,
                        last_seq = MAX(COALESCE(last_seq, 0), ?),
                        error = COALESCE(NULLIF(?, ''), error),
                        metadata_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        session_id,
                        normalized_scope,
                        turn_id,
                        runtime_session_id,
                        next_status,
                        updated,
                        next_completed_at,
                        int(last_seq or 0),
                        error,
                        _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                        run_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return self._run_from_row(row) or {}

        return self._execute_write(_do)

    def create_run_if_session_idle(
        self,
        *,
        run_id: str,
        session_id: str,
        runtime_scope_key: str = "",
        turn_id: str = "",
        runtime_session_id: str = "",
        status: str = "queued",
        started_at: float | None = None,
        updated_at: float | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Atomically create an active run unless the session is already busy.

        Returns ``{"run": <state>, "conflict": None}`` on success/idempotent
        retry.  Returns ``{"run": None, "conflict": <active-run>}`` when a
        different non-terminal run already owns the session.
        """
        normalized_run_id = str(run_id or "").strip()
        stable = str(session_id or "").strip()
        if not normalized_run_id or not stable:
            return {"run": None, "conflict": None}
        normalized_scope = str(runtime_scope_key or stable).strip()
        normalized_status = str(status or "queued").strip() or "queued"
        now = time.time()
        started = float(started_at or now)
        updated = float(updated_at or now)
        active_placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if existing is not None:
                return {"run": self._run_from_row(existing), "conflict": None, "created": False}

            active = conn.execute(
                f"""
                SELECT *
                FROM runs
                WHERE session_id = ?
                  AND status IN ({active_placeholders})
                ORDER BY updated_at DESC, started_at DESC
                LIMIT 1
                """,
                (stable, *sorted(ACTIVE_RUN_STATUSES)),
            ).fetchone()
            if active is not None:
                return {"run": None, "conflict": self._run_from_row(active), "created": False}

            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, session_id, runtime_scope_key, turn_id, runtime_session_id, status,
                        started_at, updated_at, completed_at, last_seq, error,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, '', ?)
                    """,
                    (
                        normalized_run_id,
                        stable,
                        normalized_scope,
                        turn_id,
                        runtime_session_id,
                        normalized_status,
                        started,
                        updated,
                        _json_dumps(metadata or {}),
                    ),
                )
            except sqlite3.IntegrityError:
                active = conn.execute(
                    f"""
                    SELECT *
                    FROM runs
                    WHERE session_id = ?
                      AND status IN ({active_placeholders})
                    ORDER BY updated_at DESC, started_at DESC
                    LIMIT 1
                    """,
                    (stable, *sorted(ACTIVE_RUN_STATUSES)),
                ).fetchone()
                return {"run": None, "conflict": self._run_from_row(active), "created": False}
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            return {"run": self._run_from_row(row), "conflict": None, "created": True}

        return self._execute_write(_do)

    def append_run_event(self, session_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        stable = str(session_id or "").strip()
        if not stable:
            return {}
        frame = dict(event or {})
        frame["stored_session_id"] = str(frame.get("stored_session_id") or stable)
        event_type = str(frame.get("type") or "").strip()
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        run_id = _event_run_id(frame)
        turn_id = _event_turn_id(frame)
        runtime_session_id = str(frame.get("session_id") or "").strip()
        runtime_scope_key = _event_runtime_scope_key(frame, stable)
        frame["runtime_scope_key"] = runtime_scope_key
        timestamp = float(frame.get("timestamp") or time.time())
        seq = int(frame.get("seq") or 0)
        if seq <= 0:
            seq = self.next_run_event_seq(stable)
            frame["seq"] = seq
        terminal_status = _event_status(event_type, payload)
        frame["timestamp"] = timestamp
        event_json = _json_dumps(frame)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT OR IGNORE INTO run_events (
                    session_id, run_id, turn_id, runtime_session_id, runtime_scope_key, event_type,
                    seq, timestamp, payload_json, event_json, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable,
                    run_id,
                    turn_id,
                    runtime_session_id,
                    runtime_scope_key,
                    event_type,
                    seq,
                    timestamp,
                    _json_dumps(payload),
                    event_json,
                    terminal_status or "",
                ),
            )
            if run_id:
                existing = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                existing_status = str(_row_value(existing, "status", "") or "")
                next_status = terminal_status or existing_status or "running"
                if existing_status in TERMINAL_RUN_STATUSES and terminal_status is None:
                    next_status = existing_status
                if event_type in {"message.start", "tool.start", "tool.generating"} and existing_status not in TERMINAL_RUN_STATUSES:
                    next_status = "running"
                rejected_duplicate_active = ""
                if existing is None and next_status in ACTIVE_RUN_STATUSES:
                    active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
                    active = conn.execute(
                        f"""
                        SELECT run_id
                        FROM runs
                        WHERE session_id = ?
                          AND run_id != ?
                          AND status IN ({active_statuses})
                        ORDER BY updated_at DESC, started_at DESC
                        LIMIT 1
                        """,
                        (stable, run_id),
                    ).fetchone()
                    if active is not None:
                        active_run_id = str(_row_value(active, "run_id", "") or "")
                        rejected_duplicate_active = (
                            "rejected active run event because session already "
                            f"has active run {active_run_id}"
                        )
                        next_status = "failed"
                completed_at = timestamp if next_status in TERMINAL_RUN_STATUSES else None
                metadata = {}
                if existing is not None:
                    metadata = _json_loads(existing["metadata_json"], {})
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO runs (
                            run_id, session_id, runtime_scope_key, turn_id, runtime_session_id, status,
                            started_at, updated_at, completed_at, last_seq, error,
                            metadata_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            stable,
                            runtime_scope_key,
                            turn_id,
                            runtime_session_id,
                            next_status,
                            timestamp,
                            timestamp,
                            completed_at,
                            seq,
                            rejected_duplicate_active
                            or (str(payload.get("message") or "") if next_status == "failed" else ""),
                            _json_dumps(metadata if isinstance(metadata, dict) else {}),
                        ),
                    )
                else:
                    if completed_at is None:
                        completed_at = existing["completed_at"]
                    conn.execute(
                        """
                        UPDATE runs
                        SET session_id = ?,
                            runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                            turn_id = COALESCE(NULLIF(?, ''), turn_id),
                            runtime_session_id = COALESCE(NULLIF(?, ''), runtime_session_id),
                            status = ?,
                            updated_at = ?,
                            completed_at = ?,
                            last_seq = MAX(COALESCE(last_seq, 0), ?),
                            error = COALESCE(NULLIF(?, ''), error)
                        WHERE run_id = ?
                        """,
                        (
                            stable,
                            runtime_scope_key,
                            turn_id,
                            runtime_session_id,
                            next_status,
                            timestamp,
                            completed_at,
                            seq,
                            str(payload.get("message") or "") if next_status == "failed" else "",
                            run_id,
                        ),
                    )
            return frame

        saved = self._execute_write(_do)
        if seq > 0 and seq % RUN_EVENT_PRUNE_INTERVAL_EVENTS == 0:
            try:
                self.prune_run_events(session_id=stable)
            except Exception as exc:
                logger.debug("run event retention skipped for %s: %s", stable, exc)
        return saved

    def list_run_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        active_only: bool = False,
        runtime_scope_key: str = "",
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        stable = str(session_id or "").strip()
        if not stable:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 5000))
        params: list[Any] = [stable, int(after_seq or 0)]
        scope = str(runtime_scope_key or "").strip()
        scope_clause = ""
        if scope:
            scope_clause = "AND COALESCE(runtime_scope_key, session_id) = ?"
            params.append(scope)
        active_clause = ""
        if active_only:
            active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
            active_clause = (
                "AND run_id IN ("
                "SELECT run_id FROM runs WHERE session_id = ? "
                f"AND status IN ({active_statuses})"
                ")"
            )
            params.append(stable)
        params.append(bounded_limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT event_json
                FROM run_events
                WHERE session_id = ?
                  AND seq > ?
                  {scope_clause}
                  {active_clause}
                ORDER BY seq ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        events = []
        for row in rows:
            event = _json_loads(row["event_json"], {})
            if isinstance(event, dict):
                events.append(event)
        return events

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        normalized = str(run_id or "").strip()
        if not normalized:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (normalized,),
            ).fetchone()
        return self._run_from_row(row)

    def list_runs(
        self,
        session_id: str = "",
        *,
        runtime_scope_key: str = "",
        statuses: List[str] | None = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        stable = str(session_id or "").strip()
        scope = str(runtime_scope_key or "").strip()
        normalized_statuses = [
            str(status or "").strip()
            for status in (statuses or [])
            if str(status or "").strip()
        ]
        if not stable and not scope and not normalized_statuses:
            return []
        bounded_limit = max(1, min(int(limit or 200), 1000))
        clauses = []
        params: list[Any] = []
        if stable:
            clauses.append("session_id = ?")
            params.append(stable)
        if scope:
            clauses.append("COALESCE(runtime_scope_key, session_id) = ?")
            params.append(scope)
        if normalized_statuses:
            placeholders = ",".join("?" for _ in normalized_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(normalized_statuses)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(bounded_limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM runs
                {where_sql}
                ORDER BY updated_at DESC, started_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [run for row in rows if (run := self._run_from_row(row))]

    def fail_orphaned_active_runs(
        self,
        *,
        live_runtime_session_ids: set[str] | None = None,
        current_pid: int | None = None,
        current_gateway_instance_id: str = "",
        stale_after_seconds: float = 300.0,
        reason: str = "runtime owner is no longer available",
    ) -> int:
        """Fail active runs whose runtime owner cannot be reached.

        A gateway restart loses in-process runtime containers. New runs carry
        owner metadata so recovery can distinguish dead owners from active
        gateway processes sharing the same state DB. Older rows without owner
        metadata are only failed after a short stale window.
        """
        live_runtime_session_ids = {
            str(value or "").strip()
            for value in (live_runtime_session_ids or set())
            if str(value or "").strip()
        }
        now = time.time()
        stale_after = max(0.0, float(stale_after_seconds or 0))
        active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
        instance_id = str(current_gateway_instance_id or "").strip()

        def _should_fail(row: sqlite3.Row) -> bool:
            runtime_session_id = str(row["runtime_session_id"] or "").strip()
            if runtime_session_id and runtime_session_id in live_runtime_session_ids:
                return False
            metadata = _json_loads(row["metadata_json"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            owner_instance = str(metadata.get("gateway_instance_id") or "").strip()
            try:
                owner_pid = int(metadata.get("gateway_pid") or 0)
            except (TypeError, ValueError):
                owner_pid = 0
            if owner_pid > 0:
                if (
                    current_pid is not None
                    and owner_pid == current_pid
                    and owner_instance
                    and owner_instance != instance_id
                ):
                    return True
                return not _pid_is_alive(owner_pid, current_pid=current_pid)
            updated_at = float(row["updated_at"] or row["started_at"] or 0)
            return now - updated_at >= stale_after

        def _do(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                f"""
                SELECT *
                FROM runs
                WHERE status IN ({active_statuses})
                """
            ).fetchall()
            failed = 0
            for row in rows:
                if not _should_fail(row):
                    continue
                metadata = _json_loads(row["metadata_json"], {})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["recovery_reason"] = reason
                terminal_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM run_events WHERE session_id = ?",
                    (row["session_id"],),
                ).fetchone()
                terminal_seq = int(_row_value(terminal_seq_row, "next_seq", 1) or 1)
                terminal_payload = {
                    "run_id": row["run_id"],
                    "turn_id": row["turn_id"],
                    "status": "failed",
                    "message": reason,
                    "recovery": True,
                }
                terminal_frame = {
                    "type": "message.complete",
                    "session_id": row["runtime_session_id"] or row["session_id"],
                    "stored_session_id": row["session_id"],
                    "run_id": row["run_id"],
                    "turn_id": row["turn_id"],
                    "runtime_scope_key": row["runtime_scope_key"] or row["session_id"],
                    "seq": terminal_seq,
                    "timestamp": now,
                    "payload": terminal_payload,
                }
                conn.execute(
                    """
                    INSERT INTO run_events (
                        session_id, run_id, turn_id, runtime_session_id, runtime_scope_key, event_type,
                        seq, timestamp, payload_json, event_json, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["session_id"],
                        row["run_id"],
                        row["turn_id"],
                        row["runtime_session_id"],
                        row["runtime_scope_key"] or row["session_id"],
                        "message.complete",
                        terminal_seq,
                        now,
                        _json_dumps(terminal_payload),
                        _json_dumps(terminal_frame),
                        "failed",
                    ),
                )
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'failed',
                        updated_at = ?,
                        completed_at = COALESCE(completed_at, ?),
                        last_seq = MAX(COALESCE(last_seq, 0), ?),
                        error = COALESCE(NULLIF(error, ''), ?),
                        metadata_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        now,
                        now,
                        terminal_seq,
                        reason,
                        _json_dumps(metadata),
                        row["run_id"],
                    ),
                )
                failed += 1
            return failed

        return self._execute_write(_do)

    def _archive_run_event_rows(
        self,
        conn: sqlite3.Connection,
        rows: List[sqlite3.Row],
        *,
        reason: str,
    ) -> None:
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault((str(row["session_id"] or ""), str(row["run_id"] or "")), []).append(row)
        archived_at = time.time()
        for (session_id, run_id), group in grouped.items():
            seqs = [int(row["seq"] or 0) for row in group]
            timestamps = [float(row["timestamp"] or 0) for row in group]
            conn.execute(
                """
                INSERT INTO run_event_archives (
                    session_id, run_id, archived_at, first_seq, last_seq,
                    first_timestamp, last_timestamp, event_count, reason,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    archived_at,
                    min(seqs),
                    max(seqs),
                    min(timestamps),
                    max(timestamps),
                    len(group),
                    reason,
                    _json_dumps({"policy": "run_event_retention"}),
                ),
            )

    def prune_run_events(
        self,
        *,
        session_id: str = "",
        retention_days: int = DEFAULT_RUN_EVENT_RETENTION_DAYS,
        max_events_per_session: int = DEFAULT_RUN_EVENT_MAX_PER_SESSION,
        now: float | None = None,
    ) -> Dict[str, Any]:
        """Prune non-active run events and record archive summaries.

        Active run events are never deleted.  Terminal run metadata stays in
        ``runs``; only verbose stream events are pruned after the retention
        window or when a session exceeds the configured event cap.
        """
        stable_filter = str(session_id or "").strip()
        cutoff = float(now or time.time()) - max(1, int(retention_days or 1)) * 86400
        max_per_session = max(100, int(max_events_per_session or DEFAULT_RUN_EVENT_MAX_PER_SESSION))

        def _delete_rows(conn: sqlite3.Connection, rows: List[sqlite3.Row], reason: str) -> int:
            if not rows:
                return 0
            self._archive_run_event_rows(conn, rows, reason=reason)
            ids = [int(row["id"]) for row in rows]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM run_events WHERE id IN ({placeholders})", tuple(chunk))
            return len(ids)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            total_deleted = 0
            active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
            age_params: list[Any] = [cutoff]
            session_clause = ""
            if stable_filter:
                session_clause = "AND e.session_id = ?"
                age_params.append(stable_filter)
            aged_rows = conn.execute(
                f"""
                SELECT e.*
                FROM run_events e
                LEFT JOIN runs r ON r.run_id = e.run_id
                WHERE e.timestamp < ?
                  {session_clause}
                  AND COALESCE(r.status, '') NOT IN ({active_statuses})
                ORDER BY e.session_id, e.seq
                """,
                tuple(age_params),
            ).fetchall()
            total_deleted += _delete_rows(conn, aged_rows, "retention_days")

            sessions_sql = "SELECT DISTINCT session_id FROM run_events"
            session_params: tuple[Any, ...] = ()
            if stable_filter:
                sessions_sql += " WHERE session_id = ?"
                session_params = (stable_filter,)
            sessions = [
                str(row["session_id"] or "")
                for row in conn.execute(sessions_sql, session_params).fetchall()
            ]
            for sid in sessions:
                rows = conn.execute(
                    f"""
                    SELECT e.*
                    FROM run_events e
                    LEFT JOIN runs r ON r.run_id = e.run_id
                    WHERE e.session_id = ?
                      AND COALESCE(r.status, '') NOT IN ({active_statuses})
                    ORDER BY e.seq DESC
                    """,
                    (sid,),
                ).fetchall()
                overflow = rows[max_per_session:]
                if overflow:
                    total_deleted += _delete_rows(conn, list(reversed(overflow)), "max_events_per_session")
            return {
                "deleted_events": total_deleted,
                "retention_days": int(retention_days or DEFAULT_RUN_EVENT_RETENTION_DAYS),
                "max_events_per_session": max_per_session,
            }

        return self._execute_write(_do)

    def get_session_run_status(self, session_id: str) -> Dict[str, Any]:
        stable = str(session_id or "").strip()
        if not stable:
            return {
                "running": False,
                "active_run_id": "",
                "active_turn_id": "",
                "runtime_scope_key": "",
                "run_started_at": 0,
                "run_updated_at": 0,
                "last_event_seq": 0,
            }
        with self._lock:
            active = self._conn.execute(
                f"""
                SELECT * FROM runs
                WHERE session_id = ?
                  AND status IN ({_sql_status_literals(ACTIVE_RUN_STATUSES)})
                ORDER BY updated_at DESC, started_at DESC
                LIMIT 1
                """,
                (stable,),
            ).fetchone()
            last = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM run_events WHERE session_id = ?",
                (stable,),
            ).fetchone()
        run = self._run_from_row(active)
        return {
            "running": bool(run),
            "active_run_id": str((run or {}).get("run_id") or ""),
            "active_turn_id": str((run or {}).get("turn_id") or ""),
            "runtime_scope_key": str((run or {}).get("runtime_scope_key") or ""),
            "run_started_at": float((run or {}).get("started_at") or 0),
            "run_updated_at": float((run or {}).get("updated_at") or 0),
            "last_event_seq": int(_row_value(last, "last_seq", 0) or 0),
        }
