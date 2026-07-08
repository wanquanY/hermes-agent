"""ADR-0001 §Phase 1.C Activity Command reconciler.

Drives durable ``activity_commands`` intents through the Phase 1 Activity
Command state machine and emits canonical ``activity.command.*`` run_events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from hermes_team_mission.domain.activity import (
    ACTIVITY_COMMAND_EVENT_TYPES,
    ACTIVITY_COMMAND_KINDS,
    is_legal_transition,
)

logger = logging.getLogger(__name__)

# Default poll cadence. Override with DOVIE_ACTIVITY_RECONCILER_INTERVAL_MS
# (string of milliseconds). Tests use force=True via run_one_cycle().
_DEFAULT_POLL_INTERVAL_MS = 200

# Default per-command timeout. If a command lingers in dispatched state
# longer than this, reconciler marks it failed and emits
# activity.command.run.failed.
_DEFAULT_COMMAND_TIMEOUT_S = 60


def reconciler_disabled() -> bool:
    return str(os.environ.get("DOVIE_ACTIVITY_RECONCILER_DISABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _env_poll_interval_ms() -> int:
    raw = str(os.environ.get("DOVIE_ACTIVITY_RECONCILER_INTERVAL_MS", "")).strip()
    if not raw:
        return _DEFAULT_POLL_INTERVAL_MS
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_POLL_INTERVAL_MS


def _payload(command: dict[str, Any]) -> dict[str, Any]:
    payload = command.get("payload")
    return payload if isinstance(payload, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


class ActivityReconciler:
    """ADR-0001 §Phase 1.C: drives activity_commands state machine."""

    def __init__(
        self,
        db: Any,
        *,
        poll_interval_ms: int = _DEFAULT_POLL_INTERVAL_MS,
        command_timeout_s: int = _DEFAULT_COMMAND_TIMEOUT_S,
        batch_size: int = 100,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._db = db
        effective_poll_ms = poll_interval_ms
        if effective_poll_ms == _DEFAULT_POLL_INTERVAL_MS:
            effective_poll_ms = _env_poll_interval_ms()
        self._poll_interval_s = max(0.05, effective_poll_ms / 1000.0)
        self._command_timeout_s = max(5, int(command_timeout_s))
        self._batch_size = max(1, int(batch_size))
        self._logger = logger_ or logger
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()

    # -- public API -------------------------------------------------
    async def start(self) -> None:
        if reconciler_disabled():
            self._logger.debug("activity reconciler disabled by env")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="activity-reconciler")

    async def stop(self, *, timeout: float = 5.0) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def run_one_cycle(self) -> dict[str, Any]:
        """Synchronous single-cycle drive - for tests and CLI tools."""
        return self._process_pending_commands_once()

    # -- internals --------------------------------------------------
    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_pending_commands_once()
            except Exception:
                self._logger.exception("activity reconciler cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_s)
            except asyncio.TimeoutError:
                pass

    def _process_pending_commands_once(self) -> dict[str, Any]:
        summary = {"processed": 0, "satisfied": 0, "failed": 0, "skipped": 0}
        list_pending = getattr(self._db, "list_pending_activity_commands", None)
        if not callable(list_pending):
            return summary
        commands = list_pending(states=("accepted", "dispatched"), limit=self._batch_size)
        for original in commands:
            if not isinstance(original, dict):
                summary["skipped"] += 1
                continue
            command_id = _text(original.get("command_id"))
            command = self._current_command(command_id) or original
            state = _text(command.get("state"))
            if state not in {"accepted", "dispatched"}:
                summary["skipped"] += 1
                continue
            summary["processed"] += 1
            try:
                outcome = self._handle_command(command)
            except Exception as exc:
                self._logger.exception(
                    "activity command handler failed command_id=%s", command_id
                )
                outcome = self._fail_outcome(command, exc)
            next_state = _text(outcome.get("state"))
            if not next_state:
                summary["skipped"] += 1
                continue
            current = self._current_command(command_id) or command
            current_state = _text(current.get("state"))
            if current_state not in {"accepted", "dispatched"}:
                summary["skipped"] += 1
                continue
            if current_state == next_state:
                summary["skipped"] += 1
                continue
            if not is_legal_transition(current_state, next_state):
                summary["skipped"] += 1
                continue
            updated = self._update_command_state(
                command_id,
                next_state=next_state,
                error_reason=_text(outcome.get("error_reason")),
            )
            if not updated:
                summary["skipped"] += 1
                continue
            if next_state == "satisfied":
                summary["satisfied"] += 1
            elif next_state == "failed":
                summary["failed"] += 1
        return summary

    def _handle_command(self, command: dict[str, Any]) -> dict[str, Any]:
        kind = _text(command.get("kind"))
        if kind not in ACTIVITY_COMMAND_KINDS:
            raise ValueError(f"unknown activity command kind: {kind}")
        if kind == "create":
            return self._handle_create(command)
        if kind == "start":
            return self._handle_start(command)
        if kind == "cancel":
            return self._handle_cancel(command)
        if kind == "complete":
            return self._handle_complete(command)
        raise ValueError(f"unhandled activity command kind: {kind}")

    def _handle_create(self, command: dict[str, Any]) -> dict[str, Any]:
        payload = _payload(command)
        activity_id = _text(command.get("activity_id"))
        conversation_session_id = _conversation_session_id(command)
        if not activity_id:
            raise ValueError("activity_id required")
        activity = self._get_activity(activity_id)
        if not activity:
            create_activity = getattr(self._db, "create_activity", None)
            if not callable(create_activity):
                raise RuntimeError("db.create_activity unavailable")
            create_activity(
                activity_id=activity_id,
                conversation_id=_text(payload.get("conversation_id"))
                or conversation_session_id,
                kind=_text(payload.get("kind")),
                parent_activity_id=_text(payload.get("parent_activity_id")) or None,
            )
        self._emit_event(
            "activity.command.created",
            activity_id=activity_id,
            command_id=_text(command.get("command_id")),
            conversation_session_id=conversation_session_id,
            payload={
                "kind": _text(payload.get("kind")),
                "state": "satisfied",
            },
        )
        return {"state": "satisfied"}

    def _handle_start(self, command: dict[str, Any]) -> dict[str, Any]:
        state = _text(command.get("state"))
        activity_id = _text(command.get("activity_id"))
        if not self._get_activity(activity_id):
            raise ValueError(f"activity row missing: {activity_id}")
        if state == "accepted":
            self._emit_event(
                "activity.command.start.accepted",
                activity_id=activity_id,
                command_id=_text(command.get("command_id")),
                conversation_session_id=self._conversation_session_id_for_command(command),
                payload={"state": "dispatched"},
            )
            return {"state": "dispatched"}
        if state == "dispatched":
            intent_at = _float(command.get("intent_at"))
            if intent_at > 0 and time.time() - intent_at > self._command_timeout_s:
                message = "activity start command timed out before worker spawn ack"
                self._emit_event(
                    "activity.command.run.failed",
                    activity_id=activity_id,
                    command_id=_text(command.get("command_id")),
                    conversation_session_id=self._conversation_session_id_for_command(command),
                    payload={"state": "failed", "message": message},
                )
                return {"state": "failed", "error_reason": message}
        return {}

    def _handle_cancel(self, command: dict[str, Any]) -> dict[str, Any]:
        payload = _payload(command)
        activity_id = _text(command.get("activity_id"))
        activity = self._get_activity(activity_id)
        if not activity:
            raise ValueError(f"activity row missing: {activity_id}")
        status = _text((activity or {}).get("status"))
        if status not in {"completed", "failed", "cancelled"}:
            mark_cancelled = getattr(self._db, "mark_activity_cancelled", None)
            if not callable(mark_cancelled):
                raise RuntimeError("db.mark_activity_cancelled unavailable")
            updated = mark_cancelled(
                activity_id,
                result_summary=_text(payload.get("reason")) or None,
            )
            if not updated:
                raise RuntimeError(f"activity cancel transition rejected: {activity_id}")
        self._emit_event(
            "activity.command.cancelled",
            activity_id=activity_id,
            command_id=_text(command.get("command_id")),
            conversation_session_id=self._conversation_session_id_for_command(command),
            payload={
                "state": "satisfied",
                "reason": _text(payload.get("reason")),
            },
        )
        return {"state": "satisfied"}

    def _handle_complete(self, command: dict[str, Any]) -> dict[str, Any]:
        payload = _payload(command)
        activity_id = _text(command.get("activity_id"))
        activity = self._get_activity(activity_id)
        if not activity:
            raise ValueError(f"activity row missing: {activity_id}")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        now = time.time()
        update_status = getattr(self._db, "update_activity_status", None)
        if not callable(update_status):
            raise RuntimeError("db.update_activity_status unavailable")
        if _text(activity.get("status")) != "completed":
            updated = update_status(
                activity_id,
                "completed",
                completed_at=now,
                result_json=result,
            )
            if not updated:
                raise RuntimeError(f"activity complete transition rejected: {activity_id}")
        self._emit_event(
            "activity.command.completed",
            activity_id=activity_id,
            command_id=_text(command.get("command_id")),
            conversation_session_id=self._conversation_session_id_for_command(command),
            payload={
                "state": "satisfied",
                "result": result or {},
                "completed_at": now,
            },
        )
        return {"state": "satisfied"}

    def _emit_event(
        self,
        event_type: str,
        *,
        activity_id: str,
        command_id: str,
        conversation_session_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Emit an activity.command.* event via run_control.record_event."""
        if event_type not in ACTIVITY_COMMAND_EVENT_TYPES:
            raise ValueError(f"unsupported activity command event type: {event_type}")
        stable = _text(conversation_session_id)
        if not stable:
            stable = _text(activity_id)
        event_payload: dict[str, Any] = {
            "activity_id": _text(activity_id),
            "command_id": _text(command_id),
        }
        if isinstance(payload, dict):
            event_payload.update(payload)
        from tui_gateway.services import run_control

        frame = {
            "type": event_type,
            "session_id": stable,
            "conversation_session_id": stable,
            "activity_id": _text(activity_id),
            "seq": run_control.next_event_seq(stable, db=self._db),
            "payload": event_payload,
        }
        run_control.record_event(frame, db=self._db)

    def _fail_outcome(self, command: dict[str, Any], exc: Exception) -> dict[str, Any]:
        message = str(exc) or exc.__class__.__name__
        try:
            self._emit_event(
                "activity.command.run.failed",
                activity_id=_text(command.get("activity_id")),
                command_id=_text(command.get("command_id")),
                conversation_session_id=self._conversation_session_id_for_command(command),
                payload={"state": "failed", "message": message},
            )
        except Exception:
            self._logger.exception(
                "activity command failure event emit failed command_id=%s",
                _text(command.get("command_id")),
            )
        return {"state": "failed", "error_reason": message}

    def _current_command(self, command_id: str) -> dict[str, Any]:
        get_command = getattr(self._db, "get_activity_command", None)
        if not command_id or not callable(get_command):
            return {}
        current = get_command(command_id)
        return current if isinstance(current, dict) else {}

    def _update_command_state(
        self,
        command_id: str,
        *,
        next_state: str,
        error_reason: str = "",
    ) -> dict[str, Any]:
        update_state = getattr(self._db, "update_activity_command_state", None)
        if not callable(update_state):
            return {}
        updated = update_state(
            command_id,
            next_state=next_state,
            error_reason=error_reason,
        )
        return updated if isinstance(updated, dict) else {}

    def _get_activity(self, activity_id: str) -> dict[str, Any]:
        get_activity = getattr(self._db, "get_activity", None)
        if not activity_id or not callable(get_activity):
            return {}
        activity = get_activity(activity_id)
        return activity if isinstance(activity, dict) else {}

    def _conversation_session_id_for_command(self, command: dict[str, Any]) -> str:
        explicit = _conversation_session_id(command)
        if explicit:
            return explicit
        activity = self._get_activity(_text(command.get("activity_id")))
        return _text((activity or {}).get("conversation_id"))


def _conversation_session_id(command: dict[str, Any]) -> str:
    payload = _payload(command)
    return _text(
        payload.get("conversation_session_id")
        or payload.get("conversation_id")
        or command.get("conversation_session_id")
    )


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
