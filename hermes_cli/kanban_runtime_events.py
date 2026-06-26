"""Runtime event bridge for dispatcher-spawned Kanban workers.

The Kanban dispatcher launches workers as normal Hermes CLI subprocesses.
Task lifecycle facts live in ``kanban.db``; high-frequency runtime telemetry
does not. Agent callbacks are converted into run-scoped ``runtime.*`` records
and appended to per-task NDJSON files under the board directory. Dashboards can
project those records into the same timeline UI without making token streams
contend with the SQLite coordination database.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int_value(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _compact_payload_value(value: Any, *, limit: int = 12_000) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + "\n...[truncated]"
    if isinstance(value, dict):
        return {str(k): _compact_payload_value(v, limit=limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact_payload_value(v, limit=limit) for v in value[:100]]
    return value


class KanbanRuntimeEventSink:
    """Write streamed assistant/tool events back to the current Kanban task."""

    def __init__(
        self,
        *,
        task_id: str,
        board: str = "",
        run_id: Optional[int] = None,
        session_id: str = "",
        runtime_scope_key: str = "",
        flush_interval_seconds: float = 0.25,
        chunk_size: int = 360,
    ) -> None:
        self.task_id = task_id
        self.board = board
        self.run_id = run_id
        self.session_id = session_id
        self.runtime_scope_key = runtime_scope_key
        self.flush_interval_seconds = max(0.05, float(flush_interval_seconds or 0.25))
        self.chunk_size = max(80, int(chunk_size or 360))
        self._lock = threading.RLock()
        self._message_buf: list[str] = []
        self._reasoning_buf: list[str] = []
        self._last_message_flush = 0.0
        self._last_reasoning_flush = 0.0
        self._closed = False
        self._sequence = 0
        self._last_event_id = int(time.time() * 1_000_000)
        self._message_emitted = False

    @classmethod
    def from_env(cls, *, session_id: str = "", runtime_scope_key: str = "") -> "KanbanRuntimeEventSink | None":
        task_id = _text(os.environ.get("HERMES_KANBAN_TASK"))
        if not task_id:
            return None
        if _text(os.environ.get("HERMES_KANBAN_RUNTIME_EVENTS")).lower() in {"0", "false", "off", "no"}:
            return None
        return cls(
            task_id=task_id,
            board=_text(os.environ.get("HERMES_KANBAN_BOARD")),
            run_id=_int_value(os.environ.get("HERMES_KANBAN_RUN_ID")),
            session_id=session_id,
            runtime_scope_key=runtime_scope_key,
        )

    def update_session(self, *, session_id: str = "", runtime_scope_key: str = "") -> None:
        with self._lock:
            if session_id:
                self.session_id = session_id
            if runtime_scope_key:
                self.runtime_scope_key = runtime_scope_key

    def start(self, *, profile: str = "", workspace: str = "") -> None:
        self.emit(
            "runtime.session",
            {
                "type": "session.info",
                "session_id": self.session_id,
                "stored_session_id": self.session_id,
                "runtime_scope_key": self.runtime_scope_key,
                "profile": profile,
                "workspace": workspace,
                "pid": os.getpid(),
            },
        )
        self.emit(
            "runtime.message_start",
            {
                "type": "message.start",
                "role": "assistant",
                "session_id": self.session_id,
                "stored_session_id": self.session_id,
                "runtime_scope_key": self.runtime_scope_key,
            },
        )

    def on_message_delta(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._message_buf.append(str(text))
            self._flush_buffer_if_needed("message")

    def on_reasoning_delta(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._reasoning_buf.append(str(text))
            self._flush_buffer_if_needed("reasoning")

    def on_tool_generating(self, tool_name: str) -> None:
        self.flush()
        self.emit(
            "runtime.tool_generating",
            {
                "type": "tool.generating",
                "name": _text(tool_name),
            },
        )

    def on_tool_progress(
        self,
        event_type: str,
        function_name: str | None = None,
        preview: str | None = None,
        function_args: dict | None = None,
        **kwargs: Any,
    ) -> None:
        self.flush()
        normalized = _text(event_type) or "tool.progress"
        if normalized == "tool.started":
            normalized = "tool.progress"
        elif normalized == "tool.completed":
            normalized = "tool.progress"
        payload = {
            "type": normalized,
            "name": _text(function_name),
            "summary": _text(preview),
            "text": _text(preview),
            "arguments": function_args or {},
            **{str(k): _compact_payload_value(v) for k, v in kwargs.items()},
        }
        self.emit("runtime.tool_progress", payload)

    def on_tool_start(self, tool_call_id: str, function_name: str, function_args: dict) -> None:
        self.flush()
        self.emit(
            "runtime.tool_start",
            {
                "type": "tool.start",
                "tool_call_id": _text(tool_call_id),
                "toolCallId": _text(tool_call_id),
                "name": _text(function_name),
                "arguments": function_args or {},
            },
        )

    def on_tool_complete(
        self,
        tool_call_id: str,
        function_name: str,
        function_args: dict,
        function_result: Any,
    ) -> None:
        self.flush()
        result = _compact_payload_value(function_result)
        self.emit(
            "runtime.tool_complete",
            {
                "type": "tool.complete",
                "tool_call_id": _text(tool_call_id),
                "toolCallId": _text(tool_call_id),
                "name": _text(function_name),
                "arguments": function_args or {},
                "result": result,
                "result_text": result if isinstance(result, str) else "",
            },
        )

    def complete(self, *, text: str = "", status: str = "completed") -> None:
        if text and not self._message_emitted and not self._has_pending_message_text():
            self.on_message_delta(text)
        self.flush()
        self.emit(
            "runtime.message_complete",
            {
                "type": "message.complete",
                "role": "assistant",
                "status": status,
                "text": text,
            },
        )
        self.close()

    def fail(self, message: str) -> None:
        self.flush()
        self.emit(
            "runtime.message_complete",
            {
                "type": "message.complete",
                "role": "assistant",
                "status": "failed",
                "message": message,
                "text": message,
            },
        )
        self.close()

    def flush(self) -> None:
        with self._lock:
            self._flush_message_locked(force=True)
            self._flush_reasoning_locked(force=True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        return

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        with self._lock:
            self._sequence += 1
            event_payload = {
                "seq": self._sequence,
                "timestamp": time.time(),
                "run_id": self.run_id,
                "session_id": self.session_id,
                "stored_session_id": self.session_id,
                "runtime_scope_key": self.runtime_scope_key,
                **payload,
            }
            try:
                from hermes_cli import kanban_db as kb

                kb.append_runtime_event(
                    board=self.board or None,
                    task_id=self.task_id,
                    kind=kind,
                    payload=event_payload,
                    run_id=self.run_id,
                    event_id=self._next_event_id_locked(),
                )
            except Exception:
                # Runtime telemetry must never break the Kanban worker itself.
                return

    def _next_event_id_locked(self) -> int:
        now = int(time.time() * 1_000_000)
        if now <= self._last_event_id:
            now = self._last_event_id + 1
        self._last_event_id = now
        return now

    def _flush_buffer_if_needed(self, buffer_name: str) -> None:
        now = time.monotonic()
        if buffer_name == "message":
            text = "".join(self._message_buf)
            if len(text) >= self.chunk_size or "\n" in text or now - self._last_message_flush >= self.flush_interval_seconds:
                self._flush_message_locked(force=True)
        else:
            text = "".join(self._reasoning_buf)
            if len(text) >= self.chunk_size or "\n" in text or now - self._last_reasoning_flush >= self.flush_interval_seconds:
                self._flush_reasoning_locked(force=True)

    def _flush_message_locked(self, *, force: bool = False) -> None:
        if not self._message_buf:
            return
        text = "".join(self._message_buf)
        if not force and len(text) < self.chunk_size:
            return
        self._message_buf = []
        self._last_message_flush = time.monotonic()
        self._message_emitted = True
        self.emit(
            "runtime.message_delta",
            {
                "type": "message.delta",
                "role": "assistant",
                "text": text,
            },
        )

    def _flush_reasoning_locked(self, *, force: bool = False) -> None:
        if not self._reasoning_buf:
            return
        text = "".join(self._reasoning_buf)
        if not force and len(text) < self.chunk_size:
            return
        self._reasoning_buf = []
        self._last_reasoning_flush = time.monotonic()
        self.emit(
            "runtime.reasoning_delta",
            {
                "type": "reasoning.delta",
                "text": text,
                "source": "provider_reasoning",
            },
        )

    def _has_pending_message_text(self) -> bool:
        with self._lock:
            return bool("".join(self._message_buf).strip())
