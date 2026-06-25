"""Singleton holder + lifecycle for the new ``WorkerSupervisor`` +
``WorkerFrameRouter``, plus the primary-mode dispatch entrypoint.

The legacy ``RuntimeWorkerPool`` exposes a module-level singleton via
``runtime_proxy_pool()``. This module mirrors that pattern for the
new stack so callers (``prompt.submit`` handler, ``*.respond``
handlers, ``runtime.status`` reporter) can grab a process-wide instance
without threading construction through every call site.

Initialization is **lazy** — neither the supervisor nor the router is
built until the first accessor call. That keeps two properties:

1. CLI tools and non-gateway entrypoints that import ``tui_gateway``
   sub-modules don't pay any cost.
2. Tests can clear the singletons between cases without leaving a
   running subprocess around.

The router is bound to the production ``run_control`` publish
functions lazily so this module stays import-cheap and so unit tests
can monkey-patch the publishers before the router is constructed.

Phase 5a (this file) ships ONLY the holder + env-flag detector. Phase
5b adds the worker-side agent run handler. Phase 5c wires
``prompt.submit`` to call ``router.record_run_start`` +
``supervisor.send(RunStartFrame)`` when primary mode is on. Phase 5d
adds the ``*.respond`` fast-path via ``router.respond``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import uuid
from typing import Any

from tui_gateway.run_worker import RunStartFrame
from tui_gateway.services.runtime_proxy import (
    RuntimeScope,
    runtime_scope_from_request,
)
from tui_gateway.services.worker_frame_router import WorkerFrameRouter
from tui_gateway.services.worker_supervisor import WorkerSupervisor

_log = logging.getLogger(__name__)


# Env flag values that route ``prompt.submit`` through the new stack
# instead of ``RuntimeWorkerPool``. ``legacy`` and ``""`` keep the old
# behavior; ``primary`` opts into the new path. Anything else is treated
# as ``legacy`` and a warning is logged once.
_ENV_KEY = "DOVIE_RUN_WORKER_MODE"
_LEGACY_VALUES = frozenset({"", "legacy", "off", "0", "false", "no"})
_PRIMARY_VALUES = frozenset({"primary", "on", "1", "true", "yes"})


_singleton_lock = threading.RLock()
_supervisor_singleton: Optional[WorkerSupervisor] = None
_router_singleton: Optional[WorkerFrameRouter] = None
_unknown_value_logged = False


def is_primary_run_worker_mode() -> bool:
    """Return True iff the env flag opts this process into the new
    run-worker stack. Default (unset) → False."""
    global _unknown_value_logged
    raw = str(os.environ.get(_ENV_KEY, "") or "").strip().lower()
    if raw in _PRIMARY_VALUES:
        return True
    if raw in _LEGACY_VALUES:
        return False
    if not _unknown_value_logged:
        _log.warning(
            "[worker-runtime] %s=%r is not a recognized value; "
            "treating as legacy. Use 'primary' to opt into the new path.",
            _ENV_KEY, raw,
        )
        _unknown_value_logged = True
    return False


def worker_supervisor() -> WorkerSupervisor:
    """Process-wide ``WorkerSupervisor`` singleton. Lazily constructed
    on first access. Callbacks are bound to the router (also lazy)
    so the supervisor never needs to know about ``run_control``
    directly."""
    global _supervisor_singleton
    with _singleton_lock:
        if _supervisor_singleton is None:
            router = worker_frame_router()
            _supervisor_singleton = WorkerSupervisor(
                on_event=router.on_event,
                on_interactive_request=router.on_interactive_request,
                on_run_terminal=router.on_run_terminal,
                on_log=router.on_log,
            )
        return _supervisor_singleton


def worker_frame_router() -> WorkerFrameRouter:
    """Process-wide ``WorkerFrameRouter`` singleton.

    Binds the production ``publish_recorded_event`` /
    ``publish_run_terminal_event`` from ``run_control`` and a sender
    stub that forwards to the supervisor singleton. The sender uses
    ``worker_supervisor()`` lazily — both directions are lazy so
    construction order between supervisor and router doesn't deadlock.
    """
    global _router_singleton
    with _singleton_lock:
        if _router_singleton is None:
            from tui_gateway.services import run_control  # late import — heavy module

            class _SupervisorSenderProxy:
                async def send(self, scope_key: str, frame):
                    return await worker_supervisor().send(scope_key, frame)

            _router_singleton = WorkerFrameRouter(
                sender=_SupervisorSenderProxy(),
                publish_event=run_control.publish_recorded_event,
                publish_run_terminal=run_control.publish_run_terminal_event,
            )
        return _router_singleton


async def shutdown_run_worker_runtime() -> None:
    """Terminate every running worker subprocess and clear the
    singletons. Safe to call multiple times; safe to call when nothing
    was spawned (no-op).

    Intended to run from the sidecar's async shutdown hook before the
    process exits. Sync atexit handlers can't drive this — they have
    no event loop — so this function is exposed for explicit wiring.
    """
    global _supervisor_singleton, _router_singleton
    supervisor: Optional[WorkerSupervisor]
    with _singleton_lock:
        supervisor = _supervisor_singleton
        _supervisor_singleton = None
        _router_singleton = None
    if supervisor is not None:
        try:
            await supervisor.shutdown_all()
        except Exception:
            _log.exception("[worker-runtime] shutdown_all raised")


async def primary_dispatch(req: Any, transport: Any) -> bool:
    """Phase 5c entry point: handle requests via the new run_worker
    stack instead of the legacy ``RuntimeWorkerPool`` proxy.

    Returns True if the request was handled (the caller must NOT also
    call ``proxy_to_runtime`` / dispatch locally). False = caller falls
    back to legacy handling.

    Scope:
    - ``prompt.submit`` on a scoped (non-default) profile → route to
      ``WorkerSupervisor`` + ``WorkerFrameRouter``
    - Anything else → False (unchanged)

    The env flag check is the caller's responsibility — this function
    assumes it's only called when primary mode is on. Centralizing the
    flag check here would require parsing every request twice."""
    if not isinstance(req, dict):
        return False
    method = str(req.get("method") or "").strip()
    # ``run.submit`` is the canonical entry the frontend sends (see
    # apps/desktop/src/services/hermes/gateway/runtime.ts). The
    # ``prompt.submit`` @method handler internally forwards to
    # ``run.submit`` after registry-reservation bookkeeping, so we
    # intercept both — the prompt.submit one catches any caller that
    # bypasses the desktop runtime client (CLI tools, tests).
    if method not in ("run.submit", "prompt.submit"):
        return False
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    scope = runtime_scope_from_request(req)
    if not scope.has_scope:
        # Default profile / no scope → in-process path; nothing to
        # route through worker.
        return False
    return await _dispatch_prompt_submit(req, transport, scope, params)


async def _dispatch_prompt_submit(
    req: dict, transport: Any, scope: RuntimeScope, params: dict,
) -> bool:
    rid = req.get("id")
    run_id = str(
        params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex
    ).strip()
    turn_id = str(params.get("turn_id") or uuid.uuid4().hex).strip()
    stored_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or ""
    ).strip()
    if not stored_session_id:
        await _ack_error(
            transport, rid, code=4006,
            message="stored_session_id or session_id required",
        )
        return True

    prompt_text = str(params.get("text") or "")

    supervisor = worker_supervisor()
    router = worker_frame_router()

    try:
        await supervisor.ensure(scope)
    except Exception as exc:
        _log.exception(
            "[worker-runtime] supervisor.ensure failed scope=%s",
            scope.runtime_scope_key,
        )
        await _ack_error(
            transport, rid, code=5021,
            message=f"primary worker spawn failed: {exc}",
        )
        return True

    router.record_run_start(
        scope_key=scope.runtime_scope_key,
        run_id=run_id,
        stored_session_id=stored_session_id,
        turn_id=turn_id,
    )

    ok = await supervisor.send(
        scope.runtime_scope_key,
        RunStartFrame(
            run_id=run_id,
            turn_id=turn_id,
            stored_session_id=stored_session_id,
            prompt=prompt_text,
            # Strip params we either already lifted or that are too
            # large to send over the JSON-line pipe. The worker re-
            # resolves anything it needs from its own session state.
            params={
                k: v for k, v in params.items()
                if k not in {
                    "text", "stored_session_id", "storedSessionId",
                    "session_id", "client_run_id", "run_id", "turn_id",
                }
            },
        ),
    )
    if not ok:
        router.forget_run(run_id)
        await _ack_error(
            transport, rid, code=5022,
            message="primary worker stdin write failed",
        )
        return True

    await _ack_ok(
        transport, rid,
        result={
            "status": "queued",
            "run_id": run_id,
            "turn_id": turn_id,
            "stored_session_id": stored_session_id,
            "runtime_scope_key": scope.runtime_scope_key,
            "source": "primary-run-worker",
        },
    )
    return True


async def _ack_ok(transport: Any, rid: Any, *, result: dict) -> None:
    await transport.write_async(
        {"jsonrpc": "2.0", "id": rid, "result": result}
    )


async def _ack_error(transport: Any, rid: Any, *, code: int, message: str) -> None:
    await transport.write_async(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    )


def _reset_for_tests() -> None:
    """Test-only helper. Drops the singletons WITHOUT terminating any
    running subprocesses (use ``shutdown_run_worker_runtime`` for that).
    Use sparingly — only when a test needs a fresh router/supervisor
    pair AND has already torn down any spawned workers itself."""
    global _supervisor_singleton, _router_singleton, _unknown_value_logged
    with _singleton_lock:
        _supervisor_singleton = None
        _router_singleton = None
        _unknown_value_logged = False
