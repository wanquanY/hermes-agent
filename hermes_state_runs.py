from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from hermes_runtime_event_payloads import primary_deliverable_text
from hermes_state_run_event_codec import (
    decode_run_event_row,
    encode_run_event_frame,
    payload_from_run_event_row,
    update_run_event_frame_columns,
)
from hermes_state_run_event_index import (
    project_run_event_search_index,
    project_run_event_search_index_from_row,
    runtime_source_seq_from_event,
)
from hermes_state_run_event_reference import (
    reference_projected_run_event_payloads,
    rehydrate_referenced_run_event,
)
from hermes_state_runtime import (
    json_loads as runtime_json_loads,
    session_info_payload_hash,
    session_info_record,
    session_runtime_identity_matches,
    session_runtime_state_from_row,
)
from hermes_state_tool_events import (
    TOOL_EVENT_TYPES,
    project_tool_event,
    tool_event_row_to_dict,
)
from hermes_team_mission.runtime.run_event_retention import (
    COALESCIBLE_STREAM_EVENT_TYPES,
    DEFAULT_RUN_EVENT_MAX_PER_SESSION,
    DEFAULT_RUN_EVENT_RETENTION_DAYS,
    RunEventRetentionPolicy,
    TERMINAL_RUN_PRUNABLE_EVENT_TYPES,
)

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
TERMINAL_RUN_STATUS_RANK = {
    "cancelled": 1,
    "interrupted": 1,
    "failed": 1,
    "completed": 2,
}
RUN_EVENT_PRUNE_INTERVAL_EVENTS = 500
CONTROL_ONLY_ACTIVE_RUN_REPAIR_STALE_SECONDS = 60.0
CONTROL_ONLY_ACTIVE_RUN_REPAIR_OWNER_DEAD_GRACE_SECONDS = 10.0
DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS = 300.0
DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS = 2.0
RUN_EVENT_RETENTION_POLICY = RunEventRetentionPolicy()
STREAM_COMPACTION_BOUNDARY_EVENT_TYPES = {
    "message.start",
    "message.complete",
    "error",
    "session.interrupted",
}
SUBAGENT_STREAM_COMPACTION_BOUNDARY_EVENT_TYPES = {
    "subagent.start",
    "subagent.tool",
    "subagent.complete",
    "subagent.error",
}
SUBAGENT_COALESCIBLE_STREAM_EVENT_TYPES = {
    "subagent.output_delta",
    "subagent.reasoning_delta",
    "subagent.thinking",
}
RUNTIME_LIFECYCLE_EVENT_PREFIXES = (
    "message.",
    "tool.",
    "subagent.",
    "approval.",
    "secret.",
    "sudo.",
    "input_approval.",
    "agent_profile_test.",
)
RUNTIME_LIFECYCLE_EVENT_TYPES = {
    "error",
    "reasoning.delta",
    "thinking.delta",
}
RUN_OPENING_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "reasoning.delta",
    "thinking.delta",
    "tool.start",
    "tool.generating",
    "tool.progress",
    "approval.request",
    "secret.request",
    "sudo.request",
    "input_approval.request",
    *COALESCIBLE_STREAM_EVENT_TYPES,
}
STREAM_IDENTITY_PAYLOAD_KEYS = (
    "subagent_id",
    "subagentId",
    "source",
    "role",
    "delegate_call_id",
    "delegateCallId",
    "tool_call_id",
    "toolCallId",
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _run_event_retention_class(event_type: str) -> str:
    return RUN_EVENT_RETENTION_POLICY.classify_event_type(str(event_type or "").strip())


def _sql_status_literals(statuses: set[str]) -> str:
    return ",".join("'" + status.replace("'", "''") + "'" for status in sorted(statuses))


def _terminal_run_storage_predicate(event_alias: str = "e", run_alias: str = "r") -> str:
    terminal_statuses = _sql_status_literals(TERMINAL_RUN_STATUSES)
    terminal_event_types = ("message.complete", "error", "session.interrupted")
    terminal_type_literals = ",".join("'" + event_type + "'" for event_type in terminal_event_types)
    return f"""
    (
        COALESCE({run_alias}.status, '') IN ({terminal_statuses})
        OR (
            COALESCE({event_alias}.run_id, '') != ''
            AND EXISTS (
                SELECT 1
                FROM run_events terminal_events
                WHERE terminal_events.session_id = {event_alias}.session_id
                  AND COALESCE(terminal_events.run_id, '') = COALESCE({event_alias}.run_id, '')
                  AND terminal_events.event_type IN ({terminal_type_literals})
                  AND COALESCE(terminal_events.status, '') IN ({terminal_statuses})
            )
        )
    )
    """


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


def _event_participant_id(event: Dict[str, Any], fallback: str = "") -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        event.get("participant_id")
        or event.get("participantId")
        or payload.get("participant_id")
        or payload.get("participantId")
        or fallback
        or ""
    ).strip()


def _event_activity_id(event: Dict[str, Any], fallback: str = "") -> str:
    """ADR-0001: extract activity_id from event frame.

    Look in three places (in order):
      1. top-level event["activity_id"] / event["activityId"]
      2. event.payload.activity_id / activityId
      3. event.metadata.activity_id / activityId
    Returns "" when nothing is found; the caller decides whether to fall
    back to RunContext or leave the column NULL for a backfill pass.
    """
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return str(
        event.get("activity_id")
        or event.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
        or metadata.get("activity_id")
        or metadata.get("activityId")
        or fallback
        or ""
    ).strip()


def _event_message_seq_in_run(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    seq = str(
        event.get("message_seq_in_run")
        or event.get("messageSeqInRun")
        or payload.get("message_seq_in_run")
        or payload.get("messageSeqInRun")
        or ""
    ).strip()
    if seq:
        return seq
    source_seq = str(event.get("seq") or payload.get("seq") or "").strip()
    return f"legacy-source-seq:{source_seq}" if source_seq else ""


def _event_message_text(payload: Dict[str, Any]) -> str:
    for key in ("text", "content", "output", "final_response", "finalResponse", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _event_reasoning_text(payload: Dict[str, Any]) -> str:
    for key in ("reasoning", "reasoning_content", "reasoningContent", "thinking", "thought"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _looks_like_team_visible_transcript(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    event: Dict[str, Any],
    runtime_scope_key: str,
    participant_id: str,
) -> bool:
    if session_id.startswith(("team-session-team-conversation-", "team:mission-")):
        return True
    if runtime_scope_key.startswith(("team:", "member-chat:")):
        return True
    if participant_id.startswith(("leader:", "member:")):
        return True
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for value in (
        event.get("conversation_session_id"),
        event.get("conversationSessionId"),
        payload.get("conversation_session_id"),
        payload.get("conversationSessionId"),
    ):
        if str(value or "").strip().startswith("team-session-team-conversation-"):
            return True
    try:
        row = conn.execute(
            """
            SELECT conversation_kind
            FROM session_index
            WHERE session_id = ?
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return str(_row_value(row, "conversation_kind", "") or "").strip().lower() == "team"


def _recovery_activity_id_for_run(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Return a non-empty activity id for internally synthesized run recovery events."""
    run_id = str(_row_value(row, "run_id", "") or "").strip()
    if run_id:
        try:
            binding = conn.execute(
                "SELECT mission_id FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            binding = None
        mission_id = str(_row_value(binding, "mission_id", "") or "").strip()
        if mission_id:
            return f"mission:{mission_id}"
    session_id = str(_row_value(row, "session_id", "") or "").strip()
    if session_id.startswith("team-session-team-conversation-"):
        conversation_id = session_id.removeprefix("team-session-team-conversation-")
        if conversation_id:
            return f"team-conversation:{conversation_id}"
    if session_id:
        return f"chat:{session_id}"
    return ""


def _event_with_participant_id(event: Dict[str, Any], participant_id: str = "") -> Dict[str, Any]:
    normalized = dict(event or {})
    normalized_participant_id = _event_participant_id(normalized, participant_id)
    if not normalized_participant_id:
        return normalized
    normalized["participant_id"] = normalized_participant_id
    normalized["participantId"] = normalized_participant_id
    payload = normalized.get("payload")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("participant_id", normalized_participant_id)
        normalized["payload"] = payload
    return normalized


def _event_status(event_type: str, payload: Dict[str, Any]) -> str | None:
    if event_type == "error":
        return "failed"
    if event_type == "session.recalled":
        return "interrupted"
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


def _prefer_terminal_run_status(existing_status: str, next_status: str) -> str:
    existing = str(existing_status or "").strip().lower()
    incoming = str(next_status or "").strip().lower()
    if existing not in TERMINAL_RUN_STATUSES or incoming not in TERMINAL_RUN_STATUSES:
        return incoming or existing
    existing_rank = TERMINAL_RUN_STATUS_RANK.get(existing, 0)
    incoming_rank = TERMINAL_RUN_STATUS_RANK.get(incoming, 0)
    if incoming_rank > existing_rank:
        return incoming
    return existing


def _event_opens_active_run(event_type: str) -> bool:
    return str(event_type or "").strip() in RUN_OPENING_EVENT_TYPES


def _event_reopens_terminal_run(event_type: str) -> bool:
    return _event_opens_active_run(event_type)


def _runtime_lifecycle_event_sql(column: str) -> str:
    escaped_prefixes = [prefix.replace("'", "''") for prefix in RUNTIME_LIFECYCLE_EVENT_PREFIXES]
    prefix_checks = " OR ".join(f"{column} LIKE '{prefix}%'" for prefix in escaped_prefixes)
    exact_values = _sql_status_literals(RUNTIME_LIFECYCLE_EVENT_TYPES)
    return f"({column} IN ({exact_values}) OR {prefix_checks})"


def _active_runtime_run_sql(run_column: str) -> str:
    lifecycle_sql = _runtime_lifecycle_event_sql("e.event_type")
    return (
        f"(NOT EXISTS (SELECT 1 FROM run_events e WHERE e.run_id = {run_column}) "
        f"OR EXISTS (SELECT 1 FROM run_events e WHERE e.run_id = {run_column} "
        f"AND {lifecycle_sql}))"
    )


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _event_subagent_id(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        payload.get("subagent_id")
        or payload.get("subagentId")
        or payload.get("id")
        or ""
    ).strip()


def _event_text_delta(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(payload.get("text") or payload.get("delta") or payload.get("output") or "")


def _suffix_prefix_overlap(left: str, right: str) -> int:
    if not left or not right:
        return 0
    max_len = min(len(left), len(right))
    for size in range(max_len, 0, -1):
        if left.endswith(right[:size]):
            return size
    return 0


def _merge_stream_text(previous_text: str, incoming_text: str) -> str:
    previous = str(previous_text or "")
    incoming = str(incoming_text or "")
    if not incoming:
        return previous
    if not previous:
        return incoming
    if incoming == previous or incoming in previous:
        return previous
    if incoming.startswith(previous):
        return incoming
    overlap = _suffix_prefix_overlap(previous, incoming)
    if overlap > 0:
        return previous + incoming[overlap:]
    return previous + incoming


def _event_stream_mode(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(payload.get("mode") or "").strip().lower()


def _event_is_coalescible_stream_delta(event: Dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "").strip()
    if event_type not in COALESCIBLE_STREAM_EVENT_TYPES:
        return False
    return bool(_event_text_delta(event))


def _event_stream_identity(event: Dict[str, Any]) -> tuple[Any, ...]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    identity: list[Any] = [
        str(event.get("type") or "").strip(),
        _event_run_id(event),
        _event_turn_id(event),
        _event_runtime_scope_key(event),
        _event_stream_mode(event),
    ]
    for key in STREAM_IDENTITY_PAYLOAD_KEYS:
        identity.append(str(payload.get(key) or "").strip())
    return tuple(identity)


def _stream_compaction_boundary_event_types(event_type: str) -> set[str]:
    event_type = str(event_type or "").strip()
    boundaries = set(STREAM_COMPACTION_BOUNDARY_EVENT_TYPES)
    if event_type in SUBAGENT_COALESCIBLE_STREAM_EVENT_TYPES:
        boundaries.update(SUBAGENT_STREAM_COMPACTION_BOUNDARY_EVENT_TYPES)
    return boundaries


def _boundary_stream_types(event_type: str) -> set[str] | None:
    event_type = str(event_type or "").strip()
    if event_type in STREAM_COMPACTION_BOUNDARY_EVENT_TYPES:
        return None
    if event_type in SUBAGENT_STREAM_COMPACTION_BOUNDARY_EVENT_TYPES:
        return set(SUBAGENT_COALESCIBLE_STREAM_EVENT_TYPES)
    return set()


def _stream_events_can_coalesce(previous_event: Dict[str, Any], event: Dict[str, Any]) -> bool:
    return (
        _event_is_coalescible_stream_delta(previous_event)
        and _event_is_coalescible_stream_delta(event)
        and _event_stream_identity(previous_event) == _event_stream_identity(event)
    )


def _merge_stream_payload(previous_event: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    previous_payload = previous_event.get("payload") if isinstance(previous_event.get("payload"), dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    merged_payload = dict(previous_payload)
    previous_text = _event_text_delta(previous_event)
    incoming_text = _event_text_delta(event)
    merged_text = _merge_stream_text(previous_text, incoming_text)
    for key in ("text", "delta", "output"):
        if key in previous_payload or key in payload:
            merged_payload[key] = merged_text
    # Rendered fragments are live transport hints. After storage coalescing,
    # the aggregate text is the durable representation.
    merged_payload.pop("rendered", None)
    return merged_payload


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


def orphaned_active_run_decision(
    row: sqlite3.Row | dict[str, Any],
    *,
    now: float,
    live_runtime_session_ids: set[str] | None = None,
    current_pid: int | None = None,
    current_gateway_instance_id: str = "",
    stale_after_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS,
    owner_dead_grace_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS,
) -> tuple[bool, str]:
    live_runtime_session_ids = {
        str(value or "").strip()
        for value in (live_runtime_session_ids or set())
        if str(value or "").strip()
    }
    stale_after = max(0.0, float(stale_after_seconds or 0))
    owner_dead_grace = max(0.0, float(owner_dead_grace_seconds or 0))
    instance_id = str(current_gateway_instance_id or "").strip()

    metadata = _json_loads(row["metadata_json"], {})
    metadata = metadata if isinstance(metadata, dict) else {}
    updated_at = float(row["updated_at"] or row["started_at"] or 0)
    updated_age = now - updated_at
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
            return True, "same-pid-different-gateway-instance"
        if _pid_is_alive(owner_pid, current_pid=current_pid):
            return False, "owner-pid-alive"
        if updated_age < owner_dead_grace:
            return False, "owner-pid-dead-fresh"
        return True, "owner-pid-dead"
    runtime_session_id = str(row["runtime_session_id"] or "").strip()
    if runtime_session_id and runtime_session_id in live_runtime_session_ids:
        return False, "live-runtime-session"
    if updated_age >= stale_after:
        return True, "legacy-owner-metadata-stale"
    return False, "legacy-owner-metadata-fresh"


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

    def _live_owner_control_only_active_run_locked(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        exclude_run_id: str = "",
    ) -> sqlite3.Row | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
        lifecycle_sql = _runtime_lifecycle_event_sql("e.event_type")
        params: list[Any] = [stable]
        exclude_clause = ""
        normalized_exclude = str(exclude_run_id or "").strip()
        if normalized_exclude:
            exclude_clause = "AND r.run_id != ?"
            params.append(normalized_exclude)
        rows = conn.execute(
            f"""
            SELECT r.*
            FROM runs r
            WHERE r.session_id = ?
              {exclude_clause}
              AND r.status IN ({active_statuses})
              AND EXISTS (
                SELECT 1 FROM run_events e
                WHERE e.run_id = r.run_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM run_events e
                WHERE e.run_id = r.run_id
                  AND {lifecycle_sql}
              )
            ORDER BY r.updated_at DESC, r.started_at DESC
            """,
            tuple(params),
        ).fetchall()
        for row in rows:
            metadata = _json_loads(row["metadata_json"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            try:
                owner_pid = int(metadata.get("gateway_pid") or 0)
            except (TypeError, ValueError):
                owner_pid = 0
            if owner_pid > 0 and _pid_is_alive(owner_pid):
                return row
        return None

    def _repair_control_only_active_runs_locked(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str = "",
        now: float | None = None,
    ) -> int:
        """Close stale active rows that were opened only by mission control events.

        ``runs`` models live runtime turns.  Team Mission control events such as
        ``mission.approval.requested`` must remain in ``run_events`` for graph
        replay, but they must not make the leader conversation busy.

        A newly-started runtime turn can legitimately have only control frames
        such as ``session.info`` before the first ``message.start`` arrives.
        Those rows are still owned by a live gateway worker and must remain
        active; otherwise a status read can terminalize the run before any
        assistant output is persisted.
        """
        active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
        lifecycle_sql = _runtime_lifecycle_event_sql("e.event_type")
        session_clause = ""
        params: list[Any] = []
        stable = str(session_id or "").strip()
        if stable:
            session_clause = "AND r.session_id = ?"
            params.append(stable)
        rows = conn.execute(
            f"""
            SELECT r.*
            FROM runs r
            WHERE r.status IN ({active_statuses})
              {session_clause}
              AND EXISTS (
                SELECT 1 FROM run_events e
                WHERE e.run_id = r.run_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM run_events e
                WHERE e.run_id = r.run_id
                  AND {lifecycle_sql}
              )
            """,
            tuple(params),
        ).fetchall()
        if not rows:
            return 0
        repaired_at = float(now or time.time())
        stale_after = CONTROL_ONLY_ACTIVE_RUN_REPAIR_STALE_SECONDS
        owner_dead_grace = CONTROL_ONLY_ACTIVE_RUN_REPAIR_OWNER_DEAD_GRACE_SECONDS
        for row in rows:
            metadata = _json_loads(row["metadata_json"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            updated_at = float(row["updated_at"] or row["started_at"] or 0)
            updated_age = repaired_at - updated_at
            try:
                owner_pid = int(metadata.get("gateway_pid") or 0)
            except (TypeError, ValueError):
                owner_pid = 0
            if owner_pid > 0:
                if _pid_is_alive(owner_pid):
                    continue
                if updated_age < owner_dead_grace:
                    continue
            elif updated_age < stale_after:
                continue
            metadata["recovery_reason"] = "control-only run events are not active runtime runs"
            metadata["recovered_at"] = repaired_at
            conn.execute(
                """
                UPDATE runs
                SET status = 'completed',
                    updated_at = ?,
                    completed_at = COALESCE(completed_at, updated_at, ?),
                    error = COALESCE(error, ''),
                    metadata_json = ?
                WHERE run_id = ?
                """,
                (
                    repaired_at,
                    repaired_at,
                    _json_dumps(metadata),
                    row["run_id"],
                ),
            )
        return len(rows)

    def repair_control_only_active_runs(
        self,
        *,
        session_id: str = "",
        now: float | None = None,
    ) -> int:
        return self._execute_write(
            lambda conn: self._repair_control_only_active_runs_locked(
                conn,
                session_id=session_id,
                now=now,
            )
        )

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
                elif existing_status in TERMINAL_RUN_STATUSES and normalized_status in TERMINAL_RUN_STATUSES:
                    next_status = _prefer_terminal_run_status(existing_status, normalized_status)
                next_completed_at = completed_at
                if next_completed_at is None:
                    next_completed_at = existing["completed_at"]
                if next_status in TERMINAL_RUN_STATUSES and next_completed_at is None:
                    next_completed_at = updated
                merged_metadata = _json_loads(existing["metadata_json"], {})
                if isinstance(metadata, dict):
                    merged_metadata.update(metadata)
                if next_status == "failed":
                    next_error = error or str(existing["error"] or "")
                elif next_status in TERMINAL_RUN_STATUSES:
                    next_error = ""
                else:
                    next_error = error or str(existing["error"] or "")
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
                        error = ?,
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
                        next_error,
                        _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                        run_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._project_run_state_to_session_index_locked(
                conn,
                session_id=session_id,
                run_id=run_id,
                runtime_scope_key=str(runtime_scope_key or ""),
                runtime_session_id=runtime_session_id,
                status=str((row["status"] if row else normalized_status) or ""),
                updated_at=updated,
            )
            return self._run_from_row(row) or {}

        return self._execute_write(_do)

    def _project_run_state_to_session_index_locked(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        run_id: str,
        runtime_session_id: str,
        status: str,
        updated_at: float,
        runtime_scope_key: str = "",
    ) -> None:
        """Write-time projection of run state into the control-plane session_index.

        UPDATE-only: never inserts a row, so runs whose session has no user-facing
        index row (e.g. team-member runtime sessions) are untouched. Keeps the
        sidebar's running/status correct without a read-time live merge. Best-effort
        — a missing session_index table or any error must never fail the run write.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return
        is_active = str(status or "") not in TERMINAL_RUN_STATUSES
        run_scope_key = str(runtime_scope_key or "").strip()
        # Resolve the bound team-mission conversation row, if this run belongs to
        # a team mission. A node run uses session_id = "team:mission-X:node:Y"
        # which is NOT the conversation row the sidebar reads, so without this
        # extra hop a worker/verifier/synthesis run never reaches the sidebar.
        conv_sid = ""
        conv_scope_key = ""
        try:
            row = conn.execute(
                """
                SELECT tmc.stable_session_id, tmc.conversation_id
                  FROM team_mission_run_bindings tmrb
                  JOIN team_missions tm
                    ON tm.mission_id = tmrb.mission_id
                  JOIN team_mission_conversations tmc
                    ON tmc.conversation_id = tm.conversation_id
                 WHERE tmrb.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row:
                conv_sid = str(row[0] or "").strip()
                conversation_id = str(row[1] or "").strip()
                if conversation_id:
                    conv_scope_key = f"team:{conversation_id}:leader-conversation"
        except sqlite3.OperationalError:
            conv_sid = ""
            conv_scope_key = ""
        try:
            if is_active:
                # Asymmetric design: this hook is the "lit" signal — set running
                # whenever any relevant run is active. It updates BOTH the run's
                # own session row AND, if the run is bound to a team mission, the
                # conversation's session row. The "unlit" signal (clear) is
                # owned exclusively by mission lifecycle for team_mission rows
                # below, so we never need to worry about flicker between sibling
                # runs in a mission.
                if conv_sid and conv_sid != sid:
                    cur = conn.execute(
                        """
                        UPDATE session_index
                           SET running = 1, status = 'running',
                               active_run_id = ?, active_runtime_session_id = ?,
                               runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                               updated_at = MAX(updated_at, ?)
                         WHERE session_id = ?
                        """,
                        (
                            run_id,
                            str(runtime_session_id or ""),
                            run_scope_key,
                            float(updated_at or 0),
                            sid,
                        ),
                    )
                    conv_cur = conn.execute(
                        """
                        UPDATE session_index
                           SET running = 1, status = 'running',
                               active_run_id = ?, active_runtime_session_id = ?,
                               runtime_scope_key = COALESCE(NULLIF(?, ''), NULLIF(?, ''), runtime_scope_key),
                               updated_at = MAX(updated_at, ?)
                         WHERE session_id = ?
                        """,
                        (
                            run_id,
                            str(runtime_session_id or ""),
                            conv_scope_key,
                            run_scope_key,
                            float(updated_at or 0),
                            conv_sid,
                        ),
                    )
                    logger.debug(
                        "[doxie-session-index] project_run set_running session_id=%s conv_session_id=%s run_id=%s status=%s rows=%s",
                        sid, conv_sid, run_id, status, int(cur.rowcount or 0) + int(conv_cur.rowcount or 0),
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE session_index
                           SET running = 1, status = 'running',
                               active_run_id = ?, active_runtime_session_id = ?,
                               runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                               updated_at = MAX(updated_at, ?)
                         WHERE session_id = ?
                        """,
                        (
                            run_id,
                            str(runtime_session_id or ""),
                            conv_scope_key or run_scope_key,
                            float(updated_at or 0),
                            sid,
                        ),
                    )
                    logger.debug(
                        "[doxie-session-index] project_run set_running session_id=%s run_id=%s status=%s rows=%s",
                        sid, run_id, status, cur.rowcount,
                    )
            else:
                # Clear BOTH the run's own session row AND the bound team-mission
                # conversation row (conv_sid) — symmetric with the set path above,
                # which lights up both. A first/leader-only team turn runs under a
                # session id (leader or team:mission-X:node:root) that differs from
                # the conversation's session_index row, so clearing only `sid` left
                # the sidebar spinner stuck until a list read's repair pass healed
                # it (the "first message status doesn't auto-update, refresh fixes
                # it" bug). The two guards below keep multi-node missions flicker-
                # free: a sibling run terminating mid-mission is blocked by the
                # active_run_id match AND the mission-terminal check, so the
                # conversation row only clears when its run is the active one and
                # its mission (if any) has actually finished.
                clear_ids = [sid]
                if conv_sid and conv_sid != sid:
                    clear_ids.append(conv_sid)
                id_placeholders = ",".join("?" for _ in clear_ids)
                cur = conn.execute(
                    f"""
                    UPDATE session_index
                       SET running = 0, status = 'idle',
                           active_run_id = '', active_runtime_session_id = '',
                           updated_at = MAX(updated_at, ?)
                     WHERE session_id IN ({id_placeholders})
                       AND (active_run_id = ? OR active_run_id = '')
                       AND (
                           session_kind != 'team_mission'
                           OR COALESCE(mission_id, '') = ''
                           OR mission_id IN (
                               SELECT mission_id FROM team_missions
                                WHERE LOWER(COALESCE(status,'')) IN
                                      ('completed','failed','cancelled','canceled','interrupted')
                           )
                       )
                    """,
                    (float(updated_at or 0), *clear_ids, run_id),
                )
                logger.debug(
                    "[doxie-session-index] project_run clear session_id=%s conv_session_id=%s run_id=%s status=%s rows=%s",
                    sid, conv_sid, run_id, status, cur.rowcount,
                )
        except sqlite3.OperationalError:
            # session_index table absent (legacy worker db) — nothing to project.
            pass

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
        active_runtime_sql = _active_runtime_run_sql("runs.run_id")

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            self._repair_control_only_active_runs_locked(conn, session_id=stable, now=now)
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
                  AND {active_runtime_sql}
                ORDER BY updated_at DESC, started_at DESC
                LIMIT 1
                """,
                (stable, *sorted(ACTIVE_RUN_STATUSES)),
            ).fetchone()
            if active is not None:
                return {"run": None, "conflict": self._run_from_row(active), "created": False}
            live_control_only_active = self._live_owner_control_only_active_run_locked(
                conn,
                session_id=stable,
            )
            if live_control_only_active is not None:
                return {
                    "run": None,
                    "conflict": self._run_from_row(live_control_only_active),
                    "created": False,
                }

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
                      AND {active_runtime_sql}
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

    def _upsert_session_runtime_state_locked(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        payload: Dict[str, Any],
        runtime_scope_key: str = "",
        runtime_session_id: str = "",
        run_id: str = "",
        turn_id: str = "",
        updated_at: float = 0.0,
        source_seq: int = 0,
    ) -> tuple[Dict[str, Any], bool]:
        record = session_info_record(
            session_id=session_id,
            payload=payload,
            runtime_scope_key=runtime_scope_key,
            runtime_session_id=runtime_session_id,
            run_id=run_id,
            turn_id=turn_id,
            updated_at=updated_at,
            source_seq=source_seq,
        )
        existing = conn.execute(
            "SELECT * FROM session_runtime_state WHERE session_id = ?",
            (record["session_id"],),
        ).fetchone()
        duplicate = session_runtime_identity_matches(existing, record)
        if duplicate:
            existing_state = session_runtime_state_from_row(existing)
            return existing_state or record, True
        conn.execute(
            """
            INSERT INTO session_runtime_state (
                session_id, runtime_scope_key, runtime_session_id, run_id,
                turn_id, status, model, provider, profile_json,
                payload_hash, updated_at, source_seq
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                runtime_scope_key = excluded.runtime_scope_key,
                runtime_session_id = excluded.runtime_session_id,
                run_id = excluded.run_id,
                turn_id = excluded.turn_id,
                status = excluded.status,
                model = excluded.model,
                provider = excluded.provider,
                profile_json = excluded.profile_json,
                payload_hash = excluded.payload_hash,
                updated_at = excluded.updated_at,
                source_seq = excluded.source_seq
            WHERE COALESCE(excluded.source_seq, 0) >= COALESCE(session_runtime_state.source_seq, 0)
            """,
            (
                record["session_id"],
                record["runtime_scope_key"],
                record["runtime_session_id"],
                record["run_id"],
                record["turn_id"],
                record["status"],
                record["model"],
                record["provider"],
                record["profile_json"],
                record["payload_hash"],
                record["updated_at"],
                record["source_seq"],
            ),
        )
        return record, False

    def get_session_runtime_state(self, session_id: str) -> Dict[str, Any] | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_runtime_state WHERE session_id = ?",
                (stable,),
            ).fetchone()
        state = session_runtime_state_from_row(row)
        return state or None

    def append_run_event(
        self,
        session_id: str,
        event: Dict[str, Any],
        *,
        participant_id: str = "",
        activity_id: str = "",
    ) -> Dict[str, Any]:
        stable = str(session_id or "").strip()
        if not stable:
            return {}
        frame = dict(event or {})
        frame["stored_session_id"] = str(frame.get("stored_session_id") or stable)
        event_type = str(frame.get("type") or "").strip()
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        run_id = _event_run_id(frame)
        turn_id = _event_turn_id(frame)
        if (
            event_type == "message.complete"
            and str(payload.get("status") or "").strip().lower() in {"failed", "error"}
            and primary_deliverable_text(payload)
            and hasattr(self, "get_team_mission_run_binding")
        ):
            try:
                team_mission_binding = self.get_team_mission_run_binding(run_id)  # type: ignore[attr-defined]
            except Exception:
                team_mission_binding = None
            if isinstance(team_mission_binding, dict) and team_mission_binding:
                normalized_payload = dict(payload)
                original_status = str(normalized_payload.get("status") or "").strip()
                original_error = str(normalized_payload.get("message") or normalized_payload.get("error") or "").strip()
                normalized_payload["text"] = primary_deliverable_text(payload)
                normalized_payload["status"] = "complete"
                normalized_payload["team_mission_terminal_status_recovered"] = original_status
                if original_error:
                    normalized_payload["nonfatal_error"] = original_error
                frame["payload"] = normalized_payload
                payload = normalized_payload
        runtime_session_id = str(frame.get("session_id") or "").strip()
        runtime_scope_key = _event_runtime_scope_key(frame, stable)
        frame["runtime_scope_key"] = runtime_scope_key
        timestamp = float(frame.get("timestamp") or time.time())
        requested_seq = int(frame.get("seq") or 0)
        authoritative_next_seq = self.next_run_event_seq(stable)
        # run_events.seq is the authoritative timeline sequence for a stored
        # conversation. Runtime frames may carry source-side seq values, but
        # they must never collide with or move behind the persisted run_events
        # domain for this stored_session_id.
        seq = requested_seq if requested_seq >= authoritative_next_seq else authoritative_next_seq
        assert seq >= authoritative_next_seq
        frame["seq"] = seq
        terminal_status = _event_status(event_type, payload)
        owner_metadata = frame.get("owner_metadata")
        owner_metadata = owner_metadata if isinstance(owner_metadata, dict) else {}
        frame["timestamp"] = timestamp
        event_participant_id = _event_participant_id(frame, participant_id)
        if event_participant_id:
            frame["participant_id"] = event_participant_id
            frame["participantId"] = event_participant_id
            if isinstance(frame.get("payload"), dict):
                frame["payload"]["participant_id"] = event_participant_id
                payload = frame["payload"]
        # ADR-0001: stamp the frame's activity_id at write time so all
        # downstream readers (subscribe/replay/projector) can slice by
        # activity without scanning by session_id.
        event_activity_id = _event_activity_id(frame, activity_id)
        if event_activity_id:
            frame["activity_id"] = event_activity_id
            frame["activityId"] = event_activity_id
            if isinstance(frame.get("payload"), dict):
                frame["payload"]["activity_id"] = event_activity_id
                payload = frame["payload"]
        event_json = _json_dumps(frame)
        frame_blob, frame_format = encode_run_event_frame(frame)
        retention_class = _run_event_retention_class(event_type)
        runtime_source_seq = runtime_source_seq_from_event(frame)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            inserted_event = frame
            event_json_for_insert = event_json
            frame_blob_for_insert = frame_blob
            frame_format_for_insert = frame_format
            coalesced = False
            existing = None
            existing_status = ""
            ignored_after_terminal = False
            if event_type == "session.info":
                runtime_state, duplicate_session_info = self._upsert_session_runtime_state_locked(
                    conn,
                    session_id=stable,
                    payload=payload,
                    runtime_scope_key=runtime_scope_key,
                    runtime_session_id=runtime_session_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    updated_at=timestamp,
                    source_seq=seq,
                )
                if duplicate_session_info:
                    duplicate_event = dict(frame)
                    duplicate_event["seq"] = int(runtime_state.get("source_seq") or seq)
                    duplicate_event["_persistence_disposition"] = "duplicate_session_info"
                    duplicate_event["_session_runtime_state"] = runtime_state
                    return duplicate_event
            if run_id:
                existing = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                existing_status = str(_row_value(existing, "status", "") or "")
                if existing_status in TERMINAL_RUN_STATUSES and terminal_status in TERMINAL_RUN_STATUSES:
                    preferred_status = _prefer_terminal_run_status(existing_status, terminal_status)
                    if preferred_status == existing_status:
                        canonical = conn.execute(
                            """
                            SELECT *
                            FROM run_events
                            WHERE session_id = ?
                              AND run_id = ?
                              AND status = ?
                            ORDER BY seq DESC, id DESC
                            LIMIT 1
                            """,
                            (stable, run_id, existing_status),
                        ).fetchone()
                        canonical_event = decode_run_event_row(canonical)
                        if isinstance(canonical_event, dict) and canonical_event:
                            canonical_event["_persistence_disposition"] = "duplicate_terminal"
                            return canonical_event
                        canonical_payload = dict(payload)
                        canonical_payload["status"] = "complete" if existing_status == "completed" else existing_status
                        duplicate_event = {
                            **frame,
                            "payload": canonical_payload,
                            "status": existing_status,
                            "seq": int(_row_value(existing, "last_seq", seq) or seq),
                        }
                        duplicate_event["_persistence_disposition"] = "duplicate_terminal"
                        return duplicate_event
                ignored_after_terminal = bool(
                    existing_status in TERMINAL_RUN_STATUSES
                    and terminal_status is None
                    and _event_reopens_terminal_run(event_type)
                )
                if ignored_after_terminal:
                    frame["_persistence_disposition"] = "ignored_after_terminal"
                    event_json_for_insert = _json_dumps(frame)
                    frame_blob_for_insert, frame_format_for_insert = encode_run_event_frame(frame)
            if _event_is_coalescible_stream_delta(frame):
                boundary_event_types = _stream_compaction_boundary_event_types(event_type)
                boundary_placeholders = ",".join("?" for _ in boundary_event_types)
                boundary = conn.execute(
                    f"""
                    SELECT COALESCE(MAX(seq), 0) AS boundary_seq
                    FROM run_events
                    WHERE session_id = ?
                      AND (? = '' OR run_id = ?)
                      AND (? = '' OR turn_id = ?)
                      AND event_type IN ({boundary_placeholders})
                    """,
                    (
                        stable,
                        run_id,
                        run_id,
                        turn_id,
                        turn_id,
                        *sorted(boundary_event_types),
                    ),
                ).fetchone()
                boundary_seq = int(_row_value(boundary, "boundary_seq", 0) or 0)
                candidates = conn.execute(
                    """
                    SELECT *
                    FROM run_events
                    WHERE session_id = ?
                      AND event_type = ?
                      AND (? = '' OR run_id = ?)
                      AND (? = '' OR turn_id = ?)
                      AND COALESCE(runtime_scope_key, '') = ?
                      AND seq > ?
                    ORDER BY seq DESC
                    LIMIT 128
                    """,
                    (
                        stable,
                        event_type,
                        run_id,
                        run_id,
                        turn_id,
                        turn_id,
                        runtime_scope_key,
                        boundary_seq,
                    ),
                ).fetchall()
                previous = None
                previous_event: Dict[str, Any] = {}
                for candidate in candidates:
                    candidate_event = decode_run_event_row(candidate)
                    if (
                        isinstance(candidate_event, dict)
                        and _stream_events_can_coalesce(candidate_event, frame)
                    ):
                        previous = candidate
                        previous_event = candidate_event
                        break
                if previous is not None:
                    target_seq = conn.execute(
                        """
                        SELECT id
                        FROM run_events
                        WHERE session_id = ?
                          AND seq = ?
                          AND id != ?
                        LIMIT 1
                        """,
                        (stable, seq, previous["id"]),
                    ).fetchone()
                    if target_seq is None:
                        merged_payload = _merge_stream_payload(previous_event, frame)
                        merged_event = {
                            **previous_event,
                            "session_id": runtime_session_id or previous_event.get("session_id") or "",
                            "stored_session_id": stable,
                            "run_id": run_id or previous_event.get("run_id") or "",
                            "turn_id": turn_id or previous_event.get("turn_id") or "",
                            "runtime_session_id": runtime_session_id or previous_event.get("runtime_session_id") or "",
                            "runtime_scope_key": runtime_scope_key,
                            "participant_id": event_participant_id or _event_participant_id(previous_event),
                            "participantId": event_participant_id or _event_participant_id(previous_event),
                            "activity_id": event_activity_id or _event_activity_id(previous_event),
                            "activityId": event_activity_id or _event_activity_id(previous_event),
                            "seq": seq,
                            "timestamp": timestamp,
                            "payload": merged_payload,
                        }
                        merged_participant_id = _event_participant_id(merged_event)
                        if merged_participant_id and isinstance(merged_event.get("payload"), dict):
                            merged_event["payload"]["participant_id"] = merged_participant_id
                        merged_activity_id = _event_activity_id(merged_event)
                        if merged_activity_id and isinstance(merged_event.get("payload"), dict):
                            merged_event["payload"]["activity_id"] = merged_activity_id
                        merged_runtime_source_seq = runtime_source_seq_from_event(merged_event)
                        merged_frame_blob, merged_frame_format = encode_run_event_frame(merged_event)
                        conn.execute(
                            """
                            UPDATE run_events
                            SET run_id = ?,
                                turn_id = ?,
                                runtime_session_id = ?,
                                runtime_scope_key = ?,
                                participant_id = ?,
                                activity_id = ?,
                                seq = ?,
                                timestamp = ?,
                                payload_json = ?,
                                event_json = ?,
                                status = ?,
                                frame_blob = ?,
                                frame_format = ?,
                                retention_class = ?,
                                projection_state = COALESCE(NULLIF(projection_state, ''), 'raw'),
                                runtime_source_seq = ?
                            WHERE id = ?
                            """,
                            (
                                run_id,
                                turn_id,
                                runtime_session_id,
                                runtime_scope_key,
                                merged_participant_id,
                                merged_activity_id or None,
                                seq,
                                timestamp,
                                _json_dumps(merged_payload),
                                _json_dumps(merged_event),
                                terminal_status or "",
                                merged_frame_blob,
                                merged_frame_format,
                                retention_class,
                                merged_runtime_source_seq,
                                previous["id"],
                            ),
                        )
                        inserted_event = merged_event
                        coalesced = True
            if not coalesced:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO run_events (
                        session_id, run_id, turn_id, runtime_session_id, runtime_scope_key,
                        participant_id, activity_id, event_type,
                        seq, timestamp, payload_json, event_json, status,
                        frame_blob, frame_format, retention_class, projection_state,
                        runtime_source_seq
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable,
                        run_id,
                        turn_id,
                        runtime_session_id,
                        runtime_scope_key,
                        event_participant_id,
                        event_activity_id or None,
                        event_type,
                        seq,
                        timestamp,
                        _json_dumps(payload),
                        event_json_for_insert,
                        "ignored_after_terminal" if ignored_after_terminal else terminal_status or "",
                        frame_blob_for_insert,
                        frame_format_for_insert,
                        retention_class,
                        "raw",
                        runtime_source_seq,
                    ),
                )
            inserted_row = conn.execute(
                """
                SELECT *
                FROM run_events
                WHERE session_id = ?
                  AND seq = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (stable, int(inserted_event.get("seq") or seq)),
            ).fetchone()
            if inserted_row is not None:
                try:
                    project_run_event_search_index_from_row(conn, inserted_row)
                except Exception as exc:
                    logger.debug(
                        "run event search index projection skipped for %s/%s/%s: %s",
                        stable,
                        run_id,
                        seq,
                        exc,
                    )
            if event_type in TOOL_EVENT_TYPES and not ignored_after_terminal:
                try:
                    projected_tool_event = project_tool_event(conn, inserted_event)
                    if isinstance(projected_tool_event, dict) and projected_tool_event.get("id"):
                        inserted_event = dict(inserted_event)
                        inserted_event["_projected_tool_event_id"] = projected_tool_event.get("id")
                        if inserted_row is not None:
                            conn.execute(
                                """
                                UPDATE run_events
                                SET projected_tool_event_id = ?,
                                    projection_state = COALESCE(NULLIF(projection_state, ''), 'raw')
                                WHERE id = ?
                                """,
                                (
                                    str(projected_tool_event.get("id") or ""),
                                    int(inserted_row["id"]),
                                ),
                            )
                except Exception as exc:
                    logger.debug(
                        "tool event projection skipped for %s/%s/%s: %s",
                        stable,
                        run_id,
                        seq,
                        exc,
                    )
            if event_type == "message.complete" and not ignored_after_terminal and inserted_row is not None:
                try:
                    from hermes_team_mission.runtime.team_transcript_writer import RuntimeTranscriptWriter

                    projected_message = RuntimeTranscriptWriter.project_message_complete_event_locked(
                        self,
                        conn,
                        session_id=stable,
                        event=inserted_event,
                    )
                    conversation_message_id = ""
                    if isinstance(projected_message, dict):
                        conversation_message_id = str(
                            projected_message.get("conversation_message_id")
                            or projected_message.get("conversationMessageId")
                            or ""
                        ).strip()
                    if conversation_message_id:
                        inserted_event = dict(inserted_event)
                        inserted_event["_projected_message_id"] = conversation_message_id
                        if isinstance(projected_message, dict) and isinstance(projected_message.get("_team_mission_report_ready"), dict):
                            inserted_event["_team_mission_report_ready"] = dict(projected_message["_team_mission_report_ready"])
                        conn.execute(
                            """
                            UPDATE run_events
                            SET projected_message_id = ?,
                                projection_state = 'projected'
                            WHERE id = ?
                            """,
                            (
                                conversation_message_id,
                                int(inserted_row["id"]),
                            ),
                        )
                except Exception as exc:
                    logger.debug(
                        "team transcript message projection skipped for %s/%s/%s: %s",
                        stable,
                        run_id,
                        seq,
                        exc,
                    )
            if event_type == "artifact.created" and not ignored_after_terminal and inserted_row is not None:
                try:
                    from hermes_team_mission.runtime.team_transcript_writer import RuntimeTranscriptWriter

                    RuntimeTranscriptWriter.merge_artifact_event_into_projected_message_locked(
                        self,
                        conn,
                        session_id=stable,
                        event=inserted_event,
                    )
                except Exception as exc:
                    logger.debug(
                        "team transcript artifact merge skipped for %s/%s/%s: %s",
                        stable,
                        run_id,
                        seq,
                        exc,
                    )
            if not ignored_after_terminal:
                projector = getattr(self, "_project_timeline_block_event_locked", None)
                if callable(projector):
                    try:
                        projector(conn, session_id=stable, event=inserted_event)
                    except Exception as exc:
                        logger.debug(
                            "timeline block projection skipped for %s/%s/%s: %s",
                            stable,
                            run_id,
                            seq,
                            exc,
                        )
            if run_id:
                if ignored_after_terminal:
                    return inserted_event
                should_track_run = bool(
                    existing is not None
                    or terminal_status
                    or _event_opens_active_run(event_type)
                )
                if not should_track_run:
                    return inserted_event
                next_status = terminal_status or existing_status or "running"
                if existing_status in TERMINAL_RUN_STATUSES and terminal_status is None:
                    next_status = existing_status
                elif existing_status in TERMINAL_RUN_STATUSES and terminal_status in TERMINAL_RUN_STATUSES:
                    next_status = _prefer_terminal_run_status(existing_status, terminal_status)
                if (
                    _event_opens_active_run(event_type)
                    and existing_status not in TERMINAL_RUN_STATUSES
                ):
                    next_status = "running"
                rejected_duplicate_active = ""
                if existing is None and next_status in ACTIVE_RUN_STATUSES:
                    self._repair_control_only_active_runs_locked(conn, session_id=stable, now=timestamp)
                    active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
                    active_runtime_sql = _active_runtime_run_sql("runs.run_id")
                    active = conn.execute(
                        f"""
                        SELECT run_id
                        FROM runs
                        WHERE session_id = ?
                          AND run_id != ?
                          AND status IN ({active_statuses})
                          AND {active_runtime_sql}
                        ORDER BY updated_at DESC, started_at DESC
                        LIMIT 1
                        """,
                        (stable, run_id),
                    ).fetchone()
                    if active is None:
                        active = self._live_owner_control_only_active_run_locked(
                            conn,
                            session_id=stable,
                            exclude_run_id=run_id,
                        )
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
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.update(owner_metadata)
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
                            _json_dumps(metadata),
                        ),
                    )
                else:
                    if completed_at is None:
                        completed_at = existing["completed_at"]
                    if next_status == "failed":
                        next_error = str(payload.get("message") or "") or str(existing["error"] or "")
                    elif next_status in TERMINAL_RUN_STATUSES:
                        next_error = ""
                    else:
                        next_error = str(payload.get("message") or "") or str(existing["error"] or "")
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
                            error = ?,
                            metadata_json = ?
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
                            next_error,
                            _json_dumps(metadata),
                            run_id,
                        ),
                    )
                # Mirror the run's terminal status into session_index so the
                # sidebar stops showing this session as `running` after the
                # streaming finish lands. The non-streaming write path
                # (`upsert_run`) already invokes this projection at line 609;
                # the streaming `append_run_event` path was missing it, so
                # session_index kept `running=1, status='running'` forever
                # after a `message.complete` / `error` / `tool.complete`
                # terminal event closed the run.
                if run_id:
                    self._project_run_state_to_session_index_locked(
                        conn,
                        session_id=stable,
                        run_id=run_id,
                        runtime_scope_key=runtime_scope_key,
                        runtime_session_id=runtime_session_id,
                        status=next_status,
                        updated_at=timestamp,
                    )
            return inserted_event

        saved = self._execute_write(_do)
        ignored_terminal_stream_event = bool(
            isinstance(saved, dict)
            and saved.get("_persistence_disposition") == "ignored_after_terminal"
            and event_type in TERMINAL_RUN_PRUNABLE_EVENT_TYPES
            and run_id
        )
        defer_terminal_maintenance = (
            terminal_status in TERMINAL_RUN_STATUSES
            and bool(getattr(self, "_team_mission_projecting", False))
        )
        # Write-time canonical projection for team-mission-bound runs. Runtime
        # events recorded straight onto a node's session (the streaming path
        # that bypasses append_team_mission_run_event) are projected into the
        # canonical team_mission_events log here, so replay and live share one
        # monotonic seq domain (replaces the removed read-time run_events
        # fallback). The _team_mission_projecting guard prevents the explicit
        # append_team_mission_run_event path and the conversation mirror from
        # re-entering this hook.
        if (
            run_id
            and not getattr(self, "_team_mission_projecting", False)
            and not getattr(self, "_member_chat_projecting", False)
        ):
            if hasattr(self, "_project_team_mission_run_event"):
                self._project_team_mission_run_event(run_id=run_id, saved=saved)
            report_ready = saved.get("_team_mission_report_ready") if isinstance(saved, dict) else None
            if not isinstance(report_ready, dict) and event_type == "message.complete":
                try:
                    from hermes_team_mission.runtime.team_transcript_writer import leader_report_ready_context_for_run

                    report_ready = leader_report_ready_context_for_run(
                        self,
                        run_id=run_id,
                        projected_message_id=str((saved or {}).get("_projected_message_id") or "") if isinstance(saved, dict) else "",
                    )
                except Exception as exc:
                    logger.debug("team mission report-ready context lookup skipped for %s/%s: %s", stable, run_id, exc)
            if isinstance(report_ready, dict):
                try:
                    from hermes_team_mission.runtime.team_transcript_writer import _append_leader_report_ready_event

                    _append_leader_report_ready_event(
                        self,
                        mission_id=str(report_ready.get("mission_id") or report_ready.get("missionId") or ""),
                        run_id=str(report_ready.get("run_id") or report_ready.get("runId") or run_id or ""),
                        conversation_message_id=str(
                            report_ready.get("leader_report_message_id")
                            or report_ready.get("leaderReportMessageId")
                            or ""
                        ),
                    )
                except Exception as exc:
                    logger.debug("team mission report-ready projection skipped for %s/%s: %s", stable, run_id, exc)
            # Decoupled group-chat (member-chat) mirroring is performed at the
            # record_event layer instead — that layer can both broadcast the
            # mirrored frame to the conversation's live subscribers AND
            # persist it. Mirroring from the db hook can only persist; the
            # frontend would never see the stream.
        if ignored_terminal_stream_event:
            try:
                self.prune_terminal_run_stream_events(session_id=stable, run_id=run_id)
            except Exception as exc:
                logger.debug("ignored terminal stream pruning skipped for %s/%s: %s", stable, run_id, exc)
        if not defer_terminal_maintenance:
            self._maintain_run_events_after_append(
                session_id=stable,
                run_id=run_id,
                seq=seq,
                terminal_status=terminal_status,
            )
        return saved

    def _maintain_run_events_after_append(
        self,
        *,
        session_id: str,
        run_id: str = "",
        seq: int = 0,
        terminal_status: str | None = None,
        prune_terminal_stream_events: bool = True,
    ) -> None:
        should_prune = (
            (seq > 0 and seq % RUN_EVENT_PRUNE_INTERVAL_EVENTS == 0)
            or terminal_status in TERMINAL_RUN_STATUSES
        )
        if not should_prune:
            return
        if terminal_status in TERMINAL_RUN_STATUSES:
            if prune_terminal_stream_events and run_id:
                try:
                    self.prune_terminal_run_stream_events(session_id=session_id, run_id=run_id)
                except Exception as exc:
                    logger.debug("terminal run stream pruning skipped for %s/%s: %s", session_id, run_id, exc)
            try:
                self.compact_run_events(session_id=session_id)
            except Exception as exc:
                logger.debug("run event compaction skipped for %s: %s", session_id, exc)
        try:
            self.prune_run_events(session_id=session_id)
        except Exception as exc:
            logger.debug("run event retention skipped for %s: %s", session_id, exc)

    def list_run_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        active_only: bool = False,
        runtime_scope_key: str = "",
        run_id: str = "",
        activity_id: str = "",
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
        run_clause = ""
        normalized_run_id = str(run_id or "").strip()
        if normalized_run_id:
            run_clause = "AND run_id = ?"
            params.append(normalized_run_id)
        # ADR-0001: optional activity_id filter — when supplied, narrow
        # the result to a single Activity. Backwards compatible because
        # the clause is empty when activity_id is "".
        activity_clause = ""
        normalized_activity_id = str(activity_id or "").strip()
        if normalized_activity_id:
            activity_clause = "AND activity_id = ?"
            params.append(normalized_activity_id)
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
                SELECT *
                FROM run_events
                WHERE session_id = ?
                  AND seq > ?
                  {scope_clause}
                  {run_clause}
                  {activity_clause}
                  {active_clause}
                ORDER BY seq ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        events = []
        with self._lock:
            for row in rows:
                event = decode_run_event_row(row)
                if isinstance(event, dict):
                    event = rehydrate_referenced_run_event(self._conn, row, event)
                    event = _event_with_participant_id(
                        event,
                        str(_row_value(row, "participant_id", "") or ""),
                    )
                    events.append(event)
        return events

    def list_run_events_by_activity(
        self,
        activity_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        """Return all run_events for an activity_id across sessions, ordered by seq."""
        normalized_activity_id = str(activity_id or "").strip()
        if not normalized_activity_id:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 5000))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM run_events
                WHERE activity_id = ?
                  AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (normalized_activity_id, int(after_seq or 0), bounded_limit),
            ).fetchall()
        events = []
        with self._lock:
            for row in rows:
                event = decode_run_event_row(row)
                if isinstance(event, dict):
                    event = rehydrate_referenced_run_event(self._conn, row, event)
                    event = _event_with_participant_id(
                        event,
                        str(_row_value(row, "participant_id", "") or ""),
                    )
                    event["activity_id"] = normalized_activity_id
                    event["activityId"] = normalized_activity_id
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        payload = dict(payload)
                        payload.setdefault("activity_id", normalized_activity_id)
                        event["payload"] = payload
                    events.append(event)
        return events

    def list_tool_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        run_id: str = "",
        direction: str = "after",
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        stable = str(session_id or "").strip()
        if not stable:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 5000))
        normalized_direction = str(direction or "after").strip().lower()
        params: list[Any] = [stable, int(after_seq or 0)]
        run_clause = ""
        normalized_run_id = str(run_id or "").strip()
        if normalized_run_id:
            run_clause = "AND COALESCE(run_id, '') = ?"
            params.append(normalized_run_id)
        params.append(bounded_limit)
        order_expr = "COALESCE(seq_start, seq_last, id)"
        if normalized_direction == "tail":
            query = f"""
                SELECT *
                FROM (
                    SELECT *
                    FROM tool_events
                    WHERE session_id = ?
                      AND COALESCE(seq_last, seq_start, 0) > ?
                      {run_clause}
                    ORDER BY {order_expr} DESC, id DESC
                    LIMIT ?
                )
                ORDER BY {order_expr} ASC, id ASC
                """
        else:
            query = f"""
                SELECT *
                FROM tool_events
                WHERE session_id = ?
                  AND COALESCE(seq_last, seq_start, 0) > ?
                  {run_clause}
                ORDER BY {order_expr} ASC, id ASC
                LIMIT ?
                """
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [
            item
            for row in rows
            if (item := tool_event_row_to_dict(row))
        ]

    def backfill_run_event_frame_blobs(
        self,
        *,
        session_id: str = "",
        limit: int = 5000,
    ) -> Dict[str, Any]:
        stable_filter = str(session_id or "").strip()
        bounded_limit = max(1, min(int(limit or 5000), 20000))

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            params: list[Any] = []
            session_clause = ""
            if stable_filter:
                session_clause = "AND session_id = ?"
                params.append(stable_filter)
            rows = conn.execute(
                f"""
                SELECT *
                FROM run_events
                WHERE (
                    frame_blob IS NULL
                    OR COALESCE(frame_format, '') = ''
                    OR COALESCE(retention_class, '') = ''
                    OR NOT EXISTS (
                        SELECT 1
                        FROM run_event_search_index idx
                        WHERE idx.run_event_id = run_events.id
                    )
                )
                  {session_clause}
                ORDER BY session_id ASC, seq ASC, id ASC
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()
            for row in rows:
                event = decode_run_event_row(row)
                runtime_source_seq = runtime_source_seq_from_event(event)
                update_run_event_frame_columns(
                    conn,
                    row_id=int(row["id"]),
                    event=event,
                    retention_class=_run_event_retention_class(str(row["event_type"] or event.get("type") or "")),
                    projection_state="raw",
                )
                conn.execute(
                    "UPDATE run_events SET runtime_source_seq = ? WHERE id = ?",
                    (runtime_source_seq, int(row["id"])),
                )
                project_run_event_search_index(
                    conn,
                    row_id=int(row["id"]),
                    session_id=str(row["session_id"] or event.get("stored_session_id") or ""),
                    seq=int(row["seq"] or event.get("seq") or 0),
                    event_type=str(row["event_type"] or event.get("type") or ""),
                    runtime_scope_key=str(row["runtime_scope_key"] or event.get("runtime_scope_key") or ""),
                    runtime_source_seq=runtime_source_seq,
                    event=event,
                    updated_at=float(row["timestamp"] or event.get("timestamp") or 0),
                )
            remaining = conn.execute(
                f"""
                SELECT COUNT(1) AS count
                FROM run_events
                WHERE (
                    frame_blob IS NULL
                    OR COALESCE(frame_format, '') = ''
                    OR COALESCE(retention_class, '') = ''
                    OR NOT EXISTS (
                        SELECT 1
                        FROM run_event_search_index idx
                        WHERE idx.run_event_id = run_events.id
                    )
                )
                  {session_clause}
                """,
                tuple(params),
            ).fetchone()
            return {
                "updated_events": len(rows),
                "remaining_events": int(_row_value(remaining, "count", 0) or 0),
                "limit": bounded_limit,
            }

        return self._execute_write(_do)

    def reference_run_event_payloads(
        self,
        *,
        session_id: str = "",
        limit: int = 1000,
    ) -> Dict[str, Any]:
        stable_filter = str(session_id or "").strip()
        bounded_limit = max(1, min(int(limit or 1000), 20000))

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            return reference_projected_run_event_payloads(
                conn,
                session_id=stable_filter,
                limit=bounded_limit,
            )

        return self._execute_write(_do)

    def has_run_event_source(
        self,
        session_id: str,
        *,
        run_id: str = "",
        runtime_session_id: str = "",
        event_type: str = "",
        runtime_source_seq: int = 0,
    ) -> bool:
        stable = str(session_id or "").strip()
        try:
            source_seq = int(runtime_source_seq or 0)
        except (TypeError, ValueError):
            source_seq = 0
        if not stable or source_seq <= 0:
            return False
        clauses = [
            "session_id = ?",
            "runtime_source_seq = ?",
        ]
        params: list[Any] = [stable, source_seq]
        normalized_run_id = str(run_id or "").strip()
        if normalized_run_id:
            clauses.append("run_id = ?")
            params.append(normalized_run_id)
        normalized_runtime_session_id = str(runtime_session_id or "").strip()
        if normalized_runtime_session_id:
            clauses.append("runtime_session_id = ?")
            params.append(normalized_runtime_session_id)
        normalized_type = str(event_type or "").strip()
        if normalized_type:
            clauses.append("event_type = ?")
            params.append(normalized_type)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT 1
                FROM run_events
                WHERE {" AND ".join(clauses)}
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return row is not None

    def has_run_event_frame(
        self,
        session_id: str,
        *,
        seq: int = 0,
        run_id: str = "",
        runtime_session_id: str = "",
        event_type: str = "",
    ) -> bool:
        stable = str(session_id or "").strip()
        try:
            source_seq = int(seq or 0)
        except (TypeError, ValueError):
            source_seq = 0
        if not stable or source_seq <= 0:
            return False
        clauses = ["session_id = ?", "seq = ?"]
        params: list[Any] = [stable, source_seq]
        normalized_run_id = str(run_id or "").strip()
        if normalized_run_id:
            clauses.append("run_id = ?")
            params.append(normalized_run_id)
        normalized_runtime_session_id = str(runtime_session_id or "").strip()
        if normalized_runtime_session_id:
            clauses.append("runtime_session_id = ?")
            params.append(normalized_runtime_session_id)
        normalized_type = str(event_type or "").strip()
        if normalized_type:
            clauses.append("event_type = ?")
            params.append(normalized_type)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT 1
                FROM run_events
                WHERE {" AND ".join(clauses)}
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return row is not None

    def list_run_events_filtered(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        runtime_scope_key: str = "",
        event_type_prefix: str = "",
        event_types: list[str] | tuple[str, ...] | None = None,
        payload_contains: str = "",
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        """Return a persisted event page filtered at the database boundary.

        History views often need a small subset of the durable run event log
        (for example subagent lifecycle metadata) without replaying every token
        delta.  Keep that filtering inside the repository so UI hydration does
        not depend on loading large event pages over the gateway.
        """
        stable = str(session_id or "").strip()
        if not stable:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 20000))
        params: list[Any] = [stable, int(after_seq or 0)]
        clauses: list[str] = [
            "e.session_id = ?",
            "e.seq > ?",
        ]
        scope = str(runtime_scope_key or "").strip()
        if scope:
            clauses.append("COALESCE(e.runtime_scope_key, e.session_id) = ?")
            params.append(scope)
        normalized_types = [
            str(item or "").strip()
            for item in (event_types or ())
            if str(item or "").strip()
        ]
        if normalized_types:
            placeholders = ", ".join("?" for _ in normalized_types)
            clauses.append(f"e.event_type IN ({placeholders})")
            params.extend(normalized_types)
        else:
            prefix = str(event_type_prefix or "").strip()
            if prefix:
                clauses.append("e.event_type LIKE ?")
                params.append(f"{prefix}%")
        contains = str(payload_contains or "").strip()
        from_sql = "run_events e"
        if contains:
            from_sql = (
                "run_events e "
                "JOIN run_event_search_index idx ON idx.run_event_id = e.id"
            )
            clauses.append("instr(idx.search_text, ?) > 0")
            params.append(contains)
        params.append(bounded_limit)
        where_sql = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT e.*
                FROM {from_sql}
                WHERE {where_sql}
                ORDER BY e.seq ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        events = []
        with self._lock:
            for row in rows:
                event = decode_run_event_row(row)
                if isinstance(event, dict):
                    event = rehydrate_referenced_run_event(self._conn, row, event)
                    event = _event_with_participant_id(
                        event,
                        str(_row_value(row, "participant_id", "") or ""),
                    )
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
            if any(status in ACTIVE_RUN_STATUSES for status in normalized_statuses):
                active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
                clauses.append(
                    f"(status NOT IN ({active_statuses}) OR {_active_runtime_run_sql('runs.run_id')})"
                )
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
        stale_after_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS,
        owner_dead_grace_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS,
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
        active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
        instance_id = str(current_gateway_instance_id or "").strip()

        def _row_diagnostic(row: sqlite3.Row, decision: str, should_fail: bool) -> dict[str, Any]:
            metadata = _json_loads(row["metadata_json"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            return {
                "run_id": str(row["run_id"] or ""),
                "session_id": str(row["session_id"] or ""),
                "runtime_scope_key": str(row["runtime_scope_key"] or ""),
                "runtime_session_id": str(row["runtime_session_id"] or ""),
                "status": str(row["status"] or ""),
                "updated_age_seconds": round(now - float(row["updated_at"] or row["started_at"] or 0), 3),
                "owner_pid": metadata.get("gateway_pid"),
                "owner_instance": metadata.get("gateway_instance_id"),
                "current_pid": current_pid,
                "current_gateway_instance_id": instance_id,
                "decision": decision,
                "should_fail": should_fail,
            }

        def _do(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                f"""
                SELECT *
                FROM runs
                WHERE status IN ({active_statuses})
                """
            ).fetchall()
            failed = 0
            diagnostics: list[dict[str, Any]] = []
            for row in rows:
                should_fail, decision = orphaned_active_run_decision(
                    row,
                    now=now,
                    live_runtime_session_ids=live_runtime_session_ids,
                    current_pid=current_pid,
                    current_gateway_instance_id=instance_id,
                    stale_after_seconds=stale_after_seconds,
                    owner_dead_grace_seconds=owner_dead_grace_seconds,
                )
                diagnostics.append(_row_diagnostic(row, decision, should_fail))
                if not should_fail:
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
                terminal_activity_id = _recovery_activity_id_for_run(conn, row)
                if terminal_activity_id:
                    terminal_payload["activity_id"] = terminal_activity_id
                    terminal_payload["activityId"] = terminal_activity_id
                terminal_frame = {
                    "type": "message.complete",
                    "session_id": row["runtime_session_id"] or row["session_id"],
                    "stored_session_id": row["session_id"],
                    "run_id": row["run_id"],
                    "turn_id": row["turn_id"],
                    "runtime_scope_key": row["runtime_scope_key"] or row["session_id"],
                    **({"activity_id": terminal_activity_id, "activityId": terminal_activity_id} if terminal_activity_id else {}),
                    "seq": terminal_seq,
                    "timestamp": now,
                    "payload": terminal_payload,
                }
                terminal_frame_blob, terminal_frame_format = encode_run_event_frame(terminal_frame)
                inserted_terminal = conn.execute(
                    """
                    INSERT INTO run_events (
                        session_id, run_id, turn_id, runtime_session_id, runtime_scope_key, activity_id, event_type,
                        seq, timestamp, payload_json, event_json, status,
                        frame_blob, frame_format, retention_class, projection_state, runtime_source_seq
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["session_id"],
                        row["run_id"],
                        row["turn_id"],
                        row["runtime_session_id"],
                        row["runtime_scope_key"] or row["session_id"],
                        terminal_activity_id or None,
                        "message.complete",
                        terminal_seq,
                        now,
                        _json_dumps(terminal_payload),
                        _json_dumps(terminal_frame),
                        "failed",
                        terminal_frame_blob,
                        terminal_frame_format,
                        _run_event_retention_class("message.complete"),
                        "raw",
                        runtime_source_seq_from_event(terminal_frame),
                    ),
                )
                project_run_event_search_index(
                    conn,
                    row_id=int(inserted_terminal.lastrowid or 0),
                    session_id=str(row["session_id"] or ""),
                    seq=terminal_seq,
                    event_type="message.complete",
                    runtime_scope_key=str(row["runtime_scope_key"] or row["session_id"] or ""),
                    runtime_source_seq=runtime_source_seq_from_event(terminal_frame),
                    event=terminal_frame,
                    updated_at=now,
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
            if diagnostics and (failed or logger.isEnabledFor(logging.DEBUG)):
                log_active_scan = logger.warning if failed else logger.debug
                try:
                    log_active_scan(
                        "[dovie-run-recovery] active-run-scan %s",
                        json.dumps(
                            {
                                "db": str(getattr(self, "db_path", "") or ""),
                                "active": len(diagnostics),
                                "failed": failed,
                                "current_pid": current_pid,
                                "current_gateway_instance_id": instance_id,
                                "live_runtime_session_ids": sorted(live_runtime_session_ids),
                                "decisions": diagnostics,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ),
                    )
                except Exception:
                    log_active_scan(
                        "[dovie-run-recovery] active-run-scan active=%s failed=%s",
                        len(diagnostics),
                        failed,
                    )
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

    def prune_duplicate_session_info_events(
        self,
        *,
        session_id: str = "",
    ) -> Dict[str, Any]:
        stable_filter = str(session_id or "").strip()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            params: list[Any] = []
            session_clause = ""
            if stable_filter:
                session_clause = "AND session_id = ?"
                params.append(stable_filter)
            rows = conn.execute(
                f"""
                SELECT *
                FROM run_events
                WHERE event_type = 'session.info'
                  {session_clause}
                ORDER BY session_id ASC, seq ASC, id ASC
                """,
                tuple(params),
            ).fetchall()
            previous_by_identity: dict[tuple[str, str, str, str, str], tuple[str, sqlite3.Row]] = {}
            rows_to_delete: list[sqlite3.Row] = []
            for row in rows:
                event = decode_run_event_row(row)
                event = event if isinstance(event, dict) else {}
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else None
                if payload is None:
                    payload = payload_from_run_event_row(row)
                payload = payload if isinstance(payload, dict) else {}
                identity = (
                    str(event.get("stored_session_id") or row["session_id"] or ""),
                    str(event.get("runtime_scope_key") or row["runtime_scope_key"] or ""),
                    str(event.get("runtime_session_id") or event.get("session_id") or row["runtime_session_id"] or ""),
                    str(event.get("run_id") or row["run_id"] or ""),
                    str(event.get("turn_id") or row["turn_id"] or ""),
                )
                payload_hash = session_info_payload_hash(payload)
                previous = previous_by_identity.get(identity)
                if previous is not None and previous[0] == payload_hash:
                    rows_to_delete.append(previous[1])
                previous_by_identity[identity] = (payload_hash, row)
            if not rows_to_delete:
                return {"deleted_events": 0}
            self._archive_run_event_rows(conn, rows_to_delete, reason="duplicate_session_info")
            ids = [int(row["id"]) for row in rows_to_delete]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM run_events WHERE id IN ({placeholders})", tuple(chunk))
            return {"deleted_events": len(rows_to_delete)}

        return self._execute_write(_do)

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
            retention_gate = getattr(self, "_run_event_row_can_be_retention_deleted_locked", None)
            if callable(retention_gate):
                rows = [
                    row
                    for row in rows
                    if retention_gate(conn, row)
                ]
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

    def prune_terminal_run_stream_events(
        self,
        *,
        session_id: str,
        run_id: str,
        event_types: set[str] | tuple[str, ...] | list[str] = tuple(sorted(TERMINAL_RUN_PRUNABLE_EVENT_TYPES)),
    ) -> Dict[str, Any]:
        """Delete replay-redundant stream deltas once a run is terminal.

        Live delivery keeps ``message.delta`` rows untouched while a run is
        active.  After a terminal event has persisted, the final assistant text
        is represented by ``message.complete`` and/or the messages table, so the
        token-sized delta rows are no longer part of the durable contract.
        """
        stable = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        normalized_types = tuple(sorted({str(item or "").strip() for item in event_types if str(item or "").strip()}))
        if not stable or not normalized_run_id or not normalized_types:
            return {"deleted_events": 0, "event_types": list(normalized_types)}

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            terminal_run_sql = _terminal_run_storage_predicate("e", "r")
            placeholders = ",".join("?" for _ in normalized_types)
            rows = conn.execute(
                f"""
                SELECT e.*
                FROM run_events e
                LEFT JOIN runs r ON r.run_id = e.run_id
                WHERE e.session_id = ?
                  AND e.run_id = ?
                  AND e.event_type IN ({placeholders})
                  AND {terminal_run_sql}
                ORDER BY e.seq ASC, e.id ASC
                """,
                (stable, normalized_run_id, *normalized_types),
            ).fetchall()
            rows = [
                row
                for row in rows
                if RUN_EVENT_RETENTION_POLICY.can_delete_terminal_stream_row(conn, row)
            ]
            retention_gate = getattr(self, "_run_event_row_can_be_retention_deleted_locked", None)
            if callable(retention_gate):
                rows = [
                    row
                    for row in rows
                    if retention_gate(conn, row)
                ]
            if not rows:
                return {"deleted_events": 0, "event_types": list(normalized_types)}
            self._archive_run_event_rows(conn, rows, reason="terminal_run_stream_events")
            ids = [int(row["id"]) for row in rows]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                id_placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM run_events WHERE id IN ({id_placeholders})", tuple(chunk))
            conn.execute(
                """
                UPDATE runs
                SET last_seq = MAX(COALESCE(last_seq, 0), (
                    SELECT COALESCE(MAX(seq), 0)
                    FROM run_events
                    WHERE run_id = ?
                ))
                WHERE run_id = ?
                """,
                (normalized_run_id, normalized_run_id),
            )
            return {"deleted_events": len(rows), "event_types": list(normalized_types)}

        return self._execute_write(_do)

    def compact_run_events(
        self,
        *,
        session_id: str = "",
        vacuum: bool = False,
    ) -> Dict[str, Any]:
        """Coalesce already-persisted stream deltas without preserving chunk rows.

        ``run_events`` is an operational replay log, not the canonical message
        store.  Token-sized stream rows are useful live but wasteful on disk.
        This maintenance pass keeps the final sequence point for each logical
        stream segment and removes redundant preceding delta rows even when
        tool/progress events were interleaved during the live run.  Terminal
        and message-start events remain hard boundaries.  Set ``vacuum`` only
        during explicit maintenance windows when the caller also wants SQLite
        to release freed pages back to the filesystem.
        """
        stable_filter = str(session_id or "").strip()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            active_statuses = _sql_status_literals(ACTIVE_RUN_STATUSES)
            params: list[Any] = []
            session_clause = ""
            if stable_filter:
                session_clause = "AND e.session_id = ?"
                params.append(stable_filter)

            compacted_segments = 0
            deleted_events = 0
            pruned_terminal_stream_events = 0
            deduplicated_terminal_groups = 0
            updated_events = 0
            pending_by_key: dict[tuple[Any, ...], list[tuple[sqlite3.Row, Dict[str, Any]]]] = {}
            affected_run_ids: set[str] = set()

            def prune_terminal_stream_rows() -> None:
                nonlocal deleted_events, pruned_terminal_stream_events
                prunable_types = tuple(sorted(TERMINAL_RUN_PRUNABLE_EVENT_TYPES))
                if not prunable_types:
                    return
                terminal_run_sql = _terminal_run_storage_predicate("e", "r")
                type_placeholders = ",".join("?" for _ in prunable_types)
                prune_params: list[Any] = [*prunable_types]
                prune_session_clause = ""
                if stable_filter:
                    prune_session_clause = "AND e.session_id = ?"
                    prune_params.append(stable_filter)
                rows_to_delete = conn.execute(
                    f"""
                    SELECT e.*
                    FROM run_events e
                    LEFT JOIN runs r ON r.run_id = e.run_id
                    WHERE e.event_type IN ({type_placeholders})
                      AND {terminal_run_sql}
                      {prune_session_clause}
                    ORDER BY e.session_id ASC, e.seq ASC, e.id ASC
                    """,
                    tuple(prune_params),
                ).fetchall()
                rows_to_delete = [
                    row
                    for row in rows_to_delete
                    if RUN_EVENT_RETENTION_POLICY.can_delete_terminal_stream_row(conn, row)
                ]
                retention_gate = getattr(self, "_run_event_row_can_be_retention_deleted_locked", None)
                if callable(retention_gate):
                    rows_to_delete = [
                        row
                        for row in rows_to_delete
                        if retention_gate(conn, row)
                    ]
                if not rows_to_delete:
                    return
                self._archive_run_event_rows(conn, rows_to_delete, reason="terminal_run_stream_events")
                ids = [int(row["id"]) for row in rows_to_delete]
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    id_placeholders = ",".join("?" for _ in chunk)
                    conn.execute(f"DELETE FROM run_events WHERE id IN ({id_placeholders})", tuple(chunk))
                for row in rows_to_delete:
                    normalized_run_id = str(row["run_id"] or "").strip()
                    if normalized_run_id:
                        affected_run_ids.add(normalized_run_id)
                pruned_terminal_stream_events += len(rows_to_delete)
                deleted_events += len(rows_to_delete)

            def compact_terminal_duplicates() -> None:
                nonlocal deduplicated_terminal_groups, deleted_events, updated_events
                terminal_types = ("message.complete", "error", "session.interrupted")
                terminal_statuses = tuple(sorted(TERMINAL_RUN_STATUSES))
                terminal_type_literals = ",".join("?" for _ in terminal_types)
                terminal_status_literals = ",".join("?" for _ in terminal_statuses)
                group_params: list[Any] = [*terminal_types, *terminal_statuses]
                group_session_clause = ""
                if stable_filter:
                    group_session_clause = "AND e.session_id = ?"
                    group_params.append(stable_filter)
                groups = conn.execute(
                    f"""
                    SELECT e.session_id,
                           COALESCE(e.run_id, '') AS run_id,
                           COALESCE(e.turn_id, '') AS turn_id,
                           e.event_type,
                           COALESCE(e.status, '') AS status,
                           COUNT(*) AS event_count
                    FROM run_events e
                    LEFT JOIN runs r ON r.run_id = e.run_id
                    WHERE e.event_type IN ({terminal_type_literals})
                      AND COALESCE(e.status, '') IN ({terminal_status_literals})
                      AND COALESCE(r.status, '') NOT IN ({active_statuses})
                      {group_session_clause}
                    GROUP BY e.session_id,
                             COALESCE(e.run_id, ''),
                             COALESCE(e.turn_id, ''),
                             e.event_type,
                             COALESCE(e.status, '')
                    HAVING COUNT(*) > 1
                    """,
                    tuple(group_params),
                ).fetchall()
                for group in groups:
                    rows_for_group = conn.execute(
                        """
                        SELECT *
                        FROM run_events
                        WHERE session_id = ?
                          AND COALESCE(run_id, '') = ?
                          AND COALESCE(turn_id, '') = ?
                          AND event_type = ?
                          AND COALESCE(status, '') = ?
                        ORDER BY seq ASC, id ASC
                        """,
                        (
                            group["session_id"],
                            group["run_id"],
                            group["turn_id"],
                            group["event_type"],
                            group["status"],
                        ),
                    ).fetchall()
                    if len(rows_for_group) <= 1:
                        continue
                    retention_gate = getattr(self, "_run_event_row_can_be_retention_deleted_locked", None)
                    if callable(retention_gate) and not all(retention_gate(conn, row) for row in rows_for_group[:-1]):
                        continue
                    keep_row = rows_for_group[-1]
                    canonical_seq = int(rows_for_group[0]["seq"] or keep_row["seq"] or 0)
                    keep_event = decode_run_event_row(keep_row)
                    if not isinstance(keep_event, dict):
                        keep_event = {}
                    keep_event = {**keep_event, "seq": canonical_seq}
                    keep_participant_id = _event_participant_id(keep_event)
                    keep_payload = keep_event.get("payload") if isinstance(keep_event.get("payload"), dict) else {}
                    keep_frame_blob, keep_frame_format = encode_run_event_frame(keep_event)
                    keep_runtime_source_seq = runtime_source_seq_from_event(keep_event)
                    delete_ids = [int(row["id"]) for row in rows_for_group if int(row["id"]) != int(keep_row["id"])]
                    for start in range(0, len(delete_ids), 500):
                        chunk = delete_ids[start:start + 500]
                        placeholders = ",".join("?" for _ in chunk)
                        conn.execute(f"DELETE FROM run_events WHERE id IN ({placeholders})", tuple(chunk))
                    conn.execute(
                        """
                        UPDATE run_events
                        SET seq = ?,
                            participant_id = ?,
                            payload_json = ?,
                            event_json = ?,
                            frame_blob = ?,
                            frame_format = ?,
                            retention_class = COALESCE(NULLIF(retention_class, ''), ?),
                            projection_state = COALESCE(NULLIF(projection_state, ''), 'raw'),
                            runtime_source_seq = ?
                        WHERE id = ?
                        """,
                        (
                            canonical_seq,
                            keep_participant_id,
                            _json_dumps(keep_payload),
                            _json_dumps(keep_event),
                            keep_frame_blob,
                            keep_frame_format,
                            _run_event_retention_class(str(group["event_type"] or keep_event.get("type") or "")),
                            keep_runtime_source_seq,
                            int(keep_row["id"]),
                        ),
                    )
                    project_run_event_search_index(
                        conn,
                        row_id=int(keep_row["id"]),
                        session_id=str(group["session_id"] or keep_event.get("stored_session_id") or ""),
                        seq=canonical_seq,
                        event_type=str(group["event_type"] or keep_event.get("type") or ""),
                        runtime_scope_key=str(
                            keep_row["runtime_scope_key"] or keep_event.get("runtime_scope_key") or ""
                        ),
                        runtime_source_seq=keep_runtime_source_seq,
                        event=keep_event,
                        updated_at=float(keep_row["timestamp"] or keep_event.get("timestamp") or 0),
                    )
                    normalized_run_id = str(group["run_id"] or "").strip()
                    if normalized_run_id:
                        affected_run_ids.add(normalized_run_id)
                    deduplicated_terminal_groups += 1
                    deleted_events += len(delete_ids)
                    updated_events += 1

            def rows_for_compaction() -> list[sqlite3.Row]:
                return conn.execute(
                    f"""
                    SELECT e.*
                    FROM run_events e
                    LEFT JOIN runs r ON r.run_id = e.run_id
                    WHERE COALESCE(r.status, '') NOT IN ({active_statuses})
                      {session_clause}
                    ORDER BY e.session_id ASC, e.seq ASC, e.id ASC
                    """,
                    tuple(params),
                ).fetchall()

            def flush_pending() -> None:
                nonlocal compacted_segments, deleted_events, updated_events
                for key in list(pending_by_key.keys()):
                    flush_pending_key(key)

            def flush_pending_key(key: tuple[Any, ...]) -> None:
                nonlocal compacted_segments, deleted_events, updated_events
                pending = pending_by_key.pop(key, [])
                if len(pending) <= 1:
                    return
                retention_gate = getattr(self, "_run_event_row_can_be_retention_deleted_locked", None)
                if callable(retention_gate) and not all(retention_gate(conn, row) for row, _ in pending[:-1]):
                    return
                merged_event = pending[0][1]
                for _, event in pending[1:]:
                    merged_payload = _merge_stream_payload(merged_event, event)
                    merged_event = {
                        **merged_event,
                        "session_id": event.get("session_id") or merged_event.get("session_id") or "",
                        "stored_session_id": event.get("stored_session_id") or merged_event.get("stored_session_id") or "",
                        "run_id": _event_run_id(event) or _event_run_id(merged_event),
                        "turn_id": _event_turn_id(event) or _event_turn_id(merged_event),
                        "runtime_session_id": event.get("runtime_session_id") or merged_event.get("runtime_session_id") or "",
                        "runtime_scope_key": _event_runtime_scope_key(event, _event_runtime_scope_key(merged_event)),
                        "participant_id": _event_participant_id(event, _event_participant_id(merged_event)),
                        "participantId": _event_participant_id(event, _event_participant_id(merged_event)),
                        "seq": int(event.get("seq") or merged_event.get("seq") or 0),
                        "timestamp": float(event.get("timestamp") or merged_event.get("timestamp") or 0),
                        "payload": merged_payload,
                    }
                keep_row = pending[-1][0]
                delete_ids = [int(row["id"]) for row, _ in pending[:-1]]
                for start in range(0, len(delete_ids), 500):
                    chunk = delete_ids[start:start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(f"DELETE FROM run_events WHERE id IN ({placeholders})", tuple(chunk))
                payload = merged_event.get("payload") if isinstance(merged_event.get("payload"), dict) else {}
                merged_participant_id = _event_participant_id(merged_event)
                if merged_participant_id and isinstance(payload, dict):
                    payload = dict(payload)
                    payload.setdefault("participant_id", merged_participant_id)
                    merged_event["payload"] = payload
                compact_frame_blob, compact_frame_format = encode_run_event_frame(merged_event)
                compact_runtime_source_seq = runtime_source_seq_from_event(merged_event)
                conn.execute(
                    """
                    UPDATE run_events
                    SET run_id = ?,
                        turn_id = ?,
                        runtime_session_id = ?,
                        runtime_scope_key = ?,
                        participant_id = ?,
                        seq = ?,
                        timestamp = ?,
                        payload_json = ?,
                        event_json = ?,
                        frame_blob = ?,
                        frame_format = ?,
                        retention_class = COALESCE(NULLIF(retention_class, ''), ?),
                        projection_state = COALESCE(NULLIF(projection_state, ''), 'raw'),
                        runtime_source_seq = ?
                    WHERE id = ?
                    """,
                    (
                        _event_run_id(merged_event),
                        _event_turn_id(merged_event),
                        str(merged_event.get("runtime_session_id") or ""),
                        _event_runtime_scope_key(merged_event),
                        merged_participant_id,
                        int(merged_event.get("seq") or 0),
                        float(merged_event.get("timestamp") or 0),
                        _json_dumps(payload),
                        _json_dumps(merged_event),
                        compact_frame_blob,
                        compact_frame_format,
                        _run_event_retention_class(str(merged_event.get("type") or "")),
                        compact_runtime_source_seq,
                        int(keep_row["id"]),
                    ),
                )
                project_run_event_search_index(
                    conn,
                    row_id=int(keep_row["id"]),
                    session_id=str(keep_row["session_id"] or merged_event.get("stored_session_id") or ""),
                    seq=int(merged_event.get("seq") or 0),
                    event_type=str(keep_row["event_type"] or merged_event.get("type") or ""),
                    runtime_scope_key=_event_runtime_scope_key(merged_event),
                    runtime_source_seq=compact_runtime_source_seq,
                    event=merged_event,
                    updated_at=float(merged_event.get("timestamp") or keep_row["timestamp"] or 0),
                )
                compacted_segments += 1
                deleted_events += len(delete_ids)
                updated_events += 1
                normalized_run_id = _event_run_id(merged_event)
                if normalized_run_id:
                    affected_run_ids.add(normalized_run_id)

            prune_terminal_stream_rows()
            compact_terminal_duplicates()
            for row in rows_for_compaction():
                event = decode_run_event_row(row)
                if not isinstance(event, dict) or not _event_is_coalescible_stream_delta(event):
                    event_dict = event if isinstance(event, dict) else {}
                    event_type = str(_row_value(row, "event_type", "") or event_dict.get("type") or "")
                    boundary_stream_types = _boundary_stream_types(event_type)
                    if boundary_stream_types is None:
                        flush_pending()
                    elif boundary_stream_types:
                        for key in list(pending_by_key.keys()):
                            if len(key) > 1 and key[1] in boundary_stream_types:
                                flush_pending_key(key)
                    continue
                key = (str(row["session_id"] or ""), *_event_stream_identity(event))
                pending = pending_by_key.get(key, [])
                can_append_to_pending = (
                    pending
                    and str(row["session_id"] or "") == str(pending[-1][0]["session_id"] or "")
                    and _stream_events_can_coalesce(pending[-1][1], event)
                )
                if can_append_to_pending:
                    pending.append((row, event))
                    pending_by_key[key] = pending
                    continue
                flush_pending_key(key)
                pending_by_key[key] = [(row, event)]
            flush_pending()
            for run_id in affected_run_ids:
                conn.execute(
                    """
                    UPDATE runs
                    SET last_seq = MAX(COALESCE(last_seq, 0), (
                        SELECT COALESCE(MAX(seq), 0)
                        FROM run_events
                        WHERE run_id = ?
                    ))
                    WHERE run_id = ?
                    """,
                    (run_id, run_id),
                )
            return {
                "compacted_segments": compacted_segments,
                "pruned_terminal_stream_events": pruned_terminal_stream_events,
                "deduplicated_terminal_groups": deduplicated_terminal_groups,
                "deleted_events": deleted_events,
                "updated_events": updated_events,
                "vacuumed": False,
            }

        result = self._execute_write(_do)
        if vacuum and int(result.get("deleted_events") or 0) > 0:
            with self._lock:
                self._conn.execute("VACUUM")
            result = {**result, "vacuumed": True}
        return result

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
        try:
            self.repair_control_only_active_runs(session_id=stable)
        except Exception as exc:
            logger.debug("control-only active run repair skipped for %s: %s", stable, exc)
        active_runtime_sql = _active_runtime_run_sql("runs.run_id")
        with self._lock:
            active = self._conn.execute(
                f"""
                SELECT * FROM runs
                WHERE session_id = ?
                  AND status IN ({_sql_status_literals(ACTIVE_RUN_STATUSES)})
                  AND {active_runtime_sql}
                ORDER BY updated_at DESC, started_at DESC
                LIMIT 1
                """,
                (stable,),
            ).fetchone()
            if active is None:
                active = self._live_owner_control_only_active_run_locked(
                    self._conn,
                    session_id=stable,
                )
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
