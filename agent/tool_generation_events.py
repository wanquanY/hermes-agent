"""Compatibility helpers for identity-aware tool-generation callbacks."""

from __future__ import annotations

import inspect
from collections.abc import Callable
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


__all__ = ["invoke_tool_generation_callback"]
