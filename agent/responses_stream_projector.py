"""Classify Responses stream events into Hermes content/reasoning semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True)
class ResponsesStreamProjection:
    event_type: str = ""
    content_delta: str = ""
    reasoning_delta: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    completed_item: Any = None
    terminal_response: Any = None
    has_tool_call: bool = False
    error_message: str = ""
    error_code: str = ""
    error_param: str = ""


class ResponsesStreamProjector:
    """Stateful phase classifier shared by primary and fallback streams."""

    def __init__(self) -> None:
        self._active_message_phase = ""

    def project(self, event: Any) -> ResponsesStreamProjection:
        event_type = str(_field(event, "type", "") or "")

        if event_type in {"response.output_item.added", "response.output_item.done"}:
            item = _field(event, "item")
            item_type = str(_field(item, "type", "") or "")
            if item_type == "message":
                self._active_message_phase = str(
                    _field(item, "phase", "") or ""
                ).strip().lower()
            is_tool_call = "function_call" in item_type
            return ResponsesStreamProjection(
                event_type=event_type,
                tool_call_id=(
                    str(
                        _field(item, "call_id", "")
                        or _field(item, "id", "")
                        or ""
                    )
                    if is_tool_call
                    else ""
                ),
                tool_name=(
                    str(_field(item, "name", "") or "")
                    if is_tool_call
                    else ""
                ),
                completed_item=item if event_type == "response.output_item.done" else None,
                has_tool_call=is_tool_call,
            )

        if "function_call" in event_type:
            return ResponsesStreamProjection(event_type=event_type, has_tool_call=True)

        if event_type == "response.output_text.delta" or "output_text.delta" in event_type:
            delta = str(_field(event, "delta", "") or "")
            if self._active_message_phase in {"commentary", "analysis"}:
                return ResponsesStreamProjection(
                    event_type=event_type, reasoning_delta=delta
                )
            return ResponsesStreamProjection(event_type=event_type, content_delta=delta)

        if "reasoning" in event_type and "delta" in event_type:
            return ResponsesStreamProjection(
                event_type=event_type,
                reasoning_delta=str(_field(event, "delta", "") or ""),
            )

        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            return ResponsesStreamProjection(
                event_type=event_type,
                terminal_response=_field(event, "response"),
            )

        if event_type == "error":
            return ResponsesStreamProjection(
                event_type=event_type,
                error_message=str(_field(event, "message", "") or "stream emitted error event"),
                error_code=str(_field(event, "code", "") or ""),
                error_param=str(_field(event, "param", "") or ""),
            )

        return ResponsesStreamProjection(event_type=event_type)


__all__ = ["ResponsesStreamProjection", "ResponsesStreamProjector"]
