"""Identity-bound output and reasoning stream callbacks for delegated agents."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tools.delegation_event_origin import (
    capture_parent_event_origin as _capture_parent_event_origin,
)
from tools.delegation_tracing import (
    trace_subagent_stream_producer as _trace_subagent_stream_producer,
)

logger = logging.getLogger(__name__)


def build_child_output_delta_callback(
    task_index: int,
    goal: str,
    parent_agent,
    task_count: int = 1,
    *,
    subagent_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    depth: Optional[int] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    role: Optional[str] = None,
    context: Optional[str] = None,
    delegate_call_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> Optional[callable]:
    """Build a callback that relays child assistant text deltas to the parent.

    This is intentionally separate from the tool-progress callback.
    Gateway/Dovie sessions need live child answer streaming in the side panel,
    while still keeping that text out of the parent's main assistant response.
    """
    del goal, context, agent_name
    if getattr(parent_agent, "_delegate_child_output_delta_enabled", True) is False:
        return None

    parent_cb = getattr(parent_agent, "tool_progress_callback", None)
    if not parent_cb:
        return None

    normalized_delegate_call_id = str(delegate_call_id or "").strip()
    raw_output_tool_name = getattr(parent_agent, "_delegate_child_output_tool_name", "")
    tool_name = (
        raw_output_tool_name.strip() if isinstance(raw_output_tool_name, str) else ""
    )
    parent_event_origin = _capture_parent_event_origin(parent_agent)
    execution_identity: Dict[str, str] = {}

    def identity_kwargs() -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            **parent_event_origin,
            **execution_identity,
            "task_index": task_index,
            "task_count": task_count,
            "tool_count": 0,
        }
        if subagent_id is not None:
            kw["subagent_id"] = subagent_id
        if parent_id is not None:
            kw["parent_id"] = parent_id
        if depth is not None:
            kw["depth"] = depth
        if model is not None:
            kw["model"] = model
        if toolsets is not None:
            kw["toolsets"] = list(toolsets)
        if role:
            kw["role"] = str(role)
        if normalized_delegate_call_id:
            kw["delegate_call_id"] = normalized_delegate_call_id
            kw["tool_call_id"] = normalized_delegate_call_id
        return kw

    stream_offset = 0

    def callback(text: Optional[str]) -> None:
        nonlocal stream_offset
        if text is None:
            return
        delta = str(text)
        if not delta:
            return
        try:
            _trace_subagent_stream_producer(
                parent_agent,
                event_type="subagent.output_delta",
                subagent_id=subagent_id,
                delegate_call_id=normalized_delegate_call_id,
                task_index=task_index,
                offset=stream_offset,
                text=delta,
                origin=parent_event_origin,
            )
            parent_cb(
                "subagent.output_delta",
                tool_name or None,
                delta,
                None,
                **identity_kwargs(),
                mode="append",
                delta=delta,
                offset=stream_offset,
            )
            stream_offset += len(delta.encode("utf-16-le")) // 2
        except Exception as exc:
            logger.debug("Parent output-delta callback failed: %s", exc)

    def bind_execution_identity(**identity: Any) -> None:
        execution_identity.clear()
        execution_identity.update({
            key: str(identity.get(key) or "").strip()
            for key in (
                "activity_id",
                "delegation_activity_id",
                "owner_activity_id",
            )
            if str(identity.get(key) or "").strip()
        })

    callback._bind_execution_identity = bind_execution_identity
    return callback


def build_child_reasoning_delta_callback(
    task_index: int,
    goal: str,
    parent_agent,
    task_count: int = 1,
    *,
    subagent_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    depth: Optional[int] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    role: Optional[str] = None,
    delegate_call_id: Optional[str] = None,
) -> Optional[callable]:
    """Relay provider reasoning deltas from a child agent to the parent UI."""
    del goal
    if getattr(parent_agent, "_delegate_child_reasoning_delta_enabled", True) is False:
        return None

    parent_cb = getattr(parent_agent, "tool_progress_callback", None)
    if not parent_cb:
        return None

    normalized_delegate_call_id = str(delegate_call_id or "").strip()
    raw_output_tool_name = getattr(parent_agent, "_delegate_child_output_tool_name", "")
    tool_name = (
        raw_output_tool_name.strip() if isinstance(raw_output_tool_name, str) else ""
    )
    parent_event_origin = _capture_parent_event_origin(parent_agent)
    execution_identity: Dict[str, str] = {}

    def identity_kwargs() -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            **parent_event_origin,
            **execution_identity,
            "task_index": task_index,
            "task_count": task_count,
            "tool_count": 0,
            "source": "provider_reasoning",
        }
        if subagent_id is not None:
            kw["subagent_id"] = subagent_id
        if parent_id is not None:
            kw["parent_id"] = parent_id
        if depth is not None:
            kw["depth"] = depth
        if model is not None:
            kw["model"] = model
        if toolsets is not None:
            kw["toolsets"] = list(toolsets)
        if role:
            kw["role"] = str(role)
        if normalized_delegate_call_id:
            kw["delegate_call_id"] = normalized_delegate_call_id
            kw["tool_call_id"] = normalized_delegate_call_id
        return kw

    stream_offset = 0

    def callback(text: Optional[str]) -> None:
        nonlocal stream_offset
        if text is None:
            return
        delta = str(text)
        if not delta:
            return
        try:
            _trace_subagent_stream_producer(
                parent_agent,
                event_type="subagent.reasoning_delta",
                subagent_id=subagent_id,
                delegate_call_id=normalized_delegate_call_id,
                task_index=task_index,
                offset=stream_offset,
                text=delta,
                origin=parent_event_origin,
            )
            parent_cb(
                "subagent.reasoning_delta",
                tool_name or None,
                delta,
                None,
                **identity_kwargs(),
                mode="append",
                delta=delta,
                offset=stream_offset,
            )
            stream_offset += len(delta.encode("utf-16-le")) // 2
        except Exception as exc:
            logger.debug("Parent reasoning-delta callback failed: %s", exc)

    def bind_execution_identity(**identity: Any) -> None:
        execution_identity.clear()
        execution_identity.update({
            key: str(identity.get(key) or "").strip()
            for key in (
                "activity_id",
                "delegation_activity_id",
                "owner_activity_id",
            )
            if str(identity.get(key) or "").strip()
        })

    callback._bind_execution_identity = bind_execution_identity
    return callback


__all__ = [
    "build_child_output_delta_callback",
    "build_child_reasoning_delta_callback",
]
