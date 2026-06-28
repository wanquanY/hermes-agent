"""Team Mission run-event retention policy hooks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from hermes_state_run_event_codec import payload_from_run_event_row


DEFAULT_RUN_EVENT_RETENTION_DAYS = 14
DEFAULT_RUN_EVENT_MAX_PER_SESSION = 5000
COALESCIBLE_STREAM_EVENT_TYPES = frozenset(
    {
        "reasoning.delta",
        "thinking.delta",
        "subagent.output_delta",
        "subagent.reasoning_delta",
        "subagent.thinking",
        "agent_profile_test.output_delta",
        "agent_profile_test.thinking",
    }
)
TERMINAL_RUN_PRUNABLE_EVENT_TYPES = frozenset(
    {
        "message.delta",
        "tool.progress",
        "tool.generating",
        *COALESCIBLE_STREAM_EVENT_TYPES,
    }
)


@dataclass(frozen=True)
class RetentionDecision:
    retention_class: str
    raw_retention_days: int
    max_events_per_session: int
    prunable_after_terminal: bool = False


class RunEventRetentionPolicy:
    """First-stage retention policy for raw run_events.

    This is intentionally conservative: it centralizes the existing stream
    pruning and count/age defaults without changing which raw facts survive.
    Later phases can extend this policy with projection-aware deletion rules.
    """

    def __init__(
        self,
        *,
        retention_days: int = DEFAULT_RUN_EVENT_RETENTION_DAYS,
        max_events_per_session: int = DEFAULT_RUN_EVENT_MAX_PER_SESSION,
        terminal_prunable_event_types: frozenset[str] = TERMINAL_RUN_PRUNABLE_EVENT_TYPES,
    ) -> None:
        self.retention_days = int(retention_days or DEFAULT_RUN_EVENT_RETENTION_DAYS)
        self.max_events_per_session = int(max_events_per_session or DEFAULT_RUN_EVENT_MAX_PER_SESSION)
        self._terminal_prunable_event_types = frozenset(terminal_prunable_event_types)

    def terminal_prunable_event_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._terminal_prunable_event_types))

    def classify(self, event: Any) -> RetentionDecision:
        event_type = _event_type_from_value(event)
        retention_class = self.classify_event_type(event_type)
        return RetentionDecision(
            retention_class=retention_class,
            raw_retention_days=self.retention_days,
            max_events_per_session=self.max_events_per_session,
            prunable_after_terminal=event_type in self._terminal_prunable_event_types,
        )

    def classify_event_type(self, event_type: str) -> str:
        normalized = str(event_type or "").strip()
        if normalized in COALESCIBLE_STREAM_EVENT_TYPES or normalized == "message.delta":
            return "stream"
        if normalized in {"tool.progress", "tool.generating"}:
            return "tool_stream"
        if normalized in {"message.start", "message.complete", "error", "session.interrupted"}:
            return "message_fact"
        if normalized in {"tool.start", "tool.complete"}:
            return "tool_fact"
        if normalized == "session.info":
            return "state_snapshot"
        if normalized.startswith(("approval.", "secret.", "sudo.", "input_approval.")):
            return "control_fact"
        if normalized.startswith(("mission.", "artifact.", "agent_dispatch.")):
            return "business"
        if normalized.startswith("subagent."):
            return "business"
        return "runtime_fact"

    def can_delete_terminal_stream_row(self, conn: Any, row: Any) -> bool:
        event_type = str(_row_value(row, "event_type") or "").strip()
        if event_type not in self._terminal_prunable_event_types:
            return False
        return not should_preserve_terminal_stream_row(conn, row)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _truthy_payload_flag(payload: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _payload_has_stream_text(payload: dict[str, Any]) -> bool:
    for key in ("delta", "text", "snapshot"):
        if str(payload.get(key) or "").strip():
            return True
    return False


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _event_type_from_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("type") or value.get("event_type") or "").strip()
    return str(_row_value(value, "event_type") or "").strip()


def _payload_from_row(row: Any) -> dict[str, Any]:
    return payload_from_run_event_row(row)


def _run_event_stream_text_exists(
    conn: Any,
    *,
    session_id: str,
    run_id: str,
) -> bool:
    session_id = str(session_id or "").strip()
    run_id = str(run_id or "").strip()
    if not session_id or not run_id:
        return False
    rows = conn.execute(
        """
        SELECT *
        FROM run_events
        WHERE session_id = ?
          AND run_id = ?
          AND event_type = 'message.delta'
        ORDER BY seq ASC, id ASC
        LIMIT 128
        """,
        (session_id, run_id),
    ).fetchall()
    for row in rows:
        if _payload_has_stream_text(_payload_from_row(row)):
            return True
    return False


def should_preserve_terminal_stream_row(conn: Any, row: Any) -> bool:
    """Return true when pruning this terminal stream row would lose Team Mission truth."""

    event_type = str(_row_value(row, "event_type") or "").strip()
    if event_type != "message.delta":
        return False
    payload = _payload_from_row(row)
    if not (
        _truthy_payload_flag(payload, "team_mission_final_deliverable", "teamMissionFinalDeliverable")
        and _truthy_payload_flag(payload, "team_mission_conversation_mirror", "teamMissionConversationMirror")
    ):
        return False
    if str(payload.get("mode") or "").strip().lower() != "snapshot":
        return True
    source_session_id = str(payload.get("source_session_id") or payload.get("sourceSessionId") or "").strip()
    source_run_id = str(payload.get("source_run_id") or payload.get("sourceRunId") or "").strip()
    return not _run_event_stream_text_exists(
        conn,
        session_id=source_session_id,
        run_id=source_run_id,
    )
