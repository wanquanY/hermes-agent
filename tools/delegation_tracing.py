"""Diagnostic producer tracing for delegated child-agent events."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

from utils import is_truthy_value

logger = logging.getLogger(__name__)


def trace_subagent_stream_producer(
    parent_agent: Any,
    *,
    event_type: str,
    subagent_id: str | None,
    delegate_call_id: str,
    task_index: int,
    offset: int,
    text: str,
    origin: Dict[str, str] | None = None,
) -> None:
    if not is_truthy_value(os.environ.get("DOVIE_STREAM_TRACE")):
        return
    logger.info(
        "[dovie-subagent-stream-source] stage=producer event_type=%s "
        "session_id=%s run_id=%s turn_id=%s subagent_id=%s delegate_call_id=%s "
        "task_index=%s offset=%s text_len=%s utf16_len=%s",
        event_type,
        str(getattr(parent_agent, "session_id", "") or ""),
        str(
            (origin or {}).get("run_id")
            or getattr(parent_agent, "_hermes_active_run_id", "")
            or ""
        ),
        str(
            (origin or {}).get("turn_id")
            or getattr(parent_agent, "_hermes_active_turn_id", "")
            or ""
        ),
        str(subagent_id or ""),
        delegate_call_id,
        task_index,
        offset,
        len(text),
        len(text.encode("utf-16-le")) // 2,
    )


def trace_subagent_event_producer(
    parent_agent: Any,
    *,
    event_type: str,
    subagent_id: str | None,
    delegate_call_id: str,
    task_index: int,
    source_index: int,
    payload: Dict[str, Any],
) -> None:
    if not is_truthy_value(os.environ.get("DOVIE_STREAM_TRACE")):
        return

    def json_bytes(value: Any) -> int:
        try:
            return len(
                json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            )
        except Exception:
            return -1

    logger.info(
        "[dovie-subagent-event-source] stage=producer event_type=%s "
        "session_id=%s run_id=%s turn_id=%s subagent_id=%s delegate_call_id=%s "
        "task_index=%s source_index=%s tool_id=%s status=%s tool_count=%s "
        "preview_bytes=%s args_bytes=%s result_bytes=%s context_bytes=%s "
        "dispatch_message_bytes=%s payload_bytes=%s",
        event_type,
        str(getattr(parent_agent, "session_id", "") or ""),
        str(
            payload.get("run_id")
            or getattr(parent_agent, "_hermes_active_run_id", "")
            or ""
        ),
        str(
            payload.get("turn_id")
            or getattr(parent_agent, "_hermes_active_turn_id", "")
            or ""
        ),
        str(subagent_id or ""),
        delegate_call_id,
        task_index,
        source_index,
        str(payload.get("tool_id") or ""),
        str(payload.get("status") or ""),
        payload.get("tool_count"),
        json_bytes(payload.get("preview")),
        json_bytes(payload.get("args")),
        json_bytes(payload.get("result")),
        json_bytes(payload.get("context")),
        json_bytes(payload.get("dispatch_message")),
        json_bytes(payload),
    )


__all__ = ["trace_subagent_event_producer", "trace_subagent_stream_producer"]
