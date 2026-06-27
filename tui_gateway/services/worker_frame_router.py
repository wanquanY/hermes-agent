"""Bridge between ``WorkerSupervisor`` and the main sidecar's
event/interactive registries.

The supervisor is intentionally agnostic — it owns the subprocess and
the line-framed JSON pipe but knows nothing about how events are
delivered to subscribers or how clarify/approval responses get back to
the worker. This module owns that mapping:

  worker → main
    EventFrame              → publish_recorded_event(frame.params)
    InteractiveRequestFrame → record in routing table + publish a
                              frontend-visible event so the user sees
                              the clarify/approval card
    RunTerminalFrame        → publish_run_terminal_event(...)

  main → worker
    *.respond handler       → router.respond(request_id, answer)
                              → WorkerSupervisor.send(scope_key,
                                                     InteractiveResponseFrame)

The routing tables are needed because the *.respond methods (Phase 6
moves these in-process) only carry the ``request_id``; the worker that
holds the blocked agent thread is identified solely through the table.

Phase 4c (this file) implements the router. Phase 5 ``prompt.submit``
populates ``record_run_start`` so terminal/event lookups can cross-fill
``stored_session_id`` if the worker omits it. Phase 6 rewires the
``clarify.respond`` / ``approval.respond`` / ``secret.respond`` /
``sudo.respond`` handlers to call ``router.respond`` instead of the
legacy in-worker registry.
"""

from __future__ import annotations

import logging
import threading
import json
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from tui_gateway.run_worker import (
    ActivityEventFrame,
    EventFrame,
    InteractiveRequestFrame,
    InteractiveResponseFrame,
    LogFrame,
    RunTerminalFrame,
)

_log = logging.getLogger(__name__)


_INTERACTIVE_KINDS = frozenset({"clarify", "approval", "secret", "sudo"})
_CLARIFY_APPROVAL_EVENT_STATES: dict[str, tuple[str, bool]] = {
    "clarify.request": ("clarify", True),
    "clarify.resolved": ("clarify", False),
    "approval.request": ("approval", True),
    "approval.resolved": ("approval", False),
}


@dataclass
class RunInfo:
    """Cross-fill source for run-terminal / event publishes when the
    worker doesn't echo the original ``stored_session_id`` / ``turn_id``
    in its frame. Populated by ``record_run_start`` from
    ``prompt.submit`` at run-create time."""

    run_id: str
    scope_key: str
    conversation_id: str
    stored_session_id: str
    turn_id: str
    run_context_json: Any = ""
    dispatch_activity_id: str = ""
    activity_kind: str = ""
    parent_scope_key: str = ""
    parent_conversation_id: str = ""
    parent_hermes_home: str = ""
    last_message_event: dict[str, Any] | None = None


@dataclass
class _Pending:
    scope_key: str
    conversation_id: str
    kind: str
    stored_session_id: str


class _SupervisorSender(Protocol):
    """The subset of ``WorkerSupervisor`` ``WorkerFrameRouter`` calls.

    Kept narrow so tests can stub it without standing up a real
    subprocess."""

    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool: ...


class WorkerFrameRouter:
    """Per-process router. One instance lives in the main sidecar."""

    def __init__(
        self,
        *,
        sender: _SupervisorSender,
        publish_event: Any,
        publish_run_terminal: Any,
    ) -> None:
        self._sender = sender
        # Injected so unit tests don't touch the global event-publish
        # singletons in ``run_control``. In production these are bound
        # to ``run_control.publish_recorded_event`` and
        # ``run_control.publish_run_terminal_event``.
        self._publish_event = publish_event
        self._publish_run_terminal = publish_run_terminal
        self._lock = threading.RLock()
        self._runs: dict[str, RunInfo] = {}
        self._pending: dict[str, _Pending] = {}

    # ── prompt.submit-side bookkeeping ───────────────────────────────

    def record_run_start(
        self,
        *,
        scope_key: str,
        conversation_id: str = "",
        run_id: str,
        stored_session_id: str,
        turn_id: str = "",
        run_context_json: Any = "",
        dispatch_activity_id: str = "",
        activity_kind: str = "",
        parent_scope_key: str = "",
        parent_conversation_id: str = "",
        parent_hermes_home: str = "",
    ) -> None:
        run_id = str(run_id or "").strip()
        if not run_id:
            return
        with self._lock:
            self._runs[run_id] = RunInfo(
                run_id=run_id,
                scope_key=str(scope_key or ""),
                conversation_id=str(conversation_id or stored_session_id or ""),
                stored_session_id=str(stored_session_id or ""),
                turn_id=str(turn_id or ""),
                run_context_json=run_context_json,
                dispatch_activity_id=str(dispatch_activity_id or ""),
                activity_kind=str(activity_kind or ""),
                parent_scope_key=str(parent_scope_key or ""),
                parent_conversation_id=str(parent_conversation_id or ""),
                parent_hermes_home=str(parent_hermes_home or ""),
            )

    def forget_run(self, run_id: str) -> None:
        run_id = str(run_id or "").strip()
        if not run_id:
            return
        with self._lock:
            self._runs.pop(run_id, None)

    def lookup_run(self, run_id: str) -> Optional[RunInfo]:
        """Resolve a ``run_id`` to its ``RunInfo`` (scope / stored_sid /
        turn) recorded at ``record_run_start`` time. Returns ``None``
        if the run has terminated and been ``forget_run``'d, or was
        never recorded. Used by ``primary_dispatch`` for ``run.cancel``
        routing — needs the scope to know WHICH worker subprocess to
        send the cancel frame to."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return None
        with self._lock:
            info = self._runs.get(run_id)
            return RunInfo(
                scope_key=info.scope_key,
                run_id=run_id,
                conversation_id=info.conversation_id,
                stored_session_id=info.stored_session_id,
                turn_id=info.turn_id,
                run_context_json=info.run_context_json,
                dispatch_activity_id=info.dispatch_activity_id,
                activity_kind=info.activity_kind,
                parent_scope_key=info.parent_scope_key,
                parent_conversation_id=info.parent_conversation_id,
                parent_hermes_home=info.parent_hermes_home,
                last_message_event=dict(info.last_message_event or {}) if info.last_message_event else None,
            ) if info is not None else None

    def lookup_activity_run(self, activity_id: str) -> Optional[RunInfo]:
        """Resolve a dispatch activity to its currently active worker run."""
        normalized_activity_id = str(activity_id or "").strip()
        if not normalized_activity_id:
            return None
        with self._lock:
            for info in self._runs.values():
                if info.dispatch_activity_id != normalized_activity_id:
                    continue
                return RunInfo(
                    scope_key=info.scope_key,
                    run_id=str(getattr(info, "run_id", "") or ""),
                    conversation_id=info.conversation_id,
                    stored_session_id=info.stored_session_id,
                    turn_id=info.turn_id,
                    run_context_json=info.run_context_json,
                    dispatch_activity_id=info.dispatch_activity_id,
                    activity_kind=info.activity_kind,
                    parent_scope_key=info.parent_scope_key,
                    parent_conversation_id=info.parent_conversation_id,
                    parent_hermes_home=info.parent_hermes_home,
                    last_message_event=dict(info.last_message_event or {}) if info.last_message_event else None,
                )
        return None

    # ── WorkerSupervisor callbacks ──────────────────────────────────

    async def on_event(
        self,
        scope_key: str,
        conversation_id: str | EventFrame,
        frame: EventFrame | None = None,
    ) -> None:
        """Forward the 1:1 worker→main event payload to live subscribers
        + persist it. ``params`` is the same dict the legacy ws bridge
        used to put on the wire."""
        if frame is None and isinstance(conversation_id, EventFrame):
            frame = conversation_id
            conversation_id = ""
        if frame is None:
            return
        conversation = str(conversation_id or "")
        params = dict(frame.params) if isinstance(frame.params, dict) else {}
        params.setdefault("runtime_scope_key", scope_key)
        params.setdefault("conversation_id", conversation)
        self._capture_last_message_event(params)
        run_context = self._run_context_for_event(params)
        try:
            if run_context is None:
                self._publish_event(params)
            else:
                self._publish_event(params, run_context=run_context)
        except Exception:
            _log.exception(
                "[worker-router] publish_event failed scope=%s type=%s",
                scope_key, params.get("type"),
            )
        self._project_clarify_approval_state(params)

    async def on_interactive_request(
        self,
        scope_key: str,
        conversation_id: str | InteractiveRequestFrame,
        frame: InteractiveRequestFrame | None = None,
    ) -> None:
        """Register the ``request_id → scope_key`` mapping so a later
        ``respond`` knows which worker to forward the answer to.

        Intentionally does NOT publish a ``{kind}.request`` event here.
        Rationale: the worker already publishes that event through the
        standard ``run_control.publish_recorded_event`` path — Phase
        5b.2 monkey-patches that publish to emit an ``EventFrame`` over
        stdout, which arrives on the main side as ``on_event`` and is
        re-published 1:1 by ``publish_recorded_event`` on the main
        sidecar. Synthesizing a second event here would duplicate the
        frontend card. The ``InteractiveRequestFrame`` is routing
        metadata only — it carries enough to track the request_id but
        not the full payload renderers expect."""
        if frame is None and isinstance(conversation_id, InteractiveRequestFrame):
            frame = conversation_id
            conversation_id = frame.stored_session_id
        if frame is None:
            return
        conversation = str(conversation_id or "")
        if frame.kind not in _INTERACTIVE_KINDS:
            _log.warning(
                "[worker-router] dropping interactive.request kind=%r request_id=%r",
                frame.kind, frame.request_id,
            )
            return
        stored = frame.stored_session_id
        if not stored:
            # No explicit stored_session_id — try cross-filling from the
            # run that's currently active for this scope (best-effort;
            # if multiple are concurrent, the answer routing still
            # works because we key by request_id, not session).
            stored = self._infer_stored_session_for_scope(scope_key, conversation)
            if not conversation:
                conversation = stored
        with self._lock:
            self._pending[frame.request_id] = _Pending(
                scope_key=scope_key,
                conversation_id=conversation,
                kind=frame.kind,
                stored_session_id=stored,
            )

    async def on_run_terminal(
        self,
        scope_key: str,
        conversation_id: str | RunTerminalFrame,
        frame: RunTerminalFrame | None = None,
    ) -> None:
        if frame is None and isinstance(conversation_id, RunTerminalFrame):
            frame = conversation_id
            conversation_id = frame.stored_session_id
        if frame is None:
            return
        conversation = str(conversation_id or "")
        stored = frame.stored_session_id
        turn_id = frame.turn_id
        with self._lock:
            info = self._runs.get(frame.run_id)
        if info is not None:
            stored = stored or info.stored_session_id
            turn_id = turn_id or info.turn_id
            if not conversation:
                conversation = info.conversation_id
        with self._lock:
            self._runs.pop(frame.run_id, None)
            # Also clear any pending interactive entries that were tied
            # to this run — worker is done, the response can't reach
            # the (dead) blocked thread anyway.
            stale_ids = [
                rid for rid, pending in self._pending.items()
                if (
                    pending.scope_key == scope_key
                    and pending.conversation_id == conversation
                    and pending.stored_session_id == stored
                )
            ]
            for rid in stale_ids:
                self._pending.pop(rid, None)

        if not stored:
            _log.warning(
                "[worker-router] run.terminal scope=%s run_id=%s status=%s "
                "has no stored_session_id — dropping (record_run_start was "
                "not called for this run_id)",
                scope_key, frame.run_id, frame.status,
            )
            return
        await self._publish_activity_terminal(scope_key, conversation, frame, info, stored)
        # NORMAL COMPLETION: the worker's agent code already published a
        # ``message.complete`` event through the monkey-patched publish
        # path (which arrived on the main side via ``on_event`` → re-
        # published 1:1). Re-publishing here would duplicate the terminal
        # frame on every chat turn. Only synthesize a terminal event for
        # ABNORMAL exits (cancel / failure / worker crash) — those don't
        # always reach the publish path because the agent thread may
        # have died before its own terminal emit.
        normalized_status = (frame.status or "").strip().lower()
        if normalized_status in ("", "completed", "success", "ok"):
            return
        try:
            self._publish_run_terminal(
                stored_session_id=stored,
                run_id=frame.run_id,
                turn_id=turn_id,
                runtime_scope_key=scope_key,
                runtime_session_id=stored,
                status=frame.status,
                message=frame.message,
            )
        except Exception:
            _log.exception(
                "[worker-router] publish_run_terminal failed scope=%s run_id=%s",
                scope_key, frame.run_id,
            )

    async def on_log(
        self,
        scope_key: str,
        conversation_id: str | LogFrame,
        frame: LogFrame | None = None,
    ) -> None:
        """Default sink — surface worker-side log frames into the main
        sidecar logger so they appear in the same stream as other
        gateway diagnostics."""
        if frame is None and isinstance(conversation_id, LogFrame):
            frame = conversation_id
            conversation_id = ""
        if frame is None:
            return
        _log.log(
            _level_for(frame.level),
            "[run-worker:%s:%s] %s", scope_key, conversation_id, frame.text,
        )

    # ── main→worker response routing ────────────────────────────────

    async def respond(
        self, request_id: str, answer: Any,
        *, expected_kind: Optional[str] = None,
    ) -> bool:
        """Forward a frontend ``*.respond`` answer to the right worker.

        ``expected_kind`` lets the caller assert the response matches
        the original request kind (e.g. ``clarify.respond`` only resolves
        a pending ``clarify``). Returns False if no pending request was
        found or the worker could not be reached.
        """
        request_id = str(request_id or "").strip()
        if not request_id:
            return False
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            if expected_kind is not None and pending.kind != expected_kind:
                _log.warning(
                    "[worker-router] kind mismatch on respond: request_id=%s "
                    "stored_kind=%s expected_kind=%s",
                    request_id, pending.kind, expected_kind,
                )
                return False
        ok = await self._sender.send(
            pending.scope_key,
            pending.conversation_id,
            InteractiveResponseFrame(
                kind=pending.kind, request_id=request_id, answer=answer,
            ),
        )
        if ok:
            with self._lock:
                # Worker may emit multiple events before terminal; the
                # entry is owned by the response leg, drop it once we
                # handed off so a duplicate ``respond`` returns False
                # (matches legacy "no pending answer request").
                self._pending.pop(request_id, None)
        return ok

    def has_pending_request(self, request_id: str) -> bool:
        with self._lock:
            return str(request_id or "") in self._pending

    def pending_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pendingInteractive": [
                    {
                        "requestId": rid,
                        "scopeKey": pending.scope_key,
                        "conversationId": pending.conversation_id,
                        "kind": pending.kind,
                        "storedSessionId": pending.stored_session_id,
                    }
                    for rid, pending in self._pending.items()
                ],
                "activeRuns": [
                    {
                        "runId": rid,
                        "scopeKey": info.scope_key,
                        "conversationId": info.conversation_id,
                        "storedSessionId": info.stored_session_id,
                        "turnId": info.turn_id,
                    }
                    for rid, info in self._runs.items()
                ],
            }

    # ── internals ────────────────────────────────────────────────────

    def _project_clarify_approval_state(self, params: dict[str, Any]) -> None:
        event_type = str(params.get("event_type") or params.get("type") or "")
        event_state = _CLARIFY_APPROVAL_EVENT_STATES.get(event_type)
        if event_state is None:
            return
        _kind_label, present = event_state
        session_key = str(
            params.get("stored_session_id")
            or params.get("session_id")
            or params.get("session_key")
            or ""
        ).strip()
        if not session_key:
            return
        try:
            from hermes_team_mission.runtime.approval_observer import (
                project_clarify_or_approval_state,
            )

            project_clarify_or_approval_state(
                session_key,
                present=present,
                source_event_type=event_type,
            )
        except Exception as exc:
            _log.warning(
                "[worker-router] clarify/approval projection failed event_type=%s: %s",
                event_type,
                exc,
            )

    def _infer_stored_session_for_scope(self, scope_key: str, conversation_id: str) -> str:
        """Best-effort: when an interactive.request arrives without an
        explicit ``stored_session_id``, look at the active runs for the
        scope. If exactly one run is active for this scope, use its
        stored_session_id; otherwise leave empty (the response leg
        still works because it routes by request_id, not session)."""
        candidates = [
            info.stored_session_id
            for info in self._runs.values()
            if info.scope_key == scope_key
            and (not conversation_id or info.conversation_id == (conversation_id or ""))
            and info.stored_session_id
        ]
        if len(candidates) == 1:
            return candidates[0]
        return ""

    def _run_context_for_event(self, params: dict[str, Any]) -> Any:
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        run_id = str(params.get("run_id") or payload.get("run_id") or "").strip()
        if not run_id:
            return None
        with self._lock:
            info = self._runs.get(run_id)
        if info is None or not info.run_context_json:
            return None
        try:
            from hermes_team_mission.domain.run_context import RunContext

            return RunContext.from_payload(info.run_context_json)
        except Exception as exc:
            _log.warning(
                "[worker-router] run_context_json parse failed run_id=%s: %s",
                run_id,
                exc,
            )
            return None

    def _capture_last_message_event(self, params: dict[str, Any]) -> None:
        if str(params.get("type") or "") != "message.complete":
            return
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        run_id = str(params.get("run_id") or payload.get("run_id") or "").strip()
        if not run_id:
            return
        with self._lock:
            info = self._runs.get(run_id)
            if info is not None:
                info.last_message_event = dict(params)

    async def _publish_activity_terminal(
        self,
        scope_key: str,
        conversation_id: str,
        frame: RunTerminalFrame,
        info: RunInfo | None,
        stored_session_id: str,
    ) -> None:
        if info is None or not info.dispatch_activity_id:
            return
        activity_id = info.dispatch_activity_id
        parent_scope_key = info.parent_scope_key
        parent_conversation_id = info.parent_conversation_id
        parent_hermes_home = info.parent_hermes_home
        token = None
        try:
            if parent_hermes_home:
                from tui_gateway.services.profile_context import enter_profile_context

                token = enter_profile_context(
                    {"hermes_home": parent_hermes_home, "runtime_scope_key": parent_scope_key}
                )
            from tui_gateway import server as _server

            db = _server._get_db()
            if db is None:
                return
            activity = db.get_activity(activity_id)
            if not activity:
                return
            status = _activity_status(frame.status)
            last_message = _last_message_from_event(info.last_message_event if info else None)
            if not last_message:
                last_message = _last_message_for_activity(db, stored_session_id)
            result_summary = _result_summary(last_message, frame.message)
            result_json = {
                "last_message": last_message,
                "usage": _usage_from_message(last_message),
                "run_id": frame.run_id,
            }
            updated_ok = False
            if status == "completed":
                updated_ok = db.mark_activity_completed(
                    activity_id,
                    result_summary=result_summary,
                    result_json=result_json,
                )
            elif status == "failed":
                error_message = result_summary or frame.message or "worker failed"
                mark_failed = getattr(db, "mark_activity_failed", None)
                if callable(mark_failed):
                    updated_ok = mark_failed(
                        activity_id,
                        error_message=error_message,
                        result_json=result_json,
                    )
                else:
                    updated_ok = db.update_activity_status(
                        activity_id,
                        "failed",
                        result_summary=error_message,
                        result_json=result_json,
                        completed_at=time.time(),
                    )
            elif status == "cancelled":
                cancel_summary = result_summary or frame.message or "cancelled"
                mark_cancelled = getattr(db, "mark_activity_cancelled", None)
                if callable(mark_cancelled):
                    updated_ok = mark_cancelled(
                        activity_id,
                        result_summary=cancel_summary,
                        result_json=result_json,
                    )
                else:
                    updated_ok = db.update_activity_status(
                        activity_id,
                        "cancelled",
                        result_summary=cancel_summary,
                        result_json=result_json,
                        completed_at=time.time(),
                    )
            updated = db.get_activity(activity_id) or activity
            persisted_status = str(updated.get("status") or status)
            event_result_summary = result_summary if updated_ok else str(updated.get("result_summary") or "")
            event_result_json = result_json if updated_ok else {}
            event = _activity_event_from_row(
                updated,
                status=persisted_status,
                run_id=frame.run_id,
                result_summary=event_result_summary,
                result_json=event_result_json,
            )
            try:
                self._publish_event(_activity_ws_frame(event), persist=False)
            except TypeError:
                self._publish_event(_activity_ws_frame(event))
            if parent_scope_key:
                await self._sender.send(
                    parent_scope_key,
                    parent_conversation_id,
                    ActivityEventFrame(kind="activity", event=event),
                )
        except Exception:
            _log.exception(
                "[worker-router] activity terminal handling failed scope=%s run_id=%s activity_id=%s",
                scope_key,
                frame.run_id,
                activity_id,
            )
        finally:
            if token is not None:
                try:
                    from tui_gateway.services.profile_context import leave_profile_context

                    leave_profile_context(token)
                except Exception:
                    pass


def _level_for(name: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(str(name or "").lower(), logging.INFO)


def _activity_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"", "completed", "success", "ok", "complete"}:
        return "completed"
    if normalized in {"cancelled", "canceled", "interrupted"}:
        return "cancelled"
    return "failed"


def _last_message_for_activity(db: Any, stored_session_id: str) -> dict[str, Any]:
    try:
        history_reader = getattr(db, "get_conversation_message_read_model", None)
        if not callable(history_reader):
            history_reader = db.get_messages_as_conversation
        messages = history_reader(stored_session_id)
    except Exception:
        return {}
    if not isinstance(messages, list):
        return {}
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = message.get("content")
        if role == "assistant" and content:
            return dict(message)
    for message in reversed(messages):
        if isinstance(message, dict):
            return dict(message)
    return {}


def _last_message_from_event(event: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text = str(payload.get("text") or payload.get("message") or "").strip()
    if not text:
        return {}
    message = {
        "role": "assistant",
        "content": text,
        "metadata": {
            "run_id": event.get("run_id") or payload.get("run_id"),
            "turn_id": event.get("turn_id") or payload.get("turn_id"),
            "status": payload.get("status"),
            "usage": payload.get("usage"),
            "source_event": "message.complete",
        },
    }
    return message


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content or "")


def _result_summary(last_message: dict[str, Any], fallback: str) -> str:
    text = _message_text(last_message).strip() or str(fallback or "").strip()
    return text[:200]


def _usage_from_message(last_message: dict[str, Any]) -> Any:
    if not isinstance(last_message, dict):
        return None
    metadata = last_message.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if isinstance(metadata, dict):
        return metadata.get("usage")
    return None


def _activity_event_from_row(
    row: dict[str, Any],
    *,
    status: str,
    run_id: str,
    result_summary: str,
    result_json: dict[str, Any],
) -> dict[str, Any]:
    return {
        "activity_id": str(row.get("activity_id") or ""),
        "status": status,
        "activity_kind": str(row.get("kind") or ""),
        "kind": f"activity.{status if status != 'completed' else 'completed'}",
        "target_profile_id": row.get("target_profile_id"),
        "target_mission_id": row.get("target_mission_id"),
        "target": row.get("target_profile_id") or row.get("target_mission_id") or "",
        "conversation_id": row.get("conversation_id"),
        "parent_activity_id": row.get("parent_activity_id"),
        "prompt_summary": row.get("prompt_summary"),
        "result_summary": row.get("result_summary") or result_summary,
        "result_json": row.get("result_json") or result_json,
        "started_at": row.get("started_at"),
        "dispatched_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
        "run_id": run_id,
    }


def _activity_ws_frame(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": event.get("kind") or "activity.completed",
        "session_id": str(event.get("conversation_id") or ""),
        "stored_session_id": str(event.get("conversation_id") or ""),
        "payload": event,
    }
