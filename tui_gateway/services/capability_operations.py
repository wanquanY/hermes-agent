"""Durable-in-process lifecycle state for capability control operations.

MCP and plugin setup can involve downloads, external desktop applications,
browser authorization, discovery, and runtime reloads.  A single blocking RPC
cannot represent those stages faithfully, so the gateway exposes operations
that can be polled (and mirrored as transient gateway events) by GUI clients.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


OperationUpdate = Callable[..., None]
OperationRunner = Callable[[OperationUpdate], dict[str, Any] | None]
OperationEmitter = Callable[[dict[str, Any]], None]

_TERMINAL_STATUSES = frozenset({"succeeded", "degraded", "failed", "cancelled"})
_MAX_COMPLETED_OPERATIONS = 128


@dataclass
class CapabilityOperation:
    operation_id: str
    kind: str
    target: str
    status: str = "queued"
    phase: str = "queued"
    progress: int = 0
    message: str = ""
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "phase": self.phase,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "result": dict(self.result),
            "details": dict(self.details),
            "terminal": self.status in _TERMINAL_STATUSES,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class CapabilityOperationRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._operations: dict[str, CapabilityOperation] = {}

    def start(
        self,
        *,
        kind: str,
        target: str,
        runner: OperationRunner,
        emit: OperationEmitter | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = CapabilityOperation(
            operation_id=f"capability-{uuid.uuid4().hex}",
            kind=str(kind or "").strip(),
            target=str(target or "").strip(),
            details=dict(details or {}),
        )
        with self._lock:
            self._operations[operation.operation_id] = operation
            self._prune_locked()

        context = contextvars.copy_context()

        def run() -> None:
            def update(**changes: Any) -> None:
                self.update(operation.operation_id, emit=emit, **changes)

            update(status="running", phase="starting", progress=1)
            try:
                outcome = runner(update) or {}
                outcome = dict(outcome)
                terminal_status = str(outcome.pop("operation_status", "succeeded"))
                if terminal_status not in _TERMINAL_STATUSES:
                    terminal_status = "succeeded"
                terminal_phase = str(outcome.pop("operation_phase", "ready"))
                terminal_message = str(outcome.pop("operation_message", ""))
                update(
                    status=terminal_status,
                    phase=terminal_phase,
                    progress=100,
                    message=terminal_message,
                    result=outcome,
                )
            except BaseException as exc:
                error = str(exc) or type(exc).__name__
                details: dict[str, Any] | None = None
                if operation.kind.startswith("mcp."):
                    from tui_gateway.services.mcp_error_protocol import normalize_mcp_failure

                    failure = normalize_mcp_failure(exc)
                    error = failure["message"]
                    details = {
                        "error_code": failure["code"],
                        "technical_error": failure["diagnostic"],
                    }
                update(
                    status="failed",
                    phase="failed",
                    progress=100,
                    error=error,
                    **({"details": details} if details else {}),
                )

        thread = threading.Thread(
            target=lambda: context.run(run),
            name=f"capability-operation-{operation.operation_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return self.get(operation.operation_id) or operation.snapshot()

    def update(
        self,
        operation_id: str,
        *,
        emit: OperationEmitter | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise KeyError(f"capability operation not found: {operation_id}")
            for key in ("status", "phase", "message", "error"):
                if key in changes:
                    setattr(operation, key, str(changes[key] or ""))
            if "progress" in changes:
                operation.progress = max(0, min(100, int(changes["progress"])))
            if isinstance(changes.get("result"), dict):
                operation.result = dict(changes["result"])
            if isinstance(changes.get("details"), dict):
                operation.details.update(changes["details"])
            operation.updated_at = time.time()
            snapshot = operation.snapshot()
        if emit is not None:
            try:
                emit(snapshot)
            except Exception:
                # Polling remains the source of truth when a transient event
                # cannot be delivered (for example after renderer reconnect).
                pass
        return snapshot

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            operation = self._operations.get(str(operation_id or "").strip())
            return operation.snapshot() if operation is not None else None

    def _prune_locked(self) -> None:
        completed = [
            item
            for item in self._operations.values()
            if item.status in _TERMINAL_STATUSES
        ]
        if len(completed) <= _MAX_COMPLETED_OPERATIONS:
            return
        completed.sort(key=lambda item: item.updated_at)
        for operation in completed[: -_MAX_COMPLETED_OPERATIONS]:
            self._operations.pop(operation.operation_id, None)


capability_operations = CapabilityOperationRegistry()
