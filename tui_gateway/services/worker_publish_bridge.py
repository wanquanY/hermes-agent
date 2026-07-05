"""Monkey-patches that redirect worker-side publish points to the
``run_worker`` stdout protocol.

The legacy worker published events through
``run_control.publish_recorded_event`` and registered interactive
requests through ``tools/clarify_gateway.register`` /
``tools/approval.submit_pending`` /
``tools/approval.register_gateway_notify``. Each of those wrote into
in-process state that was then surfaced over the sub-sidecar's
websocket bridge.

In the new architecture the worker has no websocket. It instead
needs to ship every event + interactive request out as a JSON line
on stdout. ``install_publish_bridge`` rewires the publish/register
call sites to do exactly that **without changing call-site code in
the agent or its tools** — the patches preserve the original side
effects (DB persistence, in-worker registries that the agent thread
blocks on) and only add the stdout emit.

The bridge is **thread-safe**: the agent runs on a background thread
and may call publish from there. ``emit`` is dispatched onto the
worker's asyncio loop via ``run_coroutine_threadsafe`` so concurrent
publishes serialize on the same stdout writer the protocol loop uses.

Phase 5b.2 (this file) ships the bridge as a standalone, fully unit-
testable module. The ``AgentRunBackend`` installs it on
``RunStartFrame`` and uninstalls on completion.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from tui_gateway.run_worker import (
    EventFrame,
    InteractiveRequestFrame,
    OutgoingFrame,
)

_log = logging.getLogger(__name__)


# Emit signature provided by ``WorkerProtocol.emit``: awaitable that
# enqueues a frame on the same stdout writer the protocol uses.
EmitAsync = Callable[[OutgoingFrame], Awaitable[None]]
_active_run_context_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "worker_active_run_context",
    default=None,
)
_active_run_context_lock = threading.RLock()
_active_run_context: Any = None


# Map ``server._block`` event types to the ``InteractiveRequestFrame``
# kind the main router tracks pending responses under. Mirrors
# ``WorkerInteractiveResponder._BUILTINS`` / the kinds the router
# accepts in ``_INTERACTIVE_KINDS``.
_BLOCK_EVENT_KINDS = {
    "clarify.request": "clarify",
    "approval.request": "approval",
    "secret.request": "secret",
    "sudo.request": "sudo",
}


def _block_event_interactive_kind(event_type: str) -> Optional[str]:
    return _BLOCK_EVENT_KINDS.get(event_type)


def _payload_keys(payload: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in payload.keys())[:24]


def _choices_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


@dataclass
class _PatchHandle:
    """Records what was patched so ``uninstall`` can undo it. One
    handle per call site so partial install (e.g. publish hook
    succeeds but clarify hook fails) doesn't leak unrestored modules.
    """

    module: Any
    attr_name: str
    original: Any


@dataclass
class _RunContextHandle:
    token: contextvars.Token[Any]
    previous: Any


def get_active_run_context() -> Any:
    """Return the worker process's currently active RunContext.

    The runner thread sets a ContextVar for same-thread call paths and a
    process-active fallback for the legacy agent thread spawned under
    ``_execute_prompt_submit``. The worker process runs one active agent
    at a time, matching ``AgentRunBackend``'s concurrency guard.
    """
    context = _active_run_context_var.get()
    if context is not None:
        return context
    with _active_run_context_lock:
        return _active_run_context


def _push_active_run_context(run_context: Any) -> _RunContextHandle:
    global _active_run_context
    token = _active_run_context_var.set(run_context)
    with _active_run_context_lock:
        previous = _active_run_context
        _active_run_context = run_context
    return _RunContextHandle(token=token, previous=previous)


def _pop_active_run_context(handle: _RunContextHandle | None) -> None:
    global _active_run_context
    if handle is None:
        return
    try:
        _active_run_context_var.reset(handle.token)
    except Exception:
        _log.debug("[worker-publish-bridge] active run context reset failed", exc_info=True)
    with _active_run_context_lock:
        _active_run_context = handle.previous


class WorkerPublishBridge:
    """Owns the set of installed monkey-patches for one run.

    Use one instance per ``RunStartFrame``. ``install()`` and
    ``uninstall()`` are not symmetric across instances — installing
    twice for the same target module will not stack; the bridge
    asserts on double-install.
    """

    def __init__(self, *, emit: EmitAsync, loop: asyncio.AbstractEventLoop) -> None:
        self._emit = emit
        self._loop = loop
        self._handles: list[_PatchHandle] = []
        self._lock = threading.RLock()
        self._installed = False
        self._stored_session_id: str = ""
        self._active_context_handle: _RunContextHandle | None = None

    # ── public API ───────────────────────────────────────────────────

    def install(self, *, stored_session_id: str = "", run_context: Any = None) -> None:
        with self._lock:
            if self._installed:
                raise RuntimeError(
                    "WorkerPublishBridge.install: already installed; "
                    "build a new bridge per run instead of reusing."
                )
            self._stored_session_id = stored_session_id
            if run_context is not None:
                self._active_context_handle = _push_active_run_context(run_context)
            self._install_publish_hook()
            self._install_clarify_hook()
            self._install_approval_hooks()
            self._installed = True

    def uninstall(self) -> None:
        with self._lock:
            if not self._installed:
                return
            # Restore in reverse install order so wrappers chained on
            # the same attr unwind cleanly.
            for handle in reversed(self._handles):
                try:
                    setattr(handle.module, handle.attr_name, handle.original)
                except Exception:
                    _log.exception(
                        "[worker-publish-bridge] uninstall failed for %s.%s",
                        handle.module.__name__, handle.attr_name,
                    )
            self._handles.clear()
            _pop_active_run_context(self._active_context_handle)
            self._active_context_handle = None
            self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed

    # ── thread-safe emit ─────────────────────────────────────────────

    def emit_threadsafe(self, frame: OutgoingFrame) -> None:
        """Schedule ``self._emit(frame)`` on the worker's event loop
        from any thread. Returns immediately; the actual write may not
        have flushed yet.

        Errors are logged but never propagated — a partial publish
        must not crash the agent thread that called publish."""
        if self._loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._emit(frame), self._loop)
        except RuntimeError:
            # Loop has been closed between our check and the schedule.
            pass
        except Exception:
            _log.exception("[worker-publish-bridge] emit_threadsafe failed")

    # ── per-hook installers ─────────────────────────────────────────

    def _event_with_active_run_context(self, params: dict) -> dict:
        if not isinstance(params, dict):
            return params
        run_context = get_active_run_context()
        participant_id = str(getattr(run_context, "participant_id", "") or "").strip()
        activity_id = str(getattr(run_context, "activity_id", "") or "").strip()
        if not participant_id and not activity_id:
            return params
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        existing_participant_id = str(
            params.get("participant_id")
            or params.get("participantId")
            or payload.get("participant_id")
            or payload.get("participantId")
            or ""
        ).strip()
        existing_activity_id = str(
            params.get("activity_id")
            or params.get("activityId")
            or payload.get("activity_id")
            or payload.get("activityId")
            or ""
        ).strip()
        if (not participant_id or existing_participant_id) and (not activity_id or existing_activity_id):
            return params
        stamped = dict(params)
        stamped_payload = dict(payload)
        if participant_id and not existing_participant_id:
            stamped["participant_id"] = participant_id
            stamped["participantId"] = participant_id
            stamped_payload["participant_id"] = participant_id
            stamped_payload["participantId"] = participant_id
        if activity_id and not existing_activity_id:
            stamped["activity_id"] = activity_id
            stamped["activityId"] = activity_id
            stamped_payload["activity_id"] = activity_id
            stamped_payload["activityId"] = activity_id
        stamped["payload"] = stamped_payload
        return stamped

    def _install_publish_hook(self) -> None:
        """Wrap ``run_control.publish_recorded_event`` so every event
        the agent emits is *also* sent out as an ``EventFrame`` on
        stdout. The original side effects (record_event → DB persist,
        subscriber delivery on whatever ``_subscriptions_by_id`` exists
        in this process) are preserved."""
        try:
            from tui_gateway.services import run_control
        except Exception:
            _log.warning(
                "[worker-publish-bridge] run_control unavailable — skipping publish hook"
            )
            return
        if not hasattr(run_control, "publish_recorded_event"):
            return
        original = run_control.publish_recorded_event
        bridge = self

        def wrapped(params: dict, *args, **kwargs):
            # Emit to stdout BEFORE the original so a slow DB write
            # can't block the frame leaving — the main side's router
            # is the source of truth for live subscribers; the DB
            # persist is the canonical truth that gets read on replay.
            if isinstance(params, dict):
                params = bridge._event_with_active_run_context(params)
                bridge.emit_threadsafe(EventFrame(params=dict(params)))
                # The Dovie-native blocking primitive (``server._block``)
                # bypasses ``tools/clarify_gateway.register`` /
                # ``tools/approval.submit_pending`` (where the dedicated
                # clarify/approval hooks live) and writes its rid into
                # the worker process's own ``server._pending`` dict
                # instead, then publishes a regular
                # ``clarify.request`` / ``approval.request`` /
                # ``secret.request`` / ``sudo.request`` event. The main
                # sidecar has no way to look up that rid (the dict
                # lives in the worker process) so the frontend's
                # ``clarify.respond`` errors with
                # ``4009 no pending answer request``. Mirror the
                # ``InteractiveRequestFrame`` emission the
                # clarify/approval hooks already do — same plumbing,
                # routes the request_id → scope_key map into the main
                # router so ``primary_dispatch``'s ``*.respond``
                # interceptor can find it.
                event_type = str(params.get("type") or "")
                interactive_kind = _block_event_interactive_kind(event_type)
                if interactive_kind is not None:
                    payload = params.get("payload")
                    payload_dict = payload if isinstance(payload, dict) else {}
                    request_id = str(
                        payload_dict.get("request_id")
                        or payload_dict.get("requestId")
                        or params.get("request_id")
                        or ""
                    ).strip()
                    if request_id:
                        stored = str(
                            params.get("stored_session_id")
                            or params.get("session_id")
                            or bridge._stored_session_id
                            or ""
                        ).strip()
                        bridge.emit_threadsafe(
                            InteractiveRequestFrame(
                                kind=interactive_kind,
                                request_id=request_id,
                                payload=dict(payload_dict) if payload_dict else {},
                                stored_session_id=stored,
                            )
                        )
                    else:
                        _log.warning(
                            "[worker-publish-bridge] interactive request missing request_id "
                            "kind=%s event_type=%s stored_session_id=%s session_id=%s "
                            "run_id=%s turn_id=%s payload_keys=%s has_question=%s choices_count=%s",
                            interactive_kind,
                            event_type,
                            str(
                                params.get("stored_session_id")
                                or params.get("session_key")
                                or ""
                            ).strip(),
                            str(params.get("session_id") or "").strip(),
                            str(params.get("run_id") or "").strip(),
                            str(params.get("turn_id") or "").strip(),
                            _payload_keys(payload_dict),
                            bool(str(payload_dict.get("question") or "").strip()),
                            _choices_count(payload_dict.get("choices")),
                        )
            # R1: the single-writer invariant is enforced at
            # ``record_event`` via ``tui_gateway.process_role``. This
            # wrapper does not need to defensively strip ``persist``
            # anymore — the worker-side ``record_event`` will refuse to
            # persist regardless of what ``original`` is given.
            return original(params, *args, **kwargs)

        run_control.publish_recorded_event = wrapped  # type: ignore[assignment]
        self._handles.append(
            _PatchHandle(module=run_control, attr_name="publish_recorded_event", original=original)
        )

    def _install_clarify_hook(self) -> None:
        """Wrap ``tools/clarify_gateway.register`` so every clarify
        request the agent registers also routes through stdout for
        request_id → scope_key bookkeeping on the main side.

        The original ``register`` returns a ``_ClarifyEntry`` whose
        ``event`` (threading.Event) the agent thread blocks on. We
        keep that behavior — the entry still lives in the worker's
        ``_entries`` dict. The InteractiveResponseFrame route from
        Phase 5b.1 unblocks it on the main side's behalf."""
        try:
            from tools import clarify_gateway
        except Exception:
            _log.debug(
                "[worker-publish-bridge] tools.clarify_gateway not importable — skipping"
            )
            return
        if not hasattr(clarify_gateway, "register"):
            return
        original = clarify_gateway.register
        bridge = self

        def wrapped(clarify_id, session_key, question, choices):
            entry = original(clarify_id, session_key, question, choices)
            request_id = str(clarify_id)
            payload: dict[str, Any] = {
                "clarify_id": clarify_id,
                "request_id": request_id,
                "session_key": session_key,
                "question": question,
            }
            if choices:
                payload["choices"] = list(choices)
            _log.info(
                "[worker-publish-bridge] clarify gateway request registered "
                "request_id=%s session_key=%s stored_session_id=%s payload_keys=%s "
                "has_question=%s choices_count=%s",
                request_id,
                str(session_key or "").strip(),
                bridge._stored_session_id or str(session_key or "").strip(),
                _payload_keys(payload),
                bool(str(question or "").strip()),
                _choices_count(payload.get("choices")),
            )
            bridge.emit_threadsafe(
                InteractiveRequestFrame(
                    kind="clarify",
                    request_id=request_id,
                    payload=payload,
                    stored_session_id=bridge._stored_session_id or str(session_key or ""),
                )
            )
            return entry

        clarify_gateway.register = wrapped  # type: ignore[assignment]
        self._handles.append(
            _PatchHandle(module=clarify_gateway, attr_name="register", original=original)
        )

    def _install_approval_hooks(self) -> None:
        """Wrap ``tools/approval.submit_pending`` AND
        ``tools/approval.register_gateway_notify`` so blocking and
        non-blocking approval flows both route through stdout.

        - ``submit_pending(session_key, approval)`` → adds to
          ``_pending`` (single-slot per session)
        - ``register_gateway_notify(session_key, cb)`` + later
          ``_gateway_queues`` append → the FIFO blocking flow
        The InteractiveRequestFrame we emit unifies both: kind=approval,
        request_id=session_key (matches legacy ``approval.respond``
        resolution semantics)."""
        try:
            from tools import approval
        except Exception:
            _log.debug(
                "[worker-publish-bridge] tools.approval not importable — skipping"
            )
            return
        if hasattr(approval, "submit_pending"):
            original_submit = approval.submit_pending
            bridge = self

            def wrapped_submit(session_key, approval_data):
                result = original_submit(session_key, approval_data)
                payload = dict(approval_data) if isinstance(approval_data, dict) else {"data": approval_data}
                bridge.emit_threadsafe(
                    InteractiveRequestFrame(
                        kind="approval",
                        request_id=str(session_key),
                        payload=payload,
                        stored_session_id=bridge._stored_session_id or str(session_key or ""),
                    )
                )
                return result

            approval.submit_pending = wrapped_submit  # type: ignore[assignment]
            self._handles.append(
                _PatchHandle(module=approval, attr_name="submit_pending", original=original_submit)
            )


def install_for_run(
    *,
    emit: EmitAsync,
    loop: asyncio.AbstractEventLoop,
    stored_session_id: str = "",
    run_context: Any = None,
) -> WorkerPublishBridge:
    """Convenience factory: build + install in one call. Returns the
    bridge so the caller can ``uninstall()`` it at run end."""
    bridge = WorkerPublishBridge(emit=emit, loop=loop)
    bridge.install(stored_session_id=stored_session_id, run_context=run_context)
    return bridge
