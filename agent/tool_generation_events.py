"""Lifecycle primitives for streamed tool-call generation.

``tool.generating`` is externally visible runtime state, not a cosmetic hint.
Once it has been emitted, the invocation must either advance to ``tool.start``
or receive an explicit terminal event.  This module keeps that contract local
to the agent instead of asking every provider adapter to maintain its own
ad-hoc set.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def invoke_tool_generation_callback(
    callback: Callable[..., Any] | None,
    tool_name: str,
    tool_call_id: str | None = None,
) -> Any:
    """Invoke the two-argument contract without breaking legacy clients."""
    if callback is None:
        return None
    if not tool_call_id:
        return callback(tool_name)
    try:
        inspect.signature(callback).bind(tool_name, tool_call_id)
    except TypeError:
        return callback(tool_name)
    except (ValueError, AttributeError):
        # Some extension/builtin callables do not expose a Python signature.
        # Prefer the richer contract when their arity cannot be inspected.
        return callback(tool_name, tool_call_id)
    return callback(tool_name, tool_call_id)


@dataclass(frozen=True)
class OpenToolGeneration:
    """One invocation exposed as generating but not yet started."""

    tool_call_id: str
    tool_name: str


class ToolGenerationLifecycle:
    """Thread-safe ownership of externally visible tool-generation rows."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open: dict[str, OpenToolGeneration] = {}

    def generating(
        self,
        tool_name: str,
        tool_call_id: str | None,
    ) -> OpenToolGeneration | None:
        stable_id = str(tool_call_id or "").strip()
        stable_name = str(tool_name or "").strip()
        if not stable_id or not stable_name:
            return None
        generation = OpenToolGeneration(
            tool_call_id=stable_id,
            tool_name=stable_name,
        )
        with self._lock:
            self._open[stable_id] = generation
        return generation

    def started(self, tool_call_id: str | None) -> OpenToolGeneration | None:
        stable_id = str(tool_call_id or "").strip()
        if not stable_id:
            return None
        with self._lock:
            return self._open.pop(stable_id, None)

    def abort_open(self) -> tuple[OpenToolGeneration, ...]:
        with self._lock:
            open_generations = tuple(self._open.values())
            self._open.clear()
        return open_generations

    def snapshot(self) -> tuple[OpenToolGeneration, ...]:
        with self._lock:
            return tuple(self._open.values())


class ToolGenerationAgentMixin:
    """Agent-facing lifecycle API shared by every provider transport."""

    def _fire_tool_gen_started(
        self,
        tool_name: str,
        tool_call_id: str | None = None,
    ) -> None:
        """Expose an identity-stable invocation as argument generation begins."""
        lifecycle = getattr(self, "_tool_generation_lifecycle", None)
        if lifecycle is not None:
            lifecycle.generating(tool_name, tool_call_id)
        callback = getattr(self, "tool_gen_callback", None)
        if callback is not None:
            try:
                invoke_tool_generation_callback(
                    callback,
                    tool_name,
                    tool_call_id,
                )
            except Exception:
                pass

    def _mark_tool_generation_started(self, tool_call_id: str | None) -> None:
        """Advance one generated invocation into the executor-owned phase."""
        lifecycle = getattr(self, "_tool_generation_lifecycle", None)
        if lifecycle is not None:
            lifecycle.started(tool_call_id)

    def _abort_open_tool_generations(
        self,
        reason: str,
        *,
        status: str = "failed",
        error_code: str = "provider_stream_aborted",
    ) -> tuple[OpenToolGeneration, ...]:
        """Terminalize every generated invocation that never reached execution."""
        lifecycle = getattr(self, "_tool_generation_lifecycle", None)
        if lifecycle is None:
            return ()
        aborted = lifecycle.abort_open()
        callback = getattr(self, "tool_gen_abort_callback", None)
        if callback is None:
            return aborted
        for generation in aborted:
            try:
                callback(
                    generation.tool_name,
                    generation.tool_call_id,
                    status,
                    str(reason or "Tool generation aborted."),
                    error_code,
                )
            except Exception:
                pass
        return aborted


__all__ = [
    "OpenToolGeneration",
    "ToolGenerationAgentMixin",
    "ToolGenerationLifecycle",
    "invoke_tool_generation_callback",
]
