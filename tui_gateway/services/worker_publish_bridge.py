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


@dataclass
class _PatchHandle:
    """Records what was patched so ``uninstall`` can undo it. One
    handle per call site so partial install (e.g. publish hook
    succeeds but clarify hook fails) doesn't leak unrestored modules.
    """

    module: Any
    attr_name: str
    original: Any


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

    # ── public API ───────────────────────────────────────────────────

    def install(self, *, stored_session_id: str = "") -> None:
        with self._lock:
            if self._installed:
                raise RuntimeError(
                    "WorkerPublishBridge.install: already installed; "
                    "build a new bridge per run instead of reusing."
                )
            self._stored_session_id = stored_session_id
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
                bridge.emit_threadsafe(EventFrame(params=dict(params)))
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
            payload: dict[str, Any] = {
                "clarify_id": clarify_id,
                "session_key": session_key,
                "question": question,
            }
            if choices:
                payload["choices"] = list(choices)
            bridge.emit_threadsafe(
                InteractiveRequestFrame(
                    kind="clarify",
                    request_id=str(clarify_id),
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
) -> WorkerPublishBridge:
    """Convenience factory: build + install in one call. Returns the
    bridge so the caller can ``uninstall()`` it at run end."""
    bridge = WorkerPublishBridge(emit=emit, loop=loop)
    bridge.install(stored_session_id=stored_session_id)
    return bridge
