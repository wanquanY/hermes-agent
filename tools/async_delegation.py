"""Compatibility facade for asynchronous subagent lifecycle controls.

Business state and execution scheduling are owned exclusively by
``SubagentExecutionService`` and ``SubagentExecutionRuntime``.  This module is
kept only so shutdown/status callers from older integrations do not need an
atomic migration.  It intentionally owns no executor, completion registry, or
conversation-message delivery path.
"""

from __future__ import annotations

from typing import Any

from hermes_agent.application.subagent_execution_service import (
    subagent_execution_runtime,
)


_RETIRED_ERROR = (
    "The legacy async-delegation dispatcher is retired. Use delegate_task "
    "with execution_mode='async'; it returns a persistent Activity handle."
)


def active_count() -> int:
    """Return the number of live process-local execution handles."""
    return subagent_execution_runtime.active_count()


def list_async_delegations() -> list[dict[str, Any]]:
    """Return live handles; durable history is queried from Activity/Run."""
    return subagent_execution_runtime.active_handles()


def cancel_async_delegation(
    activity_id: str,
    *,
    reason: str = "cancelled",
) -> bool:
    """Cancel a live execution by its canonical Activity identifier."""
    return subagent_execution_runtime.cancel(activity_id, reason=reason)


def interrupt_all(reason: str = "shutdown") -> int:
    """Interrupt all live executions during explicit stop or shutdown."""
    return subagent_execution_runtime.interrupt_all(reason=reason)


def dispatch_async_delegation(**_kwargs: Any) -> dict[str, Any]:
    """Reject the retired dispatcher instead of creating a second owner."""
    return {
        "status": "rejected",
        "error_code": "retired_api",
        "error": _RETIRED_ERROR,
    }


def dispatch_async_delegation_batch(**_kwargs: Any) -> dict[str, Any]:
    """Reject the retired batch dispatcher; async fan-out is now native."""
    return {
        "status": "rejected",
        "error_code": "retired_api",
        "error": _RETIRED_ERROR,
    }


def _reset_for_tests() -> None:
    subagent_execution_runtime.reset_for_tests()


__all__ = [
    "active_count",
    "cancel_async_delegation",
    "dispatch_async_delegation",
    "dispatch_async_delegation_batch",
    "interrupt_all",
    "list_async_delegations",
]
