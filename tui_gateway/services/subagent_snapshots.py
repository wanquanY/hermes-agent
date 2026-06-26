"""Durable subagent run snapshots derived from persisted gateway events."""

from __future__ import annotations

from typing import Any

SUBAGENT_SNAPSHOT_EVENT_TYPES = (
    "subagent.spawn_requested",
    "subagent.start",
    "subagent.tool",
    "subagent.progress",
    "subagent.reasoning_delta",
    "subagent.thinking",
    "subagent.complete",
)


def subagent_id_for_event(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        payload.get("subagent_id")
        or payload.get("subagentId")
        or payload.get("id")
        or ""
    ).strip()


def filter_subagent_events(
    events: list[dict[str, Any]],
    subagent_id: str,
) -> list[dict[str, Any]]:
    normalized = str(subagent_id or "").strip()
    if not normalized:
        return [event for event in events if subagent_id_for_event(event)]
    return [
        event
        for event in events
        if subagent_id_for_event(event) == normalized
    ]


def compact_subagent_detail_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact persisted subagent detail events for history replay.

    Live streaming may emit token-sized ``subagent.output_delta`` and
    ``subagent.reasoning_delta`` events. Persisted history only needs
    contiguous text segments separated by tools, progress, or lifecycle
    events. Coalescing here keeps old databases usable without changing the
    right-panel timeline semantics.
    """
    compacted: list[dict[str, Any]] = []
    pending_delta: dict[str, Any] | None = None

    def flush_pending() -> None:
        nonlocal pending_delta
        if pending_delta is not None:
            compacted.append(pending_delta)
            pending_delta = None

    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type not in {"subagent.output_delta", "subagent.reasoning_delta"}:
            flush_pending()
            compacted.append(event)
            continue
        text = _event_text(event)
        if not text:
            continue
        if (
            pending_delta is None
            or str(pending_delta.get("type") or "") != event_type
            or subagent_id_for_event(pending_delta) != subagent_id_for_event(event)
        ):
            flush_pending()
            pending_delta = _minimal_text_delta_event(event, text)
            continue
        pending_payload = pending_delta.get("payload") if isinstance(pending_delta.get("payload"), dict) else {}
        pending_payload["text"] = f"{pending_payload.get('text') or ''}{text}"
        pending_delta["seq"] = event.get("seq") or pending_delta.get("seq")
        timestamp = event.get("timestamp") or pending_delta.get("timestamp")
        if timestamp is not None:
            pending_delta["timestamp"] = timestamp
        for key in ("run_id", "turn_id", "runtime_scope_key", "runtimeScopeKey"):
            if event.get(key):
                pending_delta[key] = event[key]
    flush_pending()
    return compacted


def build_subagent_run_snapshots(
    events: list[dict[str, Any]],
    *,
    stored_session_id: str = "",
) -> list[dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        if not event_type.startswith("subagent."):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        subagent_id = subagent_id_for_event(event)
        if not subagent_id:
            continue
        snapshot = runs.get(subagent_id)
        if snapshot is None:
            snapshot = _new_snapshot(event, payload, subagent_id, stored_session_id)
            runs[subagent_id] = snapshot
        _apply_snapshot_event(snapshot, event, payload)
    return sorted(
        runs.values(),
        key=lambda item: (
            int(item.get("task_index") or 0),
            float(item.get("started_at") or item.get("updated_at") or 0),
            str(item.get("subagent_id") or ""),
        ),
    )


def _new_snapshot(
    event: dict[str, Any],
    payload: dict[str, Any],
    subagent_id: str,
    stored_session_id: str,
) -> dict[str, Any]:
    session_id = str(
        stored_session_id
        or event.get("stored_session_id")
        or event.get("session_id")
        or ""
    ).strip()
    return {
        "id": subagent_id,
        "subagent_id": subagent_id,
        "session_id": session_id,
        "stored_session_id": session_id,
        "runtime_scope_key": str(
            event.get("runtime_scope_key")
            or event.get("runtimeScopeKey")
            or payload.get("runtime_scope_key")
            or ""
        ).strip(),
        "run_id": _text(event.get("run_id"), payload.get("run_id"), payload.get("runId")),
        "turn_id": _text(event.get("turn_id"), payload.get("turn_id"), payload.get("turnId")),
        "client_message_id": _text(payload.get("client_message_id"), payload.get("clientMessageId")),
        "delegate_call_id": _text(
            payload.get("delegate_call_id"),
            payload.get("delegateCallId"),
            payload.get("tool_call_id"),
            payload.get("toolCallId"),
            payload.get("tool_id"),
            payload.get("toolId"),
        ),
        "parent_subagent_id": _text(payload.get("parent_subagent_id"), payload.get("parent_id"), payload.get("parentId")),
        "depth": _int(payload.get("depth"), 0),
        "task_index": _int(payload.get("task_index") or payload.get("taskIndex"), 0),
        "task_count": max(1, _int(payload.get("task_count") or payload.get("taskCount"), 1)),
        "role": _text(payload.get("role")) or "leaf",
        "status": "queued",
        "goal": _text(payload.get("goal"), payload.get("task"), payload.get("instruction"), payload.get("message")),
        "dispatch_message": _text(
            payload.get("dispatch_message"),
            payload.get("dispatchMessage"),
            payload.get("prompt"),
            payload.get("goal"),
            payload.get("message"),
        ),
        "model": _text(payload.get("model"), payload.get("model_name"), payload.get("modelName")),
        "toolsets": _text_list(payload.get("toolsets") or payload.get("tool_sets") or payload.get("tools")),
        "agent_profile_id": _text(payload.get("agent_profile_id"), payload.get("agentProfileId")),
        "agent_profile_version_id": _text(payload.get("agent_profile_version_id"), payload.get("agentProfileVersionId")),
        "agent_name": _text(
            payload.get("agent_profile_name"),
            payload.get("agentProfileName"),
            payload.get("agent_name"),
            payload.get("agentName"),
            payload.get("display_name"),
            payload.get("displayName"),
            payload.get("name"),
            payload.get("title"),
        ),
        "agent_avatar": _text(
            payload.get("agent_profile_avatar"),
            payload.get("agentProfileAvatar"),
            payload.get("agent_avatar"),
            payload.get("agentAvatar"),
            payload.get("avatar"),
            payload.get("icon"),
        ),
        "summary": "",
        "metrics": {
            "duration_ms": 0,
            "tokens": 0,
            "tool_calls": 0,
        },
        "started_at": 0.0,
        "completed_at": 0.0,
        "updated_at": 0.0,
        "first_seq": _int(event.get("seq"), 0),
        "last_seq": _int(event.get("seq"), 0),
        "details_loaded": False,
    }


def _apply_snapshot_event(
    snapshot: dict[str, Any],
    event: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    event_type = str(event.get("type") or "").strip()
    at = _float(event.get("timestamp") or payload.get("timestamp") or payload.get("at"), 0.0)
    seq = _int(event.get("seq"), 0)
    if seq:
        first_seq = _int(snapshot.get("first_seq"), seq)
        snapshot["first_seq"] = min(first_seq or seq, seq)
        snapshot["last_seq"] = max(_int(snapshot.get("last_seq"), 0), seq)
    if at:
        if event_type != "subagent.complete" and not _float(snapshot.get("started_at"), 0.0):
            snapshot["started_at"] = at
        snapshot["updated_at"] = max(_float(snapshot.get("updated_at"), 0.0), at)

    _merge_identity(snapshot, payload)

    if event_type == "subagent.spawn_requested":
        if snapshot.get("status") not in _TERMINAL_STATUSES:
            snapshot["status"] = "queued"
    elif event_type in {"subagent.start", "subagent.output_delta", "subagent.reasoning_delta", "subagent.thinking", "subagent.tool", "subagent.progress"}:
        if snapshot.get("status") not in _TERMINAL_STATUSES:
            snapshot["status"] = "running"
    elif event_type == "subagent.complete":
        status = str(payload.get("status") or "").strip().lower()
        if status in {"failed", "error"}:
            snapshot["status"] = "failed"
        elif status in {"timeout", "timed_out"}:
            snapshot["status"] = "timeout"
        elif status in {"interrupted", "cancelled", "canceled"}:
            snapshot["status"] = "interrupted"
        else:
            snapshot["status"] = "completed"
        snapshot["completed_at"] = at or _float(snapshot.get("updated_at"), 0.0)

    if event_type == "subagent.complete":
        summary = _text(payload.get("summary"), payload.get("text"), payload.get("delta"), payload.get("output"), payload.get("message"))
        if summary:
            snapshot["summary"] = summary
    elif not snapshot.get("summary"):
        progress = _text(payload.get("summary"))
        if progress:
            snapshot["summary"] = progress

    metrics = snapshot.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
        snapshot["metrics"] = metrics
    metrics["duration_ms"] = max(
        _int(metrics.get("duration_ms"), 0),
        _int(payload.get("duration_ms") or payload.get("durationMs"), 0),
        int(_float(payload.get("duration_seconds") or payload.get("durationSeconds"), 0.0) * 1000),
    )
    metrics["tokens"] = max(
        _int(metrics.get("tokens"), 0),
        _int(payload.get("tokens") or payload.get("total_tokens") or payload.get("totalTokens"), 0),
        _int(payload.get("input_tokens"), 0) + _int(payload.get("output_tokens"), 0),
    )
    metrics["tool_calls"] = max(
        _int(metrics.get("tool_calls"), 0),
        _int(payload.get("tool_calls") or payload.get("toolCalls") or payload.get("tool_count") or payload.get("toolCount"), 0),
    )


def _merge_identity(snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
    for target, keys in {
        "run_id": ("run_id", "runId"),
        "turn_id": ("turn_id", "turnId"),
        "client_message_id": ("client_message_id", "clientMessageId"),
        "delegate_call_id": ("delegate_call_id", "delegateCallId", "tool_call_id", "toolCallId", "tool_id", "toolId"),
        "parent_subagent_id": ("parent_subagent_id", "parentSubagentId", "parent_id", "parentId"),
        "goal": ("goal", "task", "instruction", "message"),
        "dispatch_message": ("dispatch_message", "dispatchMessage", "prompt", "goal", "message"),
        "model": ("model", "model_name", "modelName"),
        "agent_profile_id": ("agent_profile_id", "agentProfileId", "profile_id", "profileId"),
        "agent_profile_version_id": ("agent_profile_version_id", "agentProfileVersionId", "profile_version_id", "profileVersionId"),
        "agent_name": ("agent_profile_name", "agentProfileName", "agent_name", "agentName", "display_name", "displayName", "name", "title"),
        "agent_avatar": ("agent_profile_avatar", "agentProfileAvatar", "agent_avatar", "agentAvatar", "avatar", "icon"),
    }.items():
        incoming = _text(*(payload.get(key) for key in keys))
        if incoming:
            snapshot[target] = incoming
    if payload.get("task_index") is not None or payload.get("taskIndex") is not None:
        snapshot["task_index"] = _int(payload.get("task_index") or payload.get("taskIndex"), _int(snapshot.get("task_index"), 0))
    if payload.get("task_count") is not None or payload.get("taskCount") is not None:
        snapshot["task_count"] = max(1, _int(payload.get("task_count") or payload.get("taskCount"), _int(snapshot.get("task_count"), 1)))
    if payload.get("depth") is not None:
        snapshot["depth"] = _int(payload.get("depth"), _int(snapshot.get("depth"), 0))
    if payload.get("role"):
        snapshot["role"] = _text(payload.get("role")) or snapshot.get("role") or "leaf"
    toolsets = _text_list(payload.get("toolsets") or payload.get("tool_sets") or payload.get("tools"))
    if toolsets:
        snapshot["toolsets"] = toolsets


def _text(*values: Any) -> str:
    for value in values:
        text = str(value if value is not None else "").strip()
        if text:
            return text
    return ""


def _event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(payload.get("text") or payload.get("delta") or payload.get("output") or "")


def _minimal_text_delta_event(event: dict[str, Any], text: str) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    minimal_payload: dict[str, Any] = {"text": text}
    for key in (
        "subagent_id",
        "subagentId",
        "parent_id",
        "parentId",
        "parent_subagent_id",
        "parentSubagentId",
        "depth",
        "task_index",
        "taskIndex",
        "task_count",
        "taskCount",
        "role",
        "delegate_call_id",
        "delegateCallId",
        "tool_call_id",
        "toolCallId",
        "tool_count",
        "toolCount",
    ):
        if payload.get(key) is not None:
            minimal_payload[key] = payload[key]
    minimal_event: dict[str, Any] = {
        "type": str(event.get("type") or "subagent.output_delta"),
        "seq": event.get("seq"),
        "payload": minimal_payload,
    }
    if event.get("timestamp") is not None:
        minimal_event["timestamp"] = event["timestamp"]
    for key in ("run_id", "turn_id", "runtime_scope_key", "runtimeScopeKey"):
        if event.get(key):
            minimal_event[key] = event[key]
    return minimal_event


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


_TERMINAL_STATUSES = {"completed", "failed", "timeout", "interrupted"}
