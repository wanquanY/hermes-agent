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
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from tui_gateway.run_worker import (
    EventFrame,
    InteractiveRequestFrame,
    InteractiveResponseFrame,
    LogFrame,
    RunTerminalFrame,
)

_log = logging.getLogger(__name__)


_INTERACTIVE_KINDS = frozenset({"clarify", "approval", "secret", "sudo"})


@dataclass
class RunInfo:
    """Cross-fill source for run-terminal / event publishes when the
    worker doesn't echo the original ``stored_session_id`` / ``turn_id``
    in its frame. Populated by ``record_run_start`` from
    ``prompt.submit`` at run-create time."""

    scope_key: str
    stored_session_id: str
    turn_id: str
    run_context_json: Any = ""


@dataclass
class _Pending:
    scope_key: str
    kind: str
    stored_session_id: str


class _SupervisorSender(Protocol):
    """The subset of ``WorkerSupervisor`` ``WorkerFrameRouter`` calls.

    Kept narrow so tests can stub it without standing up a real
    subprocess."""

    async def send(self, scope_key: str, frame: Any) -> bool: ...


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
        run_id: str,
        stored_session_id: str,
        turn_id: str = "",
        run_context_json: Any = "",
    ) -> None:
        run_id = str(run_id or "").strip()
        if not run_id:
            return
        with self._lock:
            self._runs[run_id] = RunInfo(
                scope_key=str(scope_key or ""),
                stored_session_id=str(stored_session_id or ""),
                turn_id=str(turn_id or ""),
                run_context_json=run_context_json,
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
                stored_session_id=info.stored_session_id,
                turn_id=info.turn_id,
            ) if info is not None else None

    # ── WorkerSupervisor callbacks ──────────────────────────────────

    async def on_event(self, scope_key: str, frame: EventFrame) -> None:
        """Forward the 1:1 worker→main event payload to live subscribers
        + persist it. ``params`` is the same dict the legacy ws bridge
        used to put on the wire."""
        params = frame.params if isinstance(frame.params, dict) else {}
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

    async def on_interactive_request(
        self, scope_key: str, frame: InteractiveRequestFrame,
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
            stored = self._infer_stored_session_for_scope(scope_key)
        with self._lock:
            self._pending[frame.request_id] = _Pending(
                scope_key=scope_key,
                kind=frame.kind,
                stored_session_id=stored,
            )

    async def on_run_terminal(
        self, scope_key: str, frame: RunTerminalFrame,
    ) -> None:
        stored = frame.stored_session_id
        turn_id = frame.turn_id
        if not stored or not turn_id:
            with self._lock:
                info = self._runs.get(frame.run_id)
            if info is not None:
                stored = stored or info.stored_session_id
                turn_id = turn_id or info.turn_id
        with self._lock:
            self._runs.pop(frame.run_id, None)
            # Also clear any pending interactive entries that were tied
            # to this run — worker is done, the response can't reach
            # the (dead) blocked thread anyway.
            stale_ids = [
                rid for rid, pending in self._pending.items()
                if pending.scope_key == scope_key and pending.stored_session_id == stored
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

    async def on_log(self, scope_key: str, frame: LogFrame) -> None:
        """Default sink — surface worker-side log frames into the main
        sidecar logger so they appear in the same stream as other
        gateway diagnostics."""
        _log.log(
            _level_for(frame.level),
            "[run-worker:%s] %s", scope_key, frame.text,
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
                        "kind": pending.kind,
                        "storedSessionId": pending.stored_session_id,
                    }
                    for rid, pending in self._pending.items()
                ],
                "activeRuns": [
                    {
                        "runId": rid,
                        "scopeKey": info.scope_key,
                        "storedSessionId": info.stored_session_id,
                        "turnId": info.turn_id,
                    }
                    for rid, info in self._runs.items()
                ],
            }

    # ── internals ────────────────────────────────────────────────────

    def _infer_stored_session_for_scope(self, scope_key: str) -> str:
        """Best-effort: when an interactive.request arrives without an
        explicit ``stored_session_id``, look at the active runs for the
        scope. If exactly one run is active for this scope, use its
        stored_session_id; otherwise leave empty (the response leg
        still works because it routes by request_id, not session)."""
        candidates = [
            info.stored_session_id
            for info in self._runs.values()
            if info.scope_key == scope_key and info.stored_session_id
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


def _level_for(name: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(str(name or "").lower(), logging.INFO)
