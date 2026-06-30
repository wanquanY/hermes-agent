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

import asyncio
import logging
import os
import threading
import time
from typing import Optional

import uuid
from typing import Any

from agent.dovie_diagnostics import emit_dovie_runtime_diagnostic
from tui_gateway.run_worker import RunCancelFrame, RunStartFrame
from tui_gateway.services.runtime_proxy import (
    RuntimeScope,
    runtime_scope_from_request,
)
from tui_gateway.services.workspace import session_workspace_run_context
from tui_gateway.services.worker_frame_router import WorkerFrameRouter
from tui_gateway.services.worker_pool import WorkerPool
from tui_gateway.services.worker_supervisor import WorkerSupervisor

_log = logging.getLogger(__name__)


_singleton_lock = threading.RLock()
_supervisor_singleton: Optional[WorkerSupervisor] = None
_router_singleton: Optional[WorkerFrameRouter] = None
_pool_singleton: Optional[WorkerPool] = None
_runtime_loop: Optional[asyncio.AbstractEventLoop] = None


def _worker_run_log(stage: str, **fields: Any) -> None:
    emit_dovie_runtime_diagnostic("dovie-worker-run", stage, fields)


class ControlPlaneTransport:
    """In-process transport for internal control-plane dispatch.

    Worker runtime commands such as Team Mission node starts need the
    ``primary_dispatch`` worker-spawn path, but they are not UI JSON-RPCs and
    must not depend on a WebSocket staying connected long enough to receive an
    acknowledgement. This transport captures ack frames for diagnostics while
    making command acceptance independent from renderer connection lifetime.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._created_at = time.time()
        self._frames: list[dict[str, Any]] = []

    def _diagnostics(self) -> dict[str, Any]:
        return {
            "transport": "ControlPlaneTransport",
            "age_s": round(time.time() - self._created_at, 3),
            "closed": False,
            "sent_count": len(self._frames),
        }

    @property
    def frames(self) -> list[dict[str, Any]]:
        return list(self._frames)

    def write(self, obj: dict) -> bool:
        self._frames.append(dict(obj or {}))
        return True

    async def write_async(self, obj: dict) -> bool:
        self._frames.append(dict(obj or {}))
        return True


def remember_worker_runtime_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Record the event loop that owns ``WorkerSupervisor`` operations."""
    global _runtime_loop
    try:
        candidate = loop or asyncio.get_running_loop()
    except RuntimeError:
        return
    if candidate.is_running() and not candidate.is_closed():
        _runtime_loop = candidate


def current_worker_runtime_loop() -> asyncio.AbstractEventLoop | None:
    loop = _runtime_loop
    if loop is not None and loop.is_running() and not loop.is_closed():
        return loop
    return None


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

    ``publish_event`` is wrapped to RESOLVE the per-profile DB from
    ``params["stored_session_id"]`` BEFORE calling
    ``publish_recorded_event``. Without this, ``db`` defaults to None
    and the run-control persist path drops the event with
    ``terminal-event-not-persisted-no-db-method`` — and worse, the
    subscriber fanout misses the event because no in-memory
    ``_events_by_session`` entry is built. The router runs outside
    any RPC handler context so the ContextVar-based
    ``_active_hermes_home`` chain ``_get_db`` would follow isn't set;
    explicit resolution via ``_db_for_stable_session`` is required.
    """
    global _router_singleton
    with _singleton_lock:
        if _router_singleton is None:
            from tui_gateway.services import run_control  # late import — heavy module
            from tui_gateway import server as _server

            class _SupervisorSenderProxy:
                async def send(self, scope_key: str, conversation_id: str, frame):
                    return await worker_supervisor().send(scope_key, conversation_id, frame)

            def _publish_event_with_db(params: dict, *, run_context=None):
                # The MAIN side is the canonical persistence point for
                # worker-relayed events. The worker's wrapped
                # ``publish_recorded_event`` (see
                # ``worker_publish_bridge._install_publish_hook``) is
                # only invoked from the agent thread with NO ``db``
                # argument, so its inner ``record_event`` falls into
                # the ``elif terminal_event`` no-db branch and skips
                # ``append_run_event`` entirely — the worker has NOT
                # already persisted the row. Sub-Phase 6b earlier
                # assumed it had, set ``persist=False`` here, and the
                # net result was that NO side wrote ``runs.status``
                # for any worker turn:
                #
                # - ``runs.status`` stayed "running" forever even after
                #   ``message.complete`` reached the frontend.
                # - ``prompt._run_prompt_submit`` reads ``runs.status``
                #   in ``terminalize_if_still_active`` after the agent
                #   thread joined; with stale "running" it
                #   re-published a synthetic terminal event with
                #   ``status="failed"`` (or "cancelled" post Phase 11)
                #   which then overwrote the frontend's correct
                #   terminal state and left the session_index spinner
                #   stuck.
                # - Refreshing the sidebar healed it because
                #   ``reconcile_session_index``'s
                #   ``_repair_session_index_terminal_active_runs_locked``
                #   cleared the projection from the runs table.
                #
                # Persist=True with a control_home DB. Duplicate
                # protection is already inside ``append_run_event``
                # (``_persistence_disposition='duplicate_terminal'``)
                # in case anything DOES write twice — and it returns
                # the canonical event in that case rather than
                # dropping subscribers, so live delivery is safe.
                stable = ""
                if run_context is not None:
                    stable = str(getattr(run_context, "conversation_session_id", "") or "").strip()
                if isinstance(params, dict):
                    stable = stable or str(
                        params.get("stored_session_id")
                        or params.get("session_id")
                        or ""
                    ).strip()
                db = None
                if stable:
                    try:
                        db = _server._db_for_stable_session(stable)
                    except Exception:
                        db = None
                return run_control.publish_recorded_event(
                    params, db=db, persist=True, run_context=run_context,
                )

            def _publish_run_terminal_with_db(**kwargs):
                stable = str(
                    kwargs.get("stored_session_id")
                    or kwargs.get("runtime_session_id")
                    or ""
                ).strip()
                db = kwargs.get("db")
                if db is None and stable:
                    try:
                        db = _server._db_for_stable_session(stable)
                    except Exception:
                        db = None
                if db is not None:
                    kwargs["db"] = db
                # publish_run_terminal_event internally calls
                # publish_recorded_event without exposing a persist
                # flag; that call DOES persist on the main side, but
                # this method is only invoked from on_run_terminal
                # which Phase 4c already gates to abnormal exits
                # (cancelled/failed) only — those have not been
                # persisted by the worker (worker may have died before
                # its own publish), so the main-side persist is the
                # canonical source there.
                return run_control.publish_run_terminal_event(**kwargs)

            _router_singleton = WorkerFrameRouter(
                sender=_SupervisorSenderProxy(),
                publish_event=_publish_event_with_db,
                publish_run_terminal=_publish_run_terminal_with_db,
            )
        return _router_singleton


def worker_pool() -> WorkerPool:
    """Process-wide per-conversation worker lease pool."""
    global _pool_singleton
    with _singleton_lock:
        if _pool_singleton is None:
            _pool_singleton = WorkerPool(worker_supervisor())
        return _pool_singleton


async def shutdown_run_worker_runtime() -> None:
    """Terminate every running worker subprocess and clear the
    singletons. Safe to call multiple times; safe to call when nothing
    was spawned (no-op).

    Intended to run from the sidecar's async shutdown hook before the
    process exits. Sync atexit handlers can't drive this — they have
    no event loop — so this function is exposed for explicit wiring.
    """
    global _supervisor_singleton, _router_singleton, _pool_singleton, _runtime_loop
    supervisor: Optional[WorkerSupervisor]
    pool: Optional[WorkerPool]
    with _singleton_lock:
        pool = _pool_singleton
        supervisor = _supervisor_singleton
        _pool_singleton = None
        _supervisor_singleton = None
        _router_singleton = None
        _runtime_loop = None
    if pool is not None:
        try:
            await pool.shutdown()
        except Exception:
            _log.exception("[worker-runtime] worker_pool shutdown raised")
    elif supervisor is not None:
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
    remember_worker_runtime_loop()
    if not isinstance(req, dict):
        return False
    method = str(req.get("method") or "").strip()
    # ``run.submit`` is the canonical entry the frontend sends (see
    # apps/desktop/src/services/hermes/gateway/runtime.ts). The
    # ``prompt.submit`` @method handler internally forwards to
    # ``run.submit`` after registry-reservation bookkeeping, so we
    # intercept both — the prompt.submit one catches any caller that
    # bypasses the desktop runtime client (CLI tools, tests).
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    if method == "runtime.cloud_proxy.update":
        return await _dispatch_runtime_cloud_proxy_update(req, transport, params)
    if method == "run.cancel":
        return await _dispatch_run_cancel(req, transport, params)
    if method in _INTERACTIVE_RESPONSE_METHODS:
        return await _dispatch_interactive_response(req, transport, method, params)
    if method not in ("run.submit", "prompt.submit"):
        return False
    scope = runtime_scope_from_request(req)
    if not scope.has_scope:
        # Default profile / no scope → in-process path; nothing to
        # route through worker.
        return False
    return await _dispatch_prompt_submit(req, transport, scope, params)


async def _dispatch_runtime_cloud_proxy_update(
    req: dict,
    transport: Any,
    params: dict,
) -> bool:
    rid = req.get("id")
    try:
        from tui_gateway.methods.runtime_cloud_proxy import (
            apply_runtime_cloud_proxy_update,
        )

        result = await apply_runtime_cloud_proxy_update(params)
    except Exception as exc:
        _log.warning("[worker-runtime] runtime cloud proxy update failed: %s", exc)
        await _ack_error(
            transport,
            rid,
            code=5023,
            message=f"runtime cloud proxy update failed: {exc}",
        )
        return True
    await _ack_ok(transport, rid, result=result)
    return True


# Interactive-response RPCs the agent's tools block on. The agent runs
# in a worker subprocess (Phase 6+) so the cooperative wait happens
# inside that process — the answer must reach the worker's
# ``WorkerInteractiveResponder``, not the main sidecar's in-process
# ``_pending`` dict (which is empty in this architecture). Each method
# maps to the JSON-RPC param field carrying the user's answer.
_INTERACTIVE_RESPONSE_METHODS: dict[str, tuple[str, str]] = {
    # method: (interactive_kind, answer_param_name)
    "clarify.respond": ("clarify", "answer"),
    "approval.respond": ("approval", "choice"),
    "secret.respond": ("secret", "value"),
    "sudo.respond": ("sudo", "password"),
}


async def _dispatch_interactive_response(
    req: dict, transport: Any, method: str, params: dict,
) -> bool:
    """Route ``*.respond`` RPCs to the worker that owns the pending
    interactive request.

    Without this, the frontend's answer never reaches the agent: the
    in-process @method handler in prompt.py looks for the entry in
    the MAIN sidecar's ``_pending`` dict, but the agent's
    ``clarify_gateway.register`` / ``tools/approval.submit_pending``
    runs in the WORKER process — its in-process ``_pending`` lives
    there. The MAIN side only knows ``request_id → scope_key``
    (recorded via ``WorkerFrameRouter.on_interactive_request`` from
    the ``InteractiveRequestFrame`` the worker emitted on stdout).
    ``WorkerFrameRouter.respond`` does the cross-process handoff:
    sends an ``InteractiveResponseFrame`` to the right worker, whose
    ``WorkerInteractiveResponder.resolve`` then unblocks the agent
    thread's ``threading.Event`` waiter.

    Falls through to the in-process handler when the router has no
    record of the request_id — covers (a) default-scope sessions
    that never spawned a worker, and (b) the duplicate-respond case
    (the entry was already popped by the first call).
    """
    rid = req.get("id")
    interactive_kind, answer_field = _INTERACTIVE_RESPONSE_METHODS[method]
    # ``approval.respond`` ships ``session_id`` (the publish bridge uses
    # the session_key as the request_id for approval — single-slot per
    # session — see worker_publish_bridge._install_approval_hooks).
    # The other three (clarify/secret/sudo) ship ``request_id`` directly.
    # Try both so this dispatcher matches the frontend's actual payload
    # shape (see runtime.ts:respondCommandApproval / respondInputApproval).
    request_id = str(
        params.get("request_id")
        or params.get("requestId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    if not request_id:
        return False
    router = worker_frame_router()
    if not router.has_pending_request(request_id):
        return False
    answer = params.get(answer_field, "")
    # ``approval.respond``'s ``choice`` field tends to come back as a
    # plain string; pass through verbatim. ``clarify.respond`` /
    # ``secret.respond`` / ``sudo.respond`` likewise — the worker
    # responder forwards the answer untouched to whichever resolver
    # the kind dictates.
    ok = await router.respond(request_id, answer, expected_kind=interactive_kind)
    if not ok:
        # Race: router said pending → respond saw it gone. Either a
        # parallel respond won, or the worker already terminalised
        # the run (on_run_terminal cleans pending). Either way, the
        # in-process fallback will return the canonical "no pending"
        # error to the caller.
        return False
    await _ack_success(transport, rid, {"status": "ok", "source": "primary-run-worker"})
    return True


async def _dispatch_run_cancel(req: dict, transport: Any, params: dict) -> bool:
    """Route ``run.cancel`` to the worker subprocess that owns the run.

    Without this, ``run.cancel`` falls through to the in-process
    ``@method`` handler in ``methods/run.py`` which:
      1. Tries ``session.interrupt`` against the MAIN sidecar's
         in-process session map. The worker is in a separate process,
         so the session row never appears there — interrupt fails.
      2. Falls back to ``publish_run_terminal_event`` with
         ``message="cancelled without live runtime"``. This publishes
         a synthetic ``message.complete(cancelled)`` to the FRONTEND
         (UI shows cancelled state) but never touches the worker.
      3. The worker keeps running. When its actual run finishes, it
         publishes its own real terminal event, which races with the
         synthetic one — usually the synthetic wins because it
         already persisted the cancelled status, but the worker has
         silently kept executing tools / consuming tokens.

    The fix: look up the scope_key the run was started on (router
    recorded it at ``record_run_start``), send a ``RunCancelFrame``
    to that worker. The worker's ``AgentRunBackend.cancel`` sets the
    cancel_event, ``_watch_for_cancel`` in agent_runner translates
    that into ``session.interrupted_run_id`` + the legacy interrupt
    RPC — same path the in-process gateway used before Phase 5.

    Returns False (fall through to in-process handler) if the run isn't
    known to the router. That handles two cases legitimately:
      * The run never started under the worker (in-process default-
        scope sessions, leader/node runs that were submitted before
        Phase 7 — unlikely in practice but defensive).
      * The run already completed and was ``forget_run``'d. There's
        nothing left to cancel; the in-process handler's fallback
        terminal-publish is a no-op when the DB already has the
        terminal row.
    """
    rid = req.get("id")
    run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    if not run_id:
        return False
    router = worker_frame_router()
    info = router.lookup_run(run_id)
    if info is None or not info.scope_key:
        return False
    supervisor = worker_supervisor()
    ok = await supervisor.send(
        info.scope_key,
        info.conversation_id,
        RunCancelFrame(run_id=run_id),
    )
    if not ok:
        # Worker stdin closed (subprocess died?) — fall through so the
        # in-process handler can synthesize a terminal event and
        # frontend sees something instead of nothing.
        return False
    # Ack frontend immediately. The actual cancelled terminal event
    # will flow back through the normal worker→main event pipe once
    # the agent unwinds its interrupt.
    await _ack_success(transport, rid, {
        "status": "cancelled",
        "run_id": run_id,
        "stored_session_id": info.stored_session_id,
        "turn_id": info.turn_id,
        "source": "primary-run-worker",
    })
    return True


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
    try:
        workspace_context = session_workspace_run_context(stored_session_id, params)
    except ValueError as exc:
        await _ack_error(transport, rid, code=4002, message=str(exc))
        return True

    pool = worker_pool()
    router = worker_frame_router()
    _worker_run_log(
        "dispatch-start",
        method=str(req.get("method") or ""),
        request_id=rid,
        scope_key=scope.runtime_scope_key,
        agent_profile_id=scope.agent_profile_id,
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        source=str(params.get("source") or ""),
        dispatch_activity_id=str(params.get("dispatch_activity_id") or ""),
        prompt_len=len(prompt_text),
        param_keys=sorted(str(key) for key in params.keys()),
    )

    try:
        lease = await pool.get_or_spawn(
            stored_session_id,
            _profile_context_for_worker_pool(scope, params),
            scope_key=scope.runtime_scope_key,
        )
    except Exception as exc:
        _worker_run_log(
            "dispatch-spawn-error",
            request_id=rid,
            scope_key=scope.runtime_scope_key,
            stored_session_id=stored_session_id,
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        _log.exception(
            "[worker-runtime] worker_pool.get_or_spawn failed scope=%s conversation=%s",
            scope.runtime_scope_key,
            stored_session_id,
        )
        await _ack_error(
            transport, rid, code=5021,
            message=f"primary worker spawn failed: {exc}",
        )
        return True
    _worker_run_log(
        "dispatch-lease-acquired",
        request_id=rid,
        requested_scope_key=scope.runtime_scope_key,
        lease_scope_key=lease.scope_key,
        stored_session_id=stored_session_id,
        worker_conversation_id=lease.worker_conversation_id,
        run_id=run_id,
        turn_id=turn_id,
        worker_pid=lease.worker.process.pid if lease.worker.process else None,
        worker_running=lease.worker.running(),
        worker_active_runs=sorted(lease.worker.active_runs),
    )
    run_start_kwargs = {
        "scope_key": lease.scope_key,
        "conversation_id": stored_session_id,
        "run_id": run_id,
        "stored_session_id": stored_session_id,
        "turn_id": turn_id,
    }
    if params.get("run_context_json") is not None:
        run_start_kwargs["run_context_json"] = params.get("run_context_json")
    if params.get("dispatch_activity_id") is not None:
        run_start_kwargs["dispatch_activity_id"] = params.get("dispatch_activity_id")
    if params.get("source") in {"agent_dispatch", "team_dispatch"}:
        run_start_kwargs["activity_kind"] = params.get("source")
    elif params.get("dispatch_activity_id") is not None:
        run_start_kwargs["activity_kind"] = "team_dispatch"
    if params.get("parent_scope_key") is not None:
        run_start_kwargs["parent_scope_key"] = params.get("parent_scope_key")
    if params.get("parent_conversation_id") is not None:
        run_start_kwargs["parent_conversation_id"] = params.get("parent_conversation_id")
    if params.get("parent_hermes_home") is not None:
        run_start_kwargs["parent_hermes_home"] = params.get("parent_hermes_home")
    router.record_run_start(**run_start_kwargs)
    _worker_run_log(
        "router-record-run-start",
        request_id=rid,
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        scope_key=lease.scope_key,
        activity_kind=str(run_start_kwargs.get("activity_kind") or ""),
        dispatch_activity_id=str(run_start_kwargs.get("dispatch_activity_id") or ""),
    )
    await pool.record_run_start(
        conversation_id=stored_session_id,
        run_id=run_id,
        stored_session_id=stored_session_id,
        turn_id=turn_id,
        scope_key=lease.scope_key,
    )
    _worker_run_log(
        "pool-record-run-start-complete",
        request_id=rid,
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        scope_key=lease.scope_key,
    )

    frame_params = {
        k: v for k, v in params.items()
        if k not in {
            "text", "stored_session_id", "storedSessionId",
            "session_id", "client_run_id", "run_id", "turn_id",
            "cwd", "workspace",
        }
    }
    if workspace_context:
        frame_params["cwd"] = workspace_context["cwd"]
        frame_params["workspace"] = workspace_context["workspace"]
    frame_params["runtime_scope_key"] = lease.scope_key
    frame_params["conversation_id"] = stored_session_id
    if scope.agent_profile_id:
        frame_params.setdefault("agent_profile_id", scope.agent_profile_id)
        frame_params.setdefault("agentProfileId", scope.agent_profile_id)

    _worker_run_log(
        "supervisor-send-start",
        request_id=rid,
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        scope_key=lease.scope_key,
        frame_param_keys=sorted(str(key) for key in frame_params.keys()),
    )
    ok = await worker_supervisor().send(
        lease.scope_key,
        stored_session_id,
        RunStartFrame(
            run_id=run_id,
            turn_id=turn_id,
            stored_session_id=stored_session_id,
            prompt=prompt_text,
            # Strip params we either already lifted or that are too
            # large to send over the JSON-line pipe. The worker re-
            # resolves anything it needs from its own session state.
            params=frame_params,
        ),
    )
    await pool.release(stored_session_id, scope_key=lease.scope_key)
    _worker_run_log(
        "supervisor-send-result",
        request_id=rid,
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        scope_key=lease.scope_key,
        ok=ok,
    )
    if not ok:
        router.forget_run(run_id)
        await pool.forget_run(run_id)
        await _ack_error(
            transport, rid, code=5022,
            message="primary worker stdin write failed",
        )
        return True

    _worker_run_log(
        "dispatch-ack-ok",
        request_id=rid,
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        scope_key=lease.scope_key,
    )
    await _ack_ok(
        transport, rid,
        result={
            "status": "queued",
            "run_id": run_id,
            "turn_id": turn_id,
            "stored_session_id": stored_session_id,
            "runtime_scope_key": lease.scope_key,
            "conversation_id": stored_session_id,
            "source": "primary-run-worker",
        },
    )
    return True


def _profile_context_for_worker_pool(scope: RuntimeScope, params: dict[str, Any]) -> dict[str, Any]:
    profile = params.get("dovie_profile") if isinstance(params.get("dovie_profile"), dict) else {}
    agent_profile_id = str(
        scope.agent_profile_id
        or profile.get("id")
        or profile.get("agent_profile_id")
        or profile.get("agentProfileId")
        or params.get("agent_profile_id")
        or params.get("agentProfileId")
        or ""
    ).strip()
    return {
        "agent_profile_id": agent_profile_id,
        "agentProfileId": agent_profile_id,
        "id": agent_profile_id,
        "hermes_home": (
            scope.hermes_home
            or profile.get("hermesHomePath")
            or profile.get("hermes_home_path")
            or ""
        ),
        "runtime_scope_key": scope.runtime_scope_key,
        "runtimeScopeKey": scope.runtime_scope_key,
        "dovie_profile": profile,
    }


async def _ack_ok(transport: Any, rid: Any, *, result: dict) -> None:
    await transport.write_async(
        {"jsonrpc": "2.0", "id": rid, "result": result}
    )


async def _ack_error(transport: Any, rid: Any, *, code: int, message: str) -> None:
    await transport.write_async(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    )


async def _ack_success(transport: Any, rid: Any, result: dict) -> None:
    await transport.write_async(
        {"jsonrpc": "2.0", "id": rid, "result": result}
    )


def _reset_for_tests() -> None:
    """Test-only helper. Drops the singletons WITHOUT terminating any
    running subprocesses (use ``shutdown_run_worker_runtime`` for that).
    Use sparingly — only when a test needs a fresh router/supervisor
    pair AND has already torn down any spawned workers itself."""
    global _supervisor_singleton, _router_singleton, _pool_singleton, _runtime_loop
    with _singleton_lock:
        _pool_singleton = None
        _supervisor_singleton = None
        _router_singleton = None
        _runtime_loop = None
