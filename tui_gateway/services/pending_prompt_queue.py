"""Typed pending-prompt queue for busy TUI/control-plane submissions.

The queue stores complete ``run.submit`` intent, not only display text. This
keeps run/turn identity, native attachments, model selection, toolset scope,
and origin transport intact when a busy conversation accepts a future turn.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PendingPrompt:
    request_id: Any
    conversation_session_id: str
    run_id: str
    turn_id: str
    params: dict[str, Any]
    transport: Any = None
    profile_context: dict[str, Any] | None = None
    blocked_by_run_id: str = ""
    enqueued_at: float = 0.0

    @classmethod
    def from_submit(
        cls,
        *,
        request_id: Any,
        conversation_session_id: str,
        run_id: str,
        turn_id: str,
        params: dict[str, Any],
        transport: Any = None,
        profile_context: dict[str, Any] | None = None,
        blocked_by_run_id: str = "",
    ) -> "PendingPrompt":
        submit_params = copy.deepcopy(dict(params or {}))
        submit_params.pop("_run_registry_reserved", None)
        submit_params.pop("_control_plane_reserved", None)
        submit_params.pop("control_plane_reserved", None)
        submit_params.pop("controlPlaneReserved", None)
        submit_params["conversation_session_id"] = conversation_session_id
        submit_params["session_id"] = conversation_session_id
        submit_params["client_run_id"] = run_id
        submit_params["run_id"] = run_id
        submit_params["turn_id"] = turn_id
        return cls(
            request_id=request_id,
            conversation_session_id=str(conversation_session_id or "").strip(),
            run_id=str(run_id or "").strip(),
            turn_id=str(turn_id or "").strip(),
            params=submit_params,
            transport=transport,
            profile_context=dict(profile_context or {}) or None,
            blocked_by_run_id=str(blocked_by_run_id or "").strip(),
            enqueued_at=time.time(),
        )


class PendingPromptQueue:
    """Thread-safe FIFO keyed by profile storage scope and conversation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queues: dict[tuple[str, str], deque[PendingPrompt]] = defaultdict(deque)

    @staticmethod
    def _key(scope: str, conversation_session_id: str) -> tuple[str, str]:
        return (
            str(scope or "").strip(),
            str(conversation_session_id or "").strip(),
        )

    def enqueue(self, scope: str, prompt: PendingPrompt) -> int:
        key = self._key(scope, prompt.conversation_session_id)
        if not key[1]:
            raise ValueError("conversation_session_id is required")
        with self._lock:
            queue = self._queues[key]
            if any(item.run_id == prompt.run_id for item in queue):
                return next(
                    index
                    for index, item in enumerate(queue, start=1)
                    if item.run_id == prompt.run_id
                )
            queue.append(prompt)
            return len(queue)

    def claim_next(self, scope: str, conversation_session_id: str) -> PendingPrompt | None:
        key = self._key(scope, conversation_session_id)
        with self._lock:
            queue = self._queues.get(key)
            if not queue:
                return None
            prompt = queue.popleft()
            if not queue:
                self._queues.pop(key, None)
            return prompt

    def requeue_front(self, scope: str, prompt: PendingPrompt) -> None:
        key = self._key(scope, prompt.conversation_session_id)
        with self._lock:
            queue = self._queues[key]
            if not any(item.run_id == prompt.run_id for item in queue):
                queue.appendleft(prompt)

    def peek(self, scope: str, conversation_session_id: str) -> PendingPrompt | None:
        key = self._key(scope, conversation_session_id)
        with self._lock:
            queue = self._queues.get(key)
            return queue[0] if queue else None

    def snapshot(self, scope: str, conversation_session_id: str) -> list[dict[str, Any]]:
        key = self._key(scope, conversation_session_id)
        with self._lock:
            queue = list(self._queues.get(key) or ())
        return [
            {
                "run_id": item.run_id,
                "turn_id": item.turn_id,
                "text": str(item.params.get("text") or ""),
                "attachments": copy.deepcopy(item.params.get("attachments") or []),
                "enqueued_at": item.enqueued_at,
            }
            for item in queue
        ]

    def clear(self, scope: str, conversation_session_id: str) -> list[PendingPrompt]:
        key = self._key(scope, conversation_session_id)
        with self._lock:
            return list(self._queues.pop(key, ()))

    def has_conversation(self, conversation_session_id: str) -> bool:
        stable = str(conversation_session_id or "").strip()
        if not stable:
            return False
        with self._lock:
            return any(key[1] == stable and queue for key, queue in self._queues.items())

    def reset_for_tests(self) -> None:
        with self._lock:
            self._queues.clear()


pending_prompt_queue = PendingPromptQueue()


def queue_scope_for_db(db: Any) -> str:
    """Return the same profile boundary used by the per-profile run store."""
    return str(getattr(db, "db_path", "") or "").strip()


__all__ = [
    "PendingPrompt",
    "PendingPromptQueue",
    "pending_prompt_queue",
    "queue_scope_for_db",
]
