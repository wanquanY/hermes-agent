"""Process supervisor for the stdin/stdout ``run_worker`` subprocess.

Replaces ``RuntimeWorkerPool`` + ``RuntimeProxyBridge``: instead of
spawning a child sidecar that listens on its own websocket and lives
through ``RuntimeProxyBridge``, the supervisor launches a plain
subprocess that speaks the line-framed JSON protocol defined in
``tui_gateway.run_worker``.

Phase 4b (this file) implements:
- ``RunWorker`` — one subprocess per ``runtime_scope_key``
- ``WorkerSupervisor`` — process registry, spawn/send/shutdown
- bounded inbound queue + a separate dispatch task per worker so the
  stdout read loop never blocks on DB writes done by the callbacks
  (handoff invariant #8)

Phase 4c wired the dispatch callbacks into ``run_control``,
``tools/approval``, ``tools/clarify_gateway``. Phase 5+ made this the
sole worker-spawning path; the legacy ``RuntimeWorkerPool`` proxy was
deleted in Phase 6.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from tui_gateway.run_worker import (
    DBRpcReplyFrame,
    DBRpcRequestFrame,
    EventFrame,
    FrameDecodeError,
    IncomingFrame,
    InteractiveRequestFrame,
    LogFrame,
    OutgoingFrame,
    RunCancelFrame,
    RunTerminalFrame,
    ShutdownFrame,
    decode_outgoing,
    encode_incoming,
)
from tui_gateway.services.runtime_proxy import RuntimeScope
from tui_gateway.services.worker_db_proxy import serialize_db_value

_log = logging.getLogger(__name__)


# Callback types. Each receives ``scope_key`` so a single registered
# handler can fan out by profile without the supervisor having to wrap
# anything. Callbacks may be coroutines — the dispatch task awaits them.
EventCallback = Callable[[str, EventFrame], Awaitable[None]]
InteractiveRequestCallback = Callable[[str, InteractiveRequestFrame], Awaitable[None]]
RunTerminalCallback = Callable[[str, RunTerminalFrame], Awaitable[None]]
LogCallback = Callable[[str, LogFrame], Awaitable[None]]


_DEFAULT_QUEUE_MAXSIZE = 1024
_DEFAULT_SHUTDOWN_TIMEOUT_S = 3.0
_SIGTERM_GRACE_S = 2.0


DB_RPC_ALLOWED_METHODS = frozenset(
    {
        "append_message",
        "append_run_event",
        "append_team_mission_conversation_status_event",
        "append_team_mission_event_for_run",
        "create_run_if_session_idle",
        "create_session",
        "end_session",
        "fail_orphaned_active_runs",
        "get_messages_as_conversation",
        "get_run",
        "get_session",
        "get_session_run_status",
        "get_team_mission_run_binding",
        "list_conversation_participants",
        "list_run_events",
        "list_runs",
        "list_team_mission_events",
        "list_team_mission_run_events",
        "next_run_event_seq",
        "reduce_team_mission_run_event",
        "resolve_participant_id",
        "set_session_title",
        "update_session_cwd",
        "update_session_meta",
        "update_session_model",
        "update_system_prompt",
        "upsert_run",
        "upsert_session",
    }
)


@dataclass
class RunWorker:
    """One ``run_worker`` subprocess + its bookkeeping. Constructed
    by ``WorkerSupervisor._spawn`` — don't instantiate elsewhere."""

    scope: RuntimeScope
    process: asyncio.subprocess.Process
    inbound_queue: asyncio.Queue
    created_at: float
    last_used_at: float
    active_runs: set[str] = field(default_factory=set)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    read_task: Optional[asyncio.Task] = None
    dispatch_task: Optional[asyncio.Task] = None
    closing: bool = False

    @property
    def scope_key(self) -> str:
        return self.scope.runtime_scope_key

    @property
    def hermes_home(self) -> str:
        """Per-profile HERMES_HOME this worker is bound to. Routed
        callbacks use this to enter the profile context so events
        publish into the right per-profile DB (matching what
        ``events.subscribe`` captured on the frontend's behalf)."""
        return self.scope.hermes_home

    def running(self) -> bool:
        return self.process.returncode is None

    def mark_used(self) -> None:
        self.last_used_at = time.time()

    def status(self) -> dict[str, Any]:
        running = self.running()
        return {
            "scopeKey": self.scope_key,
            "agentProfileId": self.scope.agent_profile_id or None,
            "hermesHome": self.scope.hermes_home or None,
            "pid": self.process.pid if running else None,
            "running": running,
            "activeRuns": sorted(self.active_runs),
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "returncode": self.process.returncode,
        }


class WorkerSupervisor:
    """Process registry keyed by ``scope.runtime_scope_key``.

    A single instance lives in the main sidecar (``app_state.worker_supervisor``,
    wired in Phase 4c/5). Dispatch callbacks are injected at construction
    so this module stays free of imports from ``run_control`` /
    ``tools/*`` — those modules wire their callbacks once on startup.
    """

    def __init__(
        self,
        *,
        on_event: EventCallback,
        on_interactive_request: InteractiveRequestCallback,
        on_run_terminal: RunTerminalCallback,
        on_log: Optional[LogCallback] = None,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        python_executable: Optional[str] = None,
    ) -> None:
        self._workers: dict[str, RunWorker] = {}
        self._lock = asyncio.Lock()
        self._on_event = on_event
        self._on_interactive_request = on_interactive_request
        self._on_run_terminal = on_run_terminal
        self._on_log = on_log
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._python = python_executable or sys.executable
        self._db_rpc_lock = asyncio.Lock()

    # ── public API ───────────────────────────────────────────────────

    async def ensure(
        self,
        scope: RuntimeScope,
        *,
        env_overrides: Optional[dict[str, str]] = None,
    ) -> RunWorker:
        if not scope.runtime_scope_key:
            raise RuntimeError("WorkerSupervisor.ensure: runtime_scope_key required")
        async with self._lock:
            existing = self._workers.get(scope.runtime_scope_key)
            if existing is not None and existing.running():
                existing.mark_used()
                return existing
            if existing is not None:
                # Process died — drop and respawn.
                self._workers.pop(scope.runtime_scope_key, None)
            worker = await self._spawn_locked(scope, env_overrides or {})
            self._workers[scope.runtime_scope_key] = worker
            return worker

    def get(self, scope_key: str) -> Optional[RunWorker]:
        return self._workers.get(scope_key)

    async def send(self, scope_key: str, frame: IncomingFrame) -> bool:
        """Write ``frame`` to the worker's stdin.

        Returns False if the worker is not registered or no longer
        running. Caller decides whether to ``ensure()`` first."""
        worker = self._workers.get(scope_key)
        if worker is None or not worker.running():
            return False
        line = encode_incoming(frame) + "\n"
        data = line.encode("utf-8")
        async with worker.send_lock:
            if worker.process.stdin is None or worker.process.stdin.is_closing():
                return False
            try:
                worker.process.stdin.write(data)
                await worker.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return False
        worker.mark_used()
        return True

    async def shutdown(self, scope_key: str) -> bool:
        async with self._lock:
            worker = self._workers.pop(scope_key, None)
        if worker is None:
            return False
        await self._terminate(worker)
        return True

    async def shutdown_all(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        if not workers:
            return
        await asyncio.gather(
            *(self._terminate(w) for w in workers),
            return_exceptions=True,
        )

    def snapshot(self) -> dict[str, Any]:
        workers = [w.status() for w in self._workers.values()]
        running = [w for w in workers if w["running"]]
        return {
            "source": "dovie-run-worker-supervisor",
            "workerCount": len(workers),
            "runningWorkerCount": len(running),
            "workers": sorted(workers, key=lambda item: str(item.get("scopeKey") or "")),
        }

    # ── internals ────────────────────────────────────────────────────

    async def _spawn_locked(
        self, scope: RuntimeScope, env_overrides: dict[str, str],
    ) -> RunWorker:
        env = os.environ.copy()
        # The new worker runs the LLM in-process; HERMES_HOME selects
        # the per-profile data root just like the legacy sub-sidecar.
        if scope.hermes_home:
            env["HERMES_HOME"] = scope.hermes_home
        env["DOVIE_HERMES_RUNTIME_SCOPE_KEY"] = scope.runtime_scope_key
        if scope.agent_profile_id:
            env["DOVIE_AGENT_PROFILE_ID"] = scope.agent_profile_id
        env.update(env_overrides)
        # stderr inherits the main sidecar's stderr so tracebacks land
        # in the same agent.log as the legacy worker. Phase 5 may rewire
        # this to a per-scope file once we have the file-rotation policy.
        # Use ``-c "from ... import main; main()"`` instead of ``-m
        # tui_gateway.run_worker`` to avoid Python's double-import
        # trap: ``python -m foo.bar`` loads ``foo/bar.py`` AS ``__main__``,
        # and any later ``from foo.bar import X`` in lazily-imported
        # modules loads a SECOND copy of ``foo.bar`` — so ``isinstance``
        # checks across the boundary fail (class identity differs).
        # The ``-c`` bootstrap loads the module exactly once under its
        # canonical name.
        process = await asyncio.create_subprocess_exec(
            self._python,
            "-c",
            "from tui_gateway.run_worker import main; main()",
            cwd=os.getcwd(),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
        )
        now = time.time()
        worker = RunWorker(
            scope=scope,
            process=process,
            inbound_queue=asyncio.Queue(maxsize=self._queue_maxsize),
            created_at=now,
            last_used_at=now,
        )
        worker.read_task = asyncio.create_task(
            self._read_loop(worker),
            name=f"run-worker-read[{scope.runtime_scope_key}]",
        )
        worker.dispatch_task = asyncio.create_task(
            self._dispatch_loop(worker),
            name=f"run-worker-dispatch[{scope.runtime_scope_key}]",
        )
        _log.warning(
            "[worker-supervisor] spawned run_worker pid=%s scope=%s",
            process.pid, scope.runtime_scope_key,
        )
        return worker

    async def _read_loop(self, worker: RunWorker) -> None:
        """Read worker stdout, decode, queue. Never call out to user
        callbacks here — they may do DB writes and we must not block
        the worker.
        """
        stdout = worker.process.stdout
        if stdout is None:
            return
        try:
            while True:
                raw = await stdout.readline()
                if not raw:
                    # EOF: worker closed stdout / exited.
                    break
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    _log.warning(
                        "[worker-supervisor] %s non-utf8 frame dropped: %s",
                        worker.scope_key, exc,
                    )
                    continue
                try:
                    frame = decode_outgoing(line)
                except FrameDecodeError as exc:
                    _log.warning(
                        "[worker-supervisor] %s decode error: %s; raw=%r",
                        worker.scope_key, exc, line[:200],
                    )
                    continue
                # bounded queue → applies backpressure to the worker's
                # stdout pipe when the dispatcher falls behind.
                await worker.inbound_queue.put(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "[worker-supervisor] %s read loop crashed", worker.scope_key,
            )
        finally:
            # Send a sentinel so the dispatch loop can drain and exit.
            await worker.inbound_queue.put(None)

    async def _dispatch_loop(self, worker: RunWorker) -> None:
        """Drain the inbound queue and invoke the right callback.

        Callback exceptions are logged but do NOT kill the dispatcher —
        one bad event must not stop the rest from flowing.
        """
        try:
            while True:
                frame = await worker.inbound_queue.get()
                if frame is None:
                    return
                try:
                    await self._dispatch_one(worker, frame)
                except Exception:
                    _log.exception(
                        "[worker-supervisor] %s dispatch callback raised",
                        worker.scope_key,
                    )
        except asyncio.CancelledError:
            raise

    async def _dispatch_one(self, worker: RunWorker, frame: OutgoingFrame) -> None:
        scope_key = worker.scope_key
        # Enter the worker's profile context so callbacks (notably
        # publish_recorded_event) resolve to the right per-profile DB.
        # Without this, the router runs in the main sidecar's default
        # ``_active_hermes_home`` and events land in the control_home
        # DB while the frontend's ``events.subscribe`` poller queries
        # the per-profile DB — events never reach the live ws.
        token = None
        if worker.hermes_home:
            try:
                from tui_gateway.services.profile_context import enter_profile_context
                token = enter_profile_context(
                    {"hermes_home": worker.hermes_home, "runtime_scope_key": scope_key},
                )
            except Exception:
                _log.exception(
                    "[worker-supervisor] enter_profile_context failed scope=%s", scope_key,
                )
        try:
            if isinstance(frame, EventFrame):
                await self._on_event(scope_key, frame)
            elif isinstance(frame, DBRpcRequestFrame):
                await self._handle_db_rpc(worker, frame)
            elif isinstance(frame, InteractiveRequestFrame):
                await self._on_interactive_request(scope_key, frame)
            elif isinstance(frame, RunTerminalFrame):
                worker.active_runs.discard(frame.run_id)
                await self._on_run_terminal(scope_key, frame)
            elif isinstance(frame, LogFrame):
                if self._on_log is not None:
                    await self._on_log(scope_key, frame)
                else:
                    _log.info(
                        "[worker-log] scope=%s level=%s text=%s",
                        scope_key, frame.level, frame.text,
                    )
            else:  # pragma: no cover — exhausted by Union
                _log.warning(
                    "[worker-supervisor] %s unknown frame type %r",
                    scope_key, type(frame),
                )
        finally:
            if token is not None:
                try:
                    from tui_gateway.services.profile_context import leave_profile_context
                    leave_profile_context(token)
                except Exception:
                    pass

    async def _handle_db_rpc(self, worker: RunWorker, frame: DBRpcRequestFrame) -> None:
        reply = await self._execute_db_rpc(frame)
        await self._send_db_reply(worker, reply)

    async def _send_db_reply(self, worker: RunWorker, reply: DBRpcReplyFrame) -> bool:
        data = (encode_incoming(reply) + "\n").encode("utf-8")
        async with worker.send_lock:
            if worker.process.stdin is None or worker.process.stdin.is_closing():
                return False
            try:
                worker.process.stdin.write(data)
                await worker.process.stdin.drain()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

    async def _execute_db_rpc(self, frame: DBRpcRequestFrame) -> DBRpcReplyFrame:
        req_id = str(frame.id or "")
        method = str(frame.method or "")
        if not method.startswith("db."):
            return _db_rpc_error(
                req_id,
                "WorkerDBProxyMethodError",
                f"unsupported worker RPC method {method!r}",
                code=-32601,
            )
        db_method_name = method[3:]
        if db_method_name not in DB_RPC_ALLOWED_METHODS:
            return _db_rpc_error(
                req_id,
                "WorkerDBProxyMethodError",
                f"db method {db_method_name!r} is not allowed over worker IPC",
                code=-32601,
            )
        try:
            args, kwargs = _decode_db_rpc_params(frame.params)
            db = _db_for_worker_rpc(frame, args, kwargs)
            if db is None:
                raise RuntimeError("state.db unavailable")
            target = getattr(db, db_method_name, None)
            if not callable(target):
                raise AttributeError(f"SessionDB has no method {db_method_name!r}")
            async with self._db_rpc_lock:
                result = target(*args, **kwargs)
            return DBRpcReplyFrame(id=req_id, result=serialize_db_value(result))
        except Exception as exc:
            return _db_rpc_error(
                req_id,
                type(exc).__name__,
                str(exc) or repr(exc),
                code=-32000,
            )

    async def _terminate(self, worker: RunWorker) -> None:
        worker.closing = True
        process = worker.process
        if process.returncode is None:
            # Try the cooperative path first: send shutdown over stdin,
            # then SIGTERM if it doesn't exit, then SIGKILL.
            try:
                async with worker.send_lock:
                    if process.stdin is not None and not process.stdin.is_closing():
                        try:
                            process.stdin.write(
                                (encode_incoming(ShutdownFrame()) + "\n").encode("utf-8")
                            )
                            await process.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        try:
                            process.stdin.close()
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=_DEFAULT_SHUTDOWN_TIMEOUT_S)
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=_SIGTERM_GRACE_S)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await process.wait()
                    except Exception:
                        pass
        # Cancel reader / dispatcher (read loop may already be at EOF).
        for task in (worker.read_task, worker.dispatch_task):
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _decode_db_rpc_params(params: Any) -> tuple[list[Any], dict[str, Any]]:
    if not isinstance(params, list) or len(params) != 2:
        raise ValueError("db RPC params must be [args, kwargs]")
    args, kwargs = params
    if not isinstance(args, list):
        raise ValueError("db RPC args must be a list")
    if not isinstance(kwargs, dict):
        raise ValueError("db RPC kwargs must be an object")
    return args, kwargs


def _db_for_worker_rpc(
    frame: DBRpcRequestFrame,
    args: list[Any],
    kwargs: dict[str, Any],
) -> Any:
    stable = _stable_session_id_from_rpc(frame, args, kwargs)
    from tui_gateway import server as _server

    if stable:
        return _server._db_for_stable_session(stable)
    return _server._get_db()


def _stable_session_id_from_rpc(
    frame: DBRpcRequestFrame,
    args: list[Any],
    kwargs: dict[str, Any],
) -> str:
    scope = getattr(frame, "db_scope", None)
    if isinstance(scope, dict):
        stable = str(scope.get("stable_session_id") or "").strip()
        if stable:
            return stable
    if isinstance(frame, DBRpcRequestFrame):
        raw_scope = getattr(frame, "db_scope", None)
        if isinstance(raw_scope, str) and raw_scope.strip():
            return raw_scope.strip()
    for key in ("stored_session_id", "session_id", "conversation_session_id"):
        value = str(kwargs.get(key) or "").strip()
        if value:
            return value
    method = str(frame.method or "")
    if method in {
        "db.get_messages_as_conversation",
        "db.list_conversation_participants",
        "db.get_session",
        "db.append_message",
        "db.append_run_event",
        "db.list_run_events",
        "db.list_runs",
        "db.get_session_run_status",
        "db.next_run_event_seq",
        "db.create_session",
        "db.end_session",
    } and args:
        return str(args[0] or "").strip()
    return ""


def _db_rpc_error(
    req_id: str,
    error_type: str,
    message: str,
    *,
    code: int,
) -> DBRpcReplyFrame:
    return DBRpcReplyFrame(
        id=req_id,
        error={
            "code": int(code),
            "type": error_type,
            "message": message,
        },
    )
