"""RunRepo protocol + concrete impl (spec §4.2) — runs + run_events."""

from __future__ import annotations

import sqlite3
import time
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hermes_agent.domain.canonical_event import CanonicalEvent as DomainCanonicalEvent
from hermes_agent.domain.event_ledger import EventLedger, LedgerEvent
from hermes_agent.domain.run_identity import (
    RunIdentity,
    ensure_run_identity_compatible,
)
from hermes_agent.domain.run_event_codec import decode_run_event_row, encode_run_event_frame
from hermes_agent.domain.run_event_index import (
    project_run_event_search_index_from_row,
    runtime_source_seq_from_event,
)
from hermes_agent.domain.run_event_stream import (
    is_coalescible_stream_delta,
    merge_stream_payload,
    stream_compaction_boundaries,
    stream_events_can_coalesce,
)
from hermes_agent.domain.seq_allocator import allocate_run_event_seq
from hermes_agent.domain.run_state_machine import TERMINAL_RUN_STATUSES
from hermes_agent.domain.run_state_machine import error_for_status
from hermes_agent.domain.run_state_machine import event_opens_active_run
from hermes_agent.domain.run_state_machine import prefer_terminal_run_status
from hermes_agent.domain.run_state_machine import resolve_explicit_run_status
from hermes_agent.domain.run_state_machine import resolve_run_status_transition
from hermes_agent.domain.run_state_machine import terminal_status_from_event
from hermes_agent.domain.run_terminator import (
    TerminateCause,
    TerminateResult,
    terminate_run as _terminate_run_atomic,
)
from hermes_agent.repositories.base import RepositoryConnection
from hermes_agent.read_models.tool_events import TOOL_EVENT_TYPES, project_tool_event
from hermes_team_mission.runtime.run_event_retention import RunEventRetentionPolicy


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    session_id: str
    turn_id: str = ""
    runtime_scope_key: str = ""
    worker_id: str = ""
    agent_profile_id: str = ""
    execution_session_id: str = ""
    status: str = "running"


@dataclass(frozen=True)
class Run:
    run_id: str
    session_id: str
    status: str
    started_at: float
    updated_at: float
    completed_at: float | None = None
    turn_id: str = ""
    runtime_scope_key: str = ""
    worker_id: str = ""
    agent_profile_id: str = ""
    execution_session_id: str = ""
    last_seq: int = 0
    terminal_seq: int = 0
    terminal_degraded: bool = False
    terminal_cause: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalEventSpec:
    """L1 persistence-layer append parameter for ``RunRepo.append_event``.

    Distinct from ``hermes_agent.domain.CanonicalEvent`` (spec §6.2 line
    412-420) — that one is the **typed wire event** with
    ``payload: TypedPayload``. This dataclass is the **serialized form**
    heading into the SQLite ``run_events.payload_json`` column, so
    ``payload`` is intentionally a plain dict (whatever the caller
    produced by ``dataclasses.asdict()`` on the typed payload).

    Use ``from_canonical(event)`` to convert from the typed form when
    the caller is holding a domain ``CanonicalEvent``.

    Audit note (docs/v3_audit_report.md §五 #1) confirmed this is not a
    spec §6.2 violation once distinguished from ``CanonicalEvent``.
    """

    event_type: str
    payload: dict[str, Any]
    run_id: str
    turn_id: str = ""
    preassigned_seq: int | None = None

    @classmethod
    def from_canonical(cls, event: "DomainCanonicalEvent") -> "CanonicalEventSpec":
        """Build a persistence spec from a typed domain ``CanonicalEvent``."""
        from dataclasses import asdict

        return cls(
            event_type=event.type.value,
            payload=asdict(event.payload),
            run_id=event.run_id,
            turn_id=event.turn_id or "",
            preassigned_seq=event.seq,
        )


@runtime_checkable
class RunRepo(Protocol):
    def create_run(self, session_id: str, spec: RunSpec) -> Run: ...

    def claim_identity(self, incoming: RunIdentity) -> RunIdentity: ...

    def upsert_materialized_state(
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
    ) -> Run: ...

    def get_run(self, run_id: str) -> Run | None: ...

    def update_metadata(self, run_id: str, metadata: dict[str, Any]) -> None: ...

    def refresh_last_seq(self, run_id: str) -> None: ...

    def reset_last_seq_from_events(self, run_id: str) -> None: ...

    def append_event(
        self,
        session_id: str,
        event: CanonicalEventSpec,
        *,
        allocate_seq: bool = True,
    ) -> int: ...

    def append_runtime_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        participant_id: str = "",
        activity_id: str = "",
    ) -> dict[str, Any]: ...

    def mark_projected_message(
        self,
        *,
        session_id: str,
        seq: int,
        conversation_message_id: str,
    ) -> bool: ...

    def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int = 0,
        types: set[str] | None = None,
        include_internal: bool = False,
        limit: int = 200,
    ) -> list[LedgerEvent]: ...

    def list_tool_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[LedgerEvent]: ...

    def set_terminal(
        self,
        run_id: str,
        session_id: str,
        target_status: str,
        cause: TerminateCause | str = TerminateCause.WORKER_EMITTED,
        message: str = "",
    ) -> TerminateResult: ...


class RunRepoImpl:
    """SQLite-backed RunRepo (spec §4.2).

    * ``append_event`` and ``list_events`` delegate to ``EventLedger`` so
      run_events is the one ledger (spec §6.1).
    * ``set_terminal`` delegates to ``domain.run_terminator.terminate_run`` —
      the ONLY authorised terminal transition (spec §7.2).
    """

    _TOOL_EVENT_TYPES = frozenset({
        "tool.start",
        "tool.generating",
        "tool.progress",
        "tool.delta",
        "tool.complete",
    })

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn
        self._ledger = EventLedger(conn)
        self._retention = RunEventRetentionPolicy()

    # ------------------------------------------------------------------

    def claim_identity(self, incoming: RunIdentity) -> RunIdentity:
        """Atomically validate and fill unclaimed durable identity fields."""
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (incoming.run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"run {incoming.run_id!r} must exist before launch")
        existing = _identity_from_run_row(row)
        claimed = existing.claimed_with(incoming)
        self._conn.execute(
            """
            UPDATE runs
               SET runtime_scope_key = ?,
                   worker_id = ?,
                   agent_profile_id = ?
             WHERE run_id = ?
            """,
            (
                claimed.runtime_scope_key,
                claimed.worker_id,
                claimed.agent_profile_id,
                claimed.run_id,
            ),
        )
        return claimed

    def create_run(self, session_id: str, spec: RunSpec) -> Run:
        stable_sid = str(session_id or "").strip()
        stable_run = str(spec.run_id or "").strip()
        if not stable_sid or not stable_run:
            raise ValueError("session_id and RunSpec.run_id are required")
        spec_session = str(spec.session_id or stable_sid).strip()
        if spec_session != stable_sid:
            raise ValueError("session_id and RunSpec.session_id must match")
        incoming_identity = RunIdentity.create(
            run_id=stable_run,
            session_id=stable_sid,
            runtime_scope_key=spec.runtime_scope_key,
            worker_id=spec.worker_id,
            agent_profile_id=spec.agent_profile_id,
        )
        existing = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (stable_run,),
        ).fetchone()
        if existing is not None:
            ensure_run_identity_compatible(
                _identity_from_run_row(existing),
                incoming_identity,
            )
            got = self.get_run(stable_run)
            assert got is not None
            return got
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO runs (
                run_id, session_id, runtime_scope_key, worker_id,
                agent_profile_id, turn_id,
                execution_session_id, status, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_run,
                stable_sid,
                incoming_identity.runtime_scope_key,
                incoming_identity.worker_id,
                incoming_identity.agent_profile_id,
                str(spec.turn_id or ""),
                str(spec.execution_session_id or ""),
                str(spec.status or "running"),
                now,
                now,
            ),
        )
        got = self.get_run(stable_run)
        assert got is not None
        return got

    def upsert_materialized_state(
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
    ) -> Run:
        stable_run = str(run_id or "").strip()
        stable_sid = str(session_id or "").strip()
        if not stable_run or not stable_sid:
            raise ValueError("run_id and session_id are required")
        now = time.time()
        started = float(started_at or now)
        updated = float(updated_at or now)
        incoming_status = str(status or "running").strip() or "running"
        normalized_scope = str(runtime_scope_key or stable_sid).strip()
        incoming_metadata = metadata if isinstance(metadata, dict) else {}
        existing = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (stable_run,),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, session_id, runtime_scope_key, worker_id,
                    agent_profile_id, turn_id, execution_session_id, status,
                    started_at, updated_at, completed_at, last_seq, error,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_run,
                    stable_sid,
                    normalized_scope,
                    "",
                    "",
                    str(turn_id or ""),
                    str(execution_session_id or ""),
                    incoming_status,
                    started,
                    updated,
                    completed_at,
                    int(last_seq or 0),
                    str(error or ""),
                    _json_dumps(incoming_metadata),
                ),
            )
        else:
            persisted_identity = _identity_from_run_row(existing)
            # Event ingestion may create a placeholder from legacy execution
            # hints before RunContext resolves the canonical conversation and
            # participant scope.  The worker claim is the seal: before it,
            # materialization may canonicalize session/scope; after it, every
            # identity dimension is immutable.
            identity_is_claimed = bool(persisted_identity.worker_id)
            if identity_is_claimed:
                ensure_run_identity_compatible(
                    persisted_identity,
                    RunIdentity.create(
                        run_id=stable_run,
                        session_id=stable_sid,
                        runtime_scope_key=normalized_scope,
                    ),
                )
            existing_status = str(existing["status"] or "")
            next_status = resolve_explicit_run_status(
                existing_status=existing_status,
                incoming_status=incoming_status,
            )
            next_completed_at = completed_at
            if next_completed_at is None:
                next_completed_at = existing["completed_at"]
            if next_status in TERMINAL_RUN_STATUSES and next_completed_at is None:
                next_completed_at = updated
            merged_metadata = _json_loads(existing["metadata_json"], {})
            if not isinstance(merged_metadata, dict):
                merged_metadata = {}
            merged_metadata.update(incoming_metadata)
            next_error = error_for_status(
                status=next_status,
                payload={"message": str(error or "")},
                existing_error=str(existing["error"] or ""),
            )
            self._conn.execute(
                """
                UPDATE runs
                SET session_id = ?,
                    runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                    turn_id = COALESCE(NULLIF(?, ''), turn_id),
                    execution_session_id = COALESCE(NULLIF(?, ''), execution_session_id),
                    status = ?,
                    updated_at = ?,
                    completed_at = ?,
                    last_seq = CASE
                        WHEN COALESCE(last_seq, 0) >= ? THEN COALESCE(last_seq, 0)
                        ELSE ?
                    END,
                    error = ?,
                    metadata_json = ?
                WHERE run_id = ?
                """,
                (
                    (
                        persisted_identity.session_id
                        if identity_is_claimed
                        else stable_sid
                    ),
                    normalized_scope,
                    str(turn_id or ""),
                    str(execution_session_id or ""),
                    next_status,
                    updated,
                    next_completed_at,
                    int(last_seq or 0),
                    int(last_seq or 0),
                    next_error,
                    _json_dumps(merged_metadata),
                    stable_run,
                ),
            )
        got = self.get_run(stable_run)
        assert got is not None
        return got

    def get_run(self, run_id: str) -> Run | None:
        stable = str(run_id or "").strip()
        if not stable:
            return None
        row = self._conn.execute(
            """
            SELECT run_id, session_id, status, started_at, updated_at,
                   completed_at, turn_id, runtime_scope_key, worker_id,
                   agent_profile_id, execution_session_id,
                   last_seq, terminal_seq, terminal_degraded, terminal_cause,
                   error, metadata_json
              FROM runs
             WHERE run_id = ?
            """,
            (stable,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_run(row)

    def update_metadata(self, run_id: str, metadata: dict[str, Any]) -> None:
        stable = str(run_id or "").strip()
        if not stable:
            return
        self._conn.execute(
            """
            UPDATE runs
            SET metadata_json = ?
            WHERE run_id = ?
            """,
            (_json_dumps(metadata if isinstance(metadata, dict) else {}), stable),
        )

    def refresh_last_seq(self, run_id: str) -> None:
        stable = str(run_id or "").strip()
        if not stable:
            return
        # ``runs.last_seq`` is a monotonic high-water mark maintained when
        # events are appended or terminal state is written. Maintenance jobs may
        # compact/delete old run_events rows, so recomputing from the ledger is
        # both expensive and semantically wrong.
        return

    def reset_last_seq_from_events(self, run_id: str) -> None:
        """Reset maintenance-visible last_seq to the current ledger max.

        Append paths keep ``last_seq`` monotonic while the run is live. Once a
        maintenance job deletes or rewrites durable event rows, the materialized
        run state must describe the remaining ledger, not a stale high-water
        value from rows that no longer exist.
        """
        stable = str(run_id or "").strip()
        if not stable:
            return
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(seq), 0) AS last_seq
              FROM run_events
             WHERE run_id = ?
            """,
            (stable,),
        ).fetchone()
        next_last_seq = int(row["last_seq"] if row else 0)
        self._conn.execute(
            """
            UPDATE runs
               SET last_seq = ?
             WHERE run_id = ?
            """,
            (next_last_seq, stable),
        )

    def append_event(
        self,
        session_id: str,
        event: CanonicalEventSpec,
        *,
        allocate_seq: bool = True,
    ) -> int:
        preassigned = None if allocate_seq else event.preassigned_seq
        outcome = self._ledger.append(
            session_id=session_id,
            run_id=event.run_id,
            event_type=event.event_type,
            payload=event.payload,
            turn_id=event.turn_id,
            preassigned_seq=preassigned,
        )
        return outcome.seq

    def append_runtime_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        participant_id: str = "",
        activity_id: str = "",
    ) -> dict[str, Any]:
        """Persist one normalized runtime frame before transport delivery.

        Runtime-provided sequence numbers are retained as source metadata. The
        repository allocates the canonical, conversation-scoped sequence in the
        same transaction that appends the ledger row and updates projections.
        """

        stable = str(session_id or "").strip()
        frame = dict(event or {})
        event_type = str(frame.get("type") or "").strip()
        if not stable or not event_type:
            raise ValueError("session_id and event.type are required")
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        payload = dict(payload)
        frame["payload"] = payload
        run_id = _event_text(frame, payload, "run_id", "runId")
        turn_id = _event_text(frame, payload, "turn_id", "turnId")
        inbound_session_id = _first_text(frame.get("session_id"), payload.get("session_id"))
        execution_session_id = _first_text(
            frame.get("execution_session_id"),
            payload.get("execution_session_id"),
            inbound_session_id if inbound_session_id != stable else "",
        )
        runtime_scope_key = _first_text(
            frame.get("runtime_scope_key"),
            payload.get("runtime_scope_key"),
            stable,
        )
        event_participant_id = _first_text(
            participant_id,
            frame.get("participant_id"),
            frame.get("participantId"),
            payload.get("participant_id"),
            payload.get("participantId"),
        )
        event_activity_id = _first_text(
            activity_id,
            frame.get("activity_id"),
            frame.get("activityId"),
            payload.get("activity_id"),
            payload.get("activityId"),
        )
        timestamp = _float_value(frame.get("timestamp"), time.time())
        inbound_seq = _positive_int(frame.get("seq"))
        if inbound_seq > 0 and runtime_source_seq_from_event(frame) <= 0:
            frame["runtime_source_seq"] = inbound_seq
        runtime_source_seq = runtime_source_seq_from_event(frame)
        terminal_status = terminal_status_from_event(event_type, payload)
        existing = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone() if run_id else None
        existing_status = str(existing["status"] or "") if existing is not None else ""
        if (
            existing is not None
            and existing_status in TERMINAL_RUN_STATUSES
            and terminal_status in TERMINAL_RUN_STATUSES
            and prefer_terminal_run_status(existing_status, terminal_status) == existing_status
        ):
            terminal_event = self._existing_terminal_event(
                stable,
                run_id,
                existing_status,
                existing,
            )
            return terminal_event

        canonical_seq = allocate_run_event_seq(
            self._conn,
            session_id=stable,
            updated_at=timestamp,
        )
        frame.update(
            {
                "conversation_session_id": stable,
                "session_id": stable,
                "execution_session_id": execution_session_id,
                "runtime_scope_key": runtime_scope_key,
                "run_id": run_id,
                "turn_id": turn_id,
                "participant_id": event_participant_id,
                "seq": canonical_seq,
                "timestamp": timestamp,
            }
        )
        if event_activity_id:
            frame["activity_id"] = event_activity_id
        interaction_request_id = _first_text(
            payload.get("interaction_request_id"),
            payload.get("request_id"),
        )
        interaction_kind = _first_text(payload.get("interaction_kind"), payload.get("kind"))
        interaction_status = _first_text(
            payload.get("interaction_status"),
            payload.get("status"),
            payload.get("state"),
        )
        anchor_seq = _positive_int(payload.get("anchor_seq") or frame.get("anchor_seq"))
        ignored_after_terminal = bool(
            existing_status in TERMINAL_RUN_STATUSES
            and terminal_status is None
            and event_opens_active_run(event_type)
        )
        inserted_row = self._try_coalesce_runtime_stream(
            session_id=stable,
            frame=frame,
            run_id=run_id,
            turn_id=turn_id,
            execution_session_id=execution_session_id,
            runtime_scope_key=runtime_scope_key,
            participant_id=event_participant_id,
            activity_id=event_activity_id,
            seq=canonical_seq,
            timestamp=timestamp,
            status="ignored_after_terminal" if ignored_after_terminal else terminal_status or "",
            runtime_source_seq=runtime_source_seq,
        )
        if inserted_row is None:
            frame_blob, frame_format = encode_run_event_frame(frame)
            self._ledger.append_runtime_frame(
                session_id=stable,
                run_id=run_id,
                turn_id=turn_id,
                execution_session_id=execution_session_id,
                runtime_scope_key=runtime_scope_key,
                participant_id=event_participant_id,
                activity_id=event_activity_id or None,
                event_type=event_type,
                seq=canonical_seq,
                timestamp=timestamp,
                payload_json=_json_dumps(payload),
                event_json=_json_dumps(frame),
                status="ignored_after_terminal" if ignored_after_terminal else terminal_status or "",
                frame_blob=frame_blob,
                frame_format=frame_format,
                retention_class=self._retention.classify_event_type(event_type),
                interaction_request_id=interaction_request_id or None,
                interaction_kind=interaction_kind or None,
                interaction_status=interaction_status or None,
                anchor_seq=anchor_seq,
                projection_state="raw",
                runtime_source_seq=runtime_source_seq,
            )
            inserted_row = self._conn.execute(
                "SELECT * FROM run_events WHERE session_id = ? AND seq = ?",
                (stable, canonical_seq),
            ).fetchone()
        else:
            frame = decode_run_event_row(inserted_row)
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if inserted_row is None:
            raise RuntimeError(f"run event append failed for {stable}/{canonical_seq}")
        project_run_event_search_index_from_row(self._conn, inserted_row)
        if event_type in TOOL_EVENT_TYPES and not ignored_after_terminal:
            projected_tool = project_tool_event(self._conn, frame)
            if isinstance(projected_tool, dict) and projected_tool.get("id"):
                frame["_projected_tool_event_id"] = projected_tool["id"]
                self._ledger.mark_projected_tool_event(
                    row_id=int(inserted_row["id"]),
                    tool_event_id=str(projected_tool["id"]),
                )
        if ignored_after_terminal:
            frame["_persistence_disposition"] = "ignored_after_terminal"
            return frame
        if run_id:
            transition = resolve_run_status_transition(
                event_type=event_type,
                existing_status=existing_status,
                terminal_status=terminal_status,
                has_existing_run=existing is not None,
            )
            if transition.should_track:
                metadata = _json_loads(existing["metadata_json"], {}) if existing is not None else {}
                if not isinstance(metadata, dict):
                    metadata = {}
                owner_metadata = frame.get("owner_metadata")
                if isinstance(owner_metadata, dict):
                    metadata.update(owner_metadata)
                self.upsert_materialized_state(
                    run_id=run_id,
                    session_id=stable,
                    runtime_scope_key=runtime_scope_key,
                    turn_id=turn_id,
                    execution_session_id=execution_session_id,
                    status=transition.status,
                    started_at=timestamp,
                    updated_at=timestamp,
                    completed_at=(
                        timestamp if transition.status in TERMINAL_RUN_STATUSES else None
                    ),
                    last_seq=canonical_seq,
                    error=error_for_status(
                        status=transition.status,
                        payload=payload,
                        existing_error=str(existing["error"] or "") if existing is not None else "",
                    ),
                    metadata=metadata,
                )
        return frame

    def _try_coalesce_runtime_stream(
        self,
        *,
        session_id: str,
        frame: dict[str, Any],
        run_id: str,
        turn_id: str,
        execution_session_id: str,
        runtime_scope_key: str,
        participant_id: str,
        activity_id: str,
        seq: int,
        timestamp: float,
        status: str,
        runtime_source_seq: int,
    ) -> Any | None:
        if not is_coalescible_stream_delta(frame):
            return None
        event_type = str(frame.get("type") or "").strip()
        boundaries = tuple(sorted(stream_compaction_boundaries(event_type)))
        placeholders = ",".join("?" for _ in boundaries)
        boundary = self._conn.execute(
            f"""
            SELECT COALESCE(MAX(seq), 0) AS boundary_seq
              FROM run_events
             WHERE session_id = ?
               AND (? = '' OR run_id = ?)
               AND (? = '' OR turn_id = ?)
               AND event_type IN ({placeholders})
            """,
            (session_id, run_id, run_id, turn_id, turn_id, *boundaries),
        ).fetchone()
        boundary_seq = int(boundary["boundary_seq"] if boundary is not None else 0)
        candidates = self._conn.execute(
            """
            SELECT * FROM run_events
             WHERE session_id = ?
               AND event_type = ?
               AND (? = '' OR run_id = ?)
               AND (? = '' OR turn_id = ?)
               AND COALESCE(runtime_scope_key, '') = ?
               AND seq > ?
             ORDER BY seq DESC, id DESC
             LIMIT 128
            """,
            (
                session_id,
                event_type,
                run_id,
                run_id,
                turn_id,
                turn_id,
                runtime_scope_key,
                boundary_seq,
            ),
        ).fetchall()
        previous_row = None
        previous_event: dict[str, Any] = {}
        for candidate in candidates:
            decoded = decode_run_event_row(candidate)
            if stream_events_can_coalesce(decoded, frame):
                previous_row = candidate
                previous_event = decoded
                break
        if previous_row is None:
            return None
        occupied = self._conn.execute(
            "SELECT 1 FROM run_events WHERE session_id = ? AND seq = ? AND id != ? LIMIT 1",
            (session_id, seq, int(previous_row["id"])),
        ).fetchone()
        if occupied is not None:
            return None

        merged_payload = merge_stream_payload(previous_event, frame)
        merged = {
            **previous_event,
            "session_id": session_id,
            "conversation_session_id": session_id,
            "execution_session_id": execution_session_id,
            "runtime_scope_key": runtime_scope_key,
            "run_id": run_id,
            "turn_id": turn_id,
            "participant_id": participant_id,
            "seq": seq,
            "timestamp": timestamp,
            "payload": merged_payload,
        }
        if activity_id:
            merged["activity_id"] = activity_id
        if runtime_source_seq > 0:
            merged["runtime_source_seq"] = runtime_source_seq
        frame_blob, frame_format = encode_run_event_frame(merged)
        self._ledger.rewrite_runtime_frame_row(
            row_id=int(previous_row["id"]),
            run_id=run_id,
            turn_id=turn_id,
            execution_session_id=execution_session_id,
            runtime_scope_key=runtime_scope_key,
            participant_id=participant_id,
            activity_id=activity_id or None,
            seq=seq,
            timestamp=timestamp,
            payload_json=_json_dumps(merged_payload),
            event_json=_json_dumps(merged),
            status=status,
            frame_blob=frame_blob,
            frame_format=frame_format,
            retention_class=self._retention.classify_event_type(event_type),
            runtime_source_seq=runtime_source_seq,
        )
        return self._conn.execute(
            "SELECT * FROM run_events WHERE id = ?",
            (int(previous_row["id"]),),
        ).fetchone()

    def _existing_terminal_event(
        self,
        session_id: str,
        run_id: str,
        status: str,
        run_row: Any,
    ) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT * FROM run_events
             WHERE session_id = ? AND run_id = ? AND status = ?
             ORDER BY seq DESC, id DESC LIMIT 1
            """,
            (session_id, run_id, status),
        ).fetchone()
        saved = decode_run_event_row(row) if row is not None else {}
        if not saved:
            saved = {
                "type": "message.complete",
                "session_id": session_id,
                "conversation_session_id": session_id,
                "execution_session_id": str(run_row["execution_session_id"] or ""),
                "run_id": run_id,
                "seq": int(run_row["last_seq"] or 0),
                "payload": {"status": "complete" if status == "completed" else status},
            }
        saved["_persistence_disposition"] = "duplicate_terminal"
        return saved

    def mark_projected_message(
        self,
        *,
        session_id: str,
        seq: int,
        conversation_message_id: str,
    ) -> bool:
        stable = str(session_id or "").strip()
        stable_message_id = str(conversation_message_id or "").strip()
        if not stable or int(seq or 0) <= 0 or not stable_message_id:
            return False
        row = self._conn.execute(
            "SELECT id FROM run_events WHERE session_id = ? AND seq = ? LIMIT 1",
            (stable, int(seq)),
        ).fetchone()
        if row is None:
            return False
        self._ledger.mark_projected_message(
            row_id=int(row["id"]),
            conversation_message_id=stable_message_id,
        )
        return True

    def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int = 0,
        types: set[str] | None = None,
        include_internal: bool = False,
        limit: int = 200,
    ) -> list[LedgerEvent]:
        return self._ledger.list(
            session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            types=types,
            include_internal=include_internal,
            limit=limit,
        )

    def list_tool_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[LedgerEvent]:
        """Tool events are a projection over run_events (spec §6.1)."""
        return self._ledger.list(
            session_id,
            after_seq=after_seq,
            types=set(self._TOOL_EVENT_TYPES),
            include_internal=False,
            limit=limit,
        )

    def set_terminal(
        self,
        run_id: str,
        session_id: str,
        target_status: str,
        cause: TerminateCause | str = TerminateCause.WORKER_EMITTED,
        message: str = "",
    ) -> TerminateResult:
        return _terminate_run_atomic(
            self._conn,
            run_id=run_id,
            session_id=session_id,
            target_status=target_status,
            cause=cause,
            message=message,
        )


def _row_to_run(row: Any) -> Run:
    def _g(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    completed_at_raw = _g("completed_at", 5)
    return Run(
        run_id=str(_g("run_id", 0) or ""),
        session_id=str(_g("session_id", 1) or ""),
        status=str(_g("status", 2) or ""),
        started_at=float(_g("started_at", 3) or 0),
        updated_at=float(_g("updated_at", 4) or 0),
        completed_at=float(completed_at_raw) if completed_at_raw is not None else None,
        turn_id=str(_g("turn_id", 6) or ""),
        runtime_scope_key=str(_g("runtime_scope_key", 7) or ""),
        worker_id=str(_g("worker_id", 8) or ""),
        agent_profile_id=str(_g("agent_profile_id", 9) or ""),
        execution_session_id=str(_g("execution_session_id", 10) or ""),
        last_seq=int(_g("last_seq", 11) or 0),
        terminal_seq=int(_g("terminal_seq", 12) or 0),
        terminal_degraded=bool(int(_g("terminal_degraded", 13) or 0)),
        terminal_cause=str(_g("terminal_cause", 14) or ""),
        error=str(_g("error", 15) or ""),
        metadata=_json_loads(_g("metadata_json", 16), {}),
    )


def _identity_from_run_row(row: Any) -> RunIdentity:
    return RunIdentity.create(
        run_id=row["run_id"],
        session_id=row["session_id"],
        runtime_scope_key=row["runtime_scope_key"],
        worker_id=row["worker_id"],
        agent_profile_id=row["agent_profile_id"],
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _event_text(
    frame: dict[str, Any],
    payload: dict[str, Any],
    snake_key: str,
    camel_key: str,
) -> str:
    return _first_text(
        frame.get(snake_key),
        frame.get(camel_key),
        payload.get(snake_key),
        payload.get(camel_key),
    )


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _float_value(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return parsed if parsed > 0 else float(fallback)


__all__ = [
    "CanonicalEventSpec",
    "Run",
    "RunRepo",
    "RunRepoImpl",
    "RunSpec",
]
