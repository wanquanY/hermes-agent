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
    RunTerminalFrame        → terminate_run(...)

  main → worker
    *.respond handler       → router.respond(request_id, answer)
                              → WorkerSupervisor.send(scope_key,
                                                     InteractiveResponseFrame)

The routing tables are needed because the *.respond methods (Phase 6
moves these in-process) only carry the ``request_id``; the worker that
holds the blocked agent thread is identified solely through the table.

Phase 4c (this file) implements the router. Phase 5 ``prompt.submit``
populates ``record_run_start`` so terminal/event lookups can cross-fill
``conversation_session_id`` if the worker omits it. Phase 6 rewires the
``clarify.respond`` / ``approval.respond`` / ``secret.respond`` /
``sudo.respond`` handlers to call ``router.respond`` instead of the
legacy in-worker registry.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Optional, Protocol

from tui_gateway.run_worker import (
    ActivityEventFrame,
    EventFrame,
    InteractiveRequestFrame,
    InteractiveResponseFrame,
    LogFrame,
    RunTerminalFrame,
)
from tui_gateway.services.message_history import load_conversation_history

_log = logging.getLogger(__name__)


_INTERACTIVE_KINDS = frozenset({"clarify", "approval", "secret", "sudo"})
_CLARIFY_APPROVAL_EVENT_STATES: dict[str, tuple[str, bool]] = {
    "clarify.request": ("clarify", True),
    "clarify.resolved": ("clarify", False),
    "approval.request": ("approval", True),
    "approval.resolved": ("approval", False),
}
_INTERACTION_EVENT_TYPES: dict[str, tuple[str, str]] = {
    "clarify.request": ("clarify", "requested"),
    "approval.request": ("approval", "requested"),
    "sudo.request": ("sudo", "requested"),
    "secret.request": ("secret", "requested"),
    "clarify.resolved": ("clarify", "resolved"),
    "approval.resolved": ("approval", "resolved"),
    "sudo.resolved": ("sudo", "resolved"),
    "secret.resolved": ("secret", "resolved"),
    "clarify.expired": ("clarify", "expired"),
    "approval.expired": ("approval", "expired"),
    "sudo.expired": ("sudo", "expired"),
    "secret.expired": ("secret", "expired"),
}
def _payload_dict(params: dict[str, Any]) -> dict[str, Any]:
    payload = params.get("payload")
    return payload if isinstance(payload, dict) else {}


def _interaction_anchor_seq(params: dict[str, Any], payload: dict[str, Any]) -> int:
    for value in (
        payload.get("anchor_seq"),
        payload.get("anchorSeq"),
        params.get("anchor_seq"),
        params.get("anchorSeq"),
    ):
        try:
            seq = int(value or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq > 0:
            return seq
    return 0


@dataclass
class RunInfo:
    """Cross-fill source for run-terminal / event publishes when the
    worker doesn't echo the original ``conversation_session_id`` / ``turn_id``
    in its frame. Populated by ``record_run_start`` from
    ``prompt.submit`` at run-create time."""

    run_id: str
    scope_key: str
    conversation_id: str
    conversation_session_id: str
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
    conversation_session_id: str


# ── PendingRegistry: single source of truth for interactive request state ──
#
# PR-5 (I8): the respond contract needs a registry that tracks the full
# lifecycle of an interactive request — pending → resolved / expired — so a
# second respond to the same request_id can return ``already_resolved``
# (4409) instead of silently swallowing the answer or erroring with a
# generic "no pending" code.
#
# This registry is process-local (one per sidecar). The in-process
# ``prompt_respond`` @method handlers use it to enforce the three-state
# contract. ``WorkerFrameRouter`` has its own ``_pending`` dict for
# cross-process routing (request_id → scope_key) and does NOT use this
# registry — the router pops its entry on successful handoff, so the
# already-resolved state is meaningless there (the worker owns the wait).
#
# Thread-safety: all mutations go through ``_lock``. Reads are also locked
# because the dict can be mutated from the respond handler thread while a
# snapshot iterates.

# Default TTL for interactive requests (seconds). Matches the legacy
# ``_block(timeout=300)`` in ``session_config._block``.
_PENDING_TTL_SECONDS = 300.0


@dataclass
class PendingEntry:
    """One tracked interactive request."""

    request_id: str
    kind: str  # "clarify" | "approval" | "secret" | "sudo"
    conversation_id: str = ""
    session_key: str = ""
    scope_key: str = ""
    state: str = "pending"  # "pending" | "resolved" | "expired"
    anchor_seq: int = 0
    created_at: float = 0.0
    resolved_at: float = 0.0
    choice: Any = None


class PendingRegistry:
    """Process-local registry enforcing the three-state respond contract.

    States:
      pending  → created, awaiting a respond
      resolved → a respond succeeded; further responds return 4409
      expired  → TTL elapsed; further responds return 4404

    The registry is intentionally side-effect-free regarding the actual
    unblock primitive (threading.Event / worker handoff). Callers register
    a request, then on a successful resolve call ``mark_resolved``. The
    registry only tracks state so the contract can distinguish
    already-resolved from unknown.

    Event publishing (interaction.requested / interaction.resolved /
    interaction.expired) is optional: pass a ``publish_event`` callable
    and the registry will emit lifecycle frames on state transitions.
    The callable receives ``(event_type: str, entry: PendingEntry)`` and
    is responsible for durable persistence and delivery. Publish failures
    propagate; callers must not observe a local state transition that failed
    to persist.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _PENDING_TTL_SECONDS,
        publish_event: Any = None,
        clock: Any = None,
    ) -> None:
        self._entries: dict[str, PendingEntry] = {}
        self._lock = threading.RLock()
        self._ttl = float(ttl_seconds)
        self._publish_event = publish_event
        self._clock = clock if callable(clock) else time.monotonic

    def register(
        self,
        *,
        request_id: str,
        kind: str,
        conversation_id: str = "",
        session_key: str = "",
        scope_key: str = "",
        anchor_seq: int = 0,
    ) -> PendingEntry:
        """Register a new pending interactive request.

        If the request_id is already registered and still pending, this is
        a no-op (returns the existing entry). If it was already resolved/
        expired, a fresh pending entry replaces it (re-registration after
        expiry is allowed — the caller started a new blocking prompt with
        the same id).
        """
        rid = str(request_id or "").strip()
        if not rid:
            raise ValueError("request_id is required")
        now = self._clock()
        entry = PendingEntry(
            request_id=rid,
            kind=str(kind or "").strip(),
            conversation_id=str(conversation_id or ""),
            session_key=str(session_key or ""),
            scope_key=str(scope_key or ""),
            anchor_seq=max(0, int(anchor_seq or 0)),
            state="pending",
            created_at=now,
        )
        self._emit("interaction.requested", entry)
        with self._lock:
            self._entries[rid] = entry
        return entry

    def lookup(self, request_id: str) -> PendingEntry | None:
        """Return the entry for ``request_id`` after lazy expiry check.

        Returns ``None`` if the request_id is unknown. If the entry exists
        but has exceeded the TTL, it is flipped to ``expired`` in place
        before being returned (so the caller sees the true state).
        """
        rid = str(request_id or "").strip()
        if not rid:
            return None
        with self._lock:
            entry = self._entries.get(rid)
            if entry is None:
                return None
            if entry.state == "pending":
                self._maybe_expire_locked(entry)
            return entry

    def mark_resolved(self, request_id: str, choice: Any = None) -> bool:
        """Flip a pending entry to ``resolved``.

        Returns True if the entry was pending and is now resolved, False
        if it was unknown, already resolved, or expired. On a successful
        transition, the ``interaction.resolved`` event is published.
        """
        rid = str(request_id or "").strip()
        if not rid:
            return False
        with self._lock:
            entry = self._entries.get(rid)
            if entry is None:
                return False
            if entry.state == "pending":
                self._maybe_expire_locked(entry)
            if entry.state != "pending":
                return False
            resolved = replace(
                entry,
                state="resolved",
                resolved_at=self._clock(),
                choice=choice,
            )
            self._emit("interaction.resolved", resolved)
            entry.state = resolved.state
            entry.resolved_at = resolved.resolved_at
            entry.choice = resolved.choice
        return True

    def is_pending(self, request_id: str) -> bool:
        """True if the entry exists and is in the pending state."""
        entry = self.lookup(request_id)
        return entry is not None and entry.state == "pending"

    def is_known(self, request_id: str) -> bool:
        """True if the entry exists in any state (pending/resolved/expired)."""
        return self.lookup(request_id) is not None

    def resolved_choice(self, request_id: str) -> Any:
        """Return the choice stored at resolve time, or ``_MISSING`` if
        the entry is not in the resolved state."""
        entry = self.lookup(request_id)
        if entry is None or entry.state != "resolved":
            return _MISSING
        return entry.choice

    def clear(self, request_id: str = None) -> None:
        """Drop one entry (by id) or all entries. No events published."""
        with self._lock:
            if request_id is None:
                self._entries.clear()
            else:
                self._entries.pop(str(request_id or "").strip(), None)

    # ── internals ───────────────────────────────────────────────────

    def _maybe_expire_locked(self, entry: PendingEntry) -> None:
        """Lazy expiry: if a pending entry has exceeded the TTL, flip it
        to ``expired`` and publish the event. Caller holds ``_lock``."""
        if entry.state != "pending":
            return
        if self._ttl <= 0:
            return
        if (self._clock() - entry.created_at) <= self._ttl:
            return
        expired = replace(entry, state="expired", resolved_at=self._clock())
        self._emit("interaction.expired", expired)
        entry.state = expired.state
        entry.resolved_at = expired.resolved_at

    def _emit(self, event_type: str, entry: PendingEntry) -> None:
        if self._publish_event is None:
            return
        self._publish_event(event_type, entry)


# Sentinel for "no resolved choice" — distinguishes a resolved choice of
# ``None`` / ``""`` (a real deny/empty answer) from "entry not resolved".
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<MISSING>"


_MISSING = _Missing()


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
        persist_interaction_event: Any = None,
    ) -> None:
        self._sender = sender
        # Injected so unit tests don't touch the global event-publish
        # singletons in ``run_control``. In production these are bound
        # to ``run_control.publish_recorded_event`` and
        # ``run_control.publish_run_terminal_event``.
        self._publish_event = publish_event
        self._publish_run_terminal = publish_run_terminal
        self._persist_interaction_event = persist_interaction_event
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
        conversation_session_id: str,
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
                conversation_id=str(conversation_id or conversation_session_id or ""),
                conversation_session_id=str(conversation_session_id or ""),
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
                conversation_session_id=info.conversation_session_id,
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
                    conversation_session_id=info.conversation_session_id,
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
        if self._publish_interaction_frame(params, scope_key, conversation, run_context):
            self._project_clarify_approval_state(params)
            return
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

    def _publish_interaction_frame(
        self,
        params: dict[str, Any],
        scope_key: str,
        conversation_id: str,
        run_context: Any = None,
    ) -> bool:
        event_type = str(params.get("type") or "").strip()
        mapped = _INTERACTION_EVENT_TYPES.get(event_type)
        if mapped is None:
            return False
        kind, status = mapped
        payload = _payload_dict(params)
        request_id = str(
            payload.get("request_id")
            or payload.get("requestId")
            or params.get("request_id")
            or params.get("requestId")
            or payload.get("id")
            or ""
        ).strip()
        if not request_id:
            _log.warning(
                "[worker-router] dropping interaction frame without request_id type=%s scope_key=%s",
                event_type,
                scope_key,
            )
            return True
        conversation_session_id = str(
            params.get("conversation_session_id")
            or params.get("conversationSessionId")
            or payload.get("conversation_session_id")
            or payload.get("conversationSessionId")
            or payload.get("session_key")
            or conversation_id
            or params.get("session_id")
            or ""
        ).strip()
        execution_session_id = str(params.get("session_id") or payload.get("session_id") or payload.get("sessionId") or "").strip()
        runtime_scope_key = str(
            params.get("runtime_scope_key")
            or params.get("runtimeScopeKey")
            or payload.get("runtime_scope_key")
            or payload.get("runtimeScopeKey")
            or scope_key
            or ""
        ).strip()
        interaction_payload = {
            **payload,
            "request_id": request_id,
            "kind": kind,
            "status": "pending" if status == "requested" else status,
            "source_event_type": event_type,
            "source_event": dict(params),
        }
        anchor_seq = _interaction_anchor_seq(params, payload)
        interaction_payload["anchor_seq"] = anchor_seq
        entry = PendingEntry(
            request_id=request_id,
            kind=kind,
            conversation_id=execution_session_id or conversation_id or conversation_session_id,
            session_key=conversation_session_id,
            scope_key=runtime_scope_key,
            state="pending" if status == "requested" else status,
            anchor_seq=anchor_seq,
            choice=payload.get("choice"),
        )
        if self._persist_interaction_event is not None:
            self._persist_interaction_event(f"interaction.{status}", entry)
        frame = {
            "type": f"interaction.{status}",
            "kind": kind,
            "request_id": request_id,
            "conversation_session_id": conversation_session_id,
            "session_id": execution_session_id,
            "runtime_scope_key": runtime_scope_key,
            "conversation_id": conversation_id or conversation_session_id,
            "run_id": str(params.get("run_id") or payload.get("run_id") or payload.get("runId") or ""),
            "turn_id": str(params.get("turn_id") or payload.get("turn_id") or payload.get("turnId") or ""),
            "seq": int(params.get("seq") or 0),
            "payload": interaction_payload,
        }
        try:
            if run_context is None:
                self._publish_event(frame, persist=False)
            else:
                self._publish_event(frame, persist=False, run_context=run_context)
        except TypeError:
            self._publish_event(frame)
        except Exception:
            _log.exception(
                "[worker-router] interaction publish failed type=%s request_id=%s",
                event_type,
                request_id,
            )
        return True

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
            conversation_id = frame.conversation_session_id
        if frame is None:
            return
        conversation = str(conversation_id or "")
        request_id = str(frame.request_id or "").strip()
        if frame.kind not in _INTERACTIVE_KINDS:
            _log.warning(
                "[worker-router] dropping interactive.request kind=%r request_id=%r",
                frame.kind, request_id,
            )
            return
        if not request_id:
            _log.warning(
                "[worker-router] dropping interactive.request without request_id "
                "kind=%s scope_key=%s conversation_id=%s conversation_session_id=%s",
                frame.kind,
                scope_key,
                conversation,
                frame.conversation_session_id,
            )
            return
        stored = frame.conversation_session_id
        if not stored:
            # No explicit conversation_session_id — try cross-filling from the
            # run that's currently active for this scope (best-effort;
            # if multiple are concurrent, the answer routing still
            # works because we key by request_id, not session).
            stored = self._infer_stored_session_for_scope(scope_key, conversation)
            if not conversation:
                conversation = stored
        with self._lock:
            self._pending[request_id] = _Pending(
                scope_key=scope_key,
                conversation_id=conversation,
                kind=frame.kind,
                conversation_session_id=stored,
            )

    async def on_run_terminal(
        self,
        scope_key: str,
        conversation_id: str | RunTerminalFrame,
        frame: RunTerminalFrame | None = None,
    ) -> None:
        if frame is None and isinstance(conversation_id, RunTerminalFrame):
            frame = conversation_id
            conversation_id = frame.conversation_session_id
        if frame is None:
            return
        conversation = str(conversation_id or "")
        stored = frame.conversation_session_id
        turn_id = frame.turn_id
        with self._lock:
            info = self._runs.get(frame.run_id)
        if info is not None:
            stored = stored or info.conversation_session_id
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
                    and pending.conversation_session_id == stored
                )
            ]
            for rid in stale_ids:
                self._pending.pop(rid, None)

        if not stored:
            _log.warning(
                "[worker-router] run.terminal scope=%s run_id=%s status=%s "
                "has no conversation_session_id — dropping (record_run_start was "
                "not called for this run_id)",
                scope_key, frame.run_id, frame.status,
            )
            return
        await self._publish_activity_terminal(scope_key, conversation, frame, info, stored)
        normalized_status = (frame.status or "").strip().lower()
        terminal_status = (
            "completed"
            if normalized_status in ("", "completed", "success", "ok")
            else "cancelled"
            if normalized_status in ("cancelled", "canceled")
            else "interrupted"
            if normalized_status == "interrupted"
            else "failed"
        )
        try:
            terminal_activity_id = ""
            if info is not None:
                terminal_activity_id = str(info.dispatch_activity_id or "").strip()
                if not terminal_activity_id and info.run_context_json:
                    try:
                        from hermes_team_mission.domain.run_context import RunContext

                        terminal_activity_id = str(
                            RunContext.from_payload(info.run_context_json).activity_id or ""
                        ).strip()
                    except Exception:
                        terminal_activity_id = ""
            self._publish_run_terminal(
                conversation_session_id=stored,
                run_id=frame.run_id,
                turn_id=turn_id,
                runtime_scope_key=scope_key,
                execution_session_id=stored,
                activity_id=terminal_activity_id,
                status=terminal_status,
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
                kind=pending.kind,
                request_id=request_id,
                answer=answer,
                conversation_session_id=pending.conversation_session_id,
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
                        "conversationSessionId": pending.conversation_session_id,
                    }
                    for rid, pending in self._pending.items()
                ],
                "activeRuns": [
                    {
                        "runId": rid,
                        "scopeKey": info.scope_key,
                        "conversationId": info.conversation_id,
                        "conversationSessionId": info.conversation_session_id,
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
            params.get("conversation_session_id")
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
        explicit ``conversation_session_id``, look at the active runs for the
        scope. If exactly one run is active for this scope, use its
        conversation_session_id; otherwise leave empty (the response leg
        still works because it routes by request_id, not session)."""
        candidates = [
            info.conversation_session_id
            for info in self._runs.values()
            if info.scope_key == scope_key
            and (not conversation_id or info.conversation_id == (conversation_id or ""))
            and info.conversation_session_id
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
        conversation_session_id: str,
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
            activity = db.activities.get(activity_id)
            if not activity:
                return
            status = _activity_status(frame.status)
            last_message = _last_message_from_event(info.last_message_event if info else None)
            if not last_message:
                last_message = _last_message_for_activity(db, conversation_session_id)
            result_summary = _result_summary(last_message, frame.message)
            result_json = {
                "last_message": last_message,
                "usage": _usage_from_message(last_message),
                "run_id": frame.run_id,
            }
            updated_ok = False
            if status == "completed":
                updated_ok = db.activities.mark_completed(
                    activity_id,
                    result_summary=result_summary,
                    result_json=result_json,
                )
            elif status == "failed":
                error_message = result_summary or frame.message or "worker failed"
                updated_ok = db.activities.mark_failed(
                    activity_id,
                    error_message=error_message,
                    result_json=result_json,
                )
            elif status == "cancelled":
                cancel_summary = result_summary or frame.message or "cancelled"
                updated_ok = db.activities.mark_cancelled(
                    activity_id,
                    result_summary=cancel_summary,
                    result_json=result_json,
                )
            updated = db.activities.get(activity_id) or activity
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
                    _log.exception(
                        "[worker-router] failed to leave profile context after activity terminal handling"
                    )


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


def _last_message_for_activity(db: Any, conversation_session_id: str) -> dict[str, Any]:
    try:
        messages = load_conversation_history(db, conversation_session_id)
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
        "conversation_session_id": str(event.get("conversation_id") or ""),
        "payload": event,
    }
