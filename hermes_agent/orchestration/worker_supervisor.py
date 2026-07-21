"""Process supervisor for the stdin/stdout ``run_worker`` subprocess.

``RunWorker`` owns subprocess IO for one ``runtime_scope_key`` /
conversation identity. ``WorkerSupervisor`` owns process registry,
spawn/send/shutdown, and callback dispatch. The bounded inbound queue keeps
stdout reading independent from DB writes performed by callbacks.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Tuple

from agent.dovie_diagnostics import emit_dovie_runtime_diagnostic
from tui_gateway.run_worker import (
    DBRpcReplyFrame,
    DBRpcRequestFrame,
    EventFrame,
    FrameDecodeError,
    IncomingFrame,
    InteractiveRequestFrame,
    LogFrame,
    OutgoingFrame,
    RuntimeEnvUpdateFrame,
    RunCancelFrame,
    RunStartFrame,
    RunTerminalFrame,
    ShutdownFrame,
    WorkerReadyFrame,
    decode_outgoing,
    encode_incoming,
)
from tui_gateway.services.runtime_scope import RuntimeScope
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_agent.orchestration.worker_db_proxy import serialize_db_value
from tools.environments.local import hermes_subprocess_env

_log = logging.getLogger(__name__)


def _worker_supervisor_log(stage: str, **fields: Any) -> None:
    emit_dovie_runtime_diagnostic("dovie-worker-run", stage, fields)


def _background_task_context() -> contextvars.Context:
    """Create background worker tasks without request-scoped transports."""
    ctx = contextvars.copy_context()
    try:
        from tui_gateway.transport import bind_transport
        ctx.run(bind_transport, None)
    except Exception:
        _log.debug(
            "[worker-supervisor] failed to clear request transport for background task",
            exc_info=True,
        )
    return ctx


def _create_worker_task(coro, *, name: str) -> asyncio.Task:
    try:
        return asyncio.create_task(coro, name=name, context=_background_task_context())
    except TypeError:  # pragma: no cover - Python < 3.11 compatibility
        try:
            from tui_gateway.transport import bind_transport, reset_transport
            token = bind_transport(None)
            try:
                return asyncio.create_task(coro, name=name)
            finally:
                reset_transport(token)
        except Exception:
            _log.debug(
                "[worker-supervisor] failed to bind clean transport before creating task",
                exc_info=True,
            )
            return asyncio.create_task(coro, name=name)


# Callback types. Each receives the UI routing ``scope_key`` and the
# per-conversation worker identity suffix so same-profile workers do not
# collapse into one response/cancel route.
EventCallback = Callable[[str, str, EventFrame], Awaitable[None]]
InteractiveRequestCallback = Callable[[str, str, InteractiveRequestFrame], Awaitable[None]]
RunTerminalCallback = Callable[[str, str, RunTerminalFrame], Awaitable[None]]
LogCallback = Callable[[str, str, LogFrame], Awaitable[None]]


_DEFAULT_QUEUE_MAXSIZE = 1024
_DEFAULT_SHUTDOWN_TIMEOUT_S = 3.0
_DEFAULT_READY_TIMEOUT_S = 30.0
_SIGTERM_GRACE_S = 2.0
_WORKER_STDIO_LIMIT_ENV = "HERMES_WORKER_STDIO_LIMIT_BYTES"
_MIN_WORKER_STDIO_LIMIT_BYTES = 1024 * 1024
_DEFAULT_WORKER_STDIO_LIMIT_BYTES = 64 * 1024 * 1024


def _normalize_worker_stdio_limit_bytes(value: Any = None) -> int:
    if value is None:
        value = os.environ.get(_WORKER_STDIO_LIMIT_ENV)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = _DEFAULT_WORKER_STDIO_LIMIT_BYTES
    if parsed <= 0:
        parsed = _DEFAULT_WORKER_STDIO_LIMIT_BYTES
    return max(_MIN_WORKER_STDIO_LIMIT_BYTES, parsed)


def _is_stream_limit_overrun(exc: ValueError) -> bool:
    message = str(exc)
    return "chunk is longer than limit" in message or "Separator is not found" in message


# Enforced by tests/test_worker_db_proxy_whitelist_coverage.py.
DB_RPC_ALLOWED_METHODS = frozenset(
    {
        "activities.create",
        "activities.get",
        "activities.get_command",
        "activities.get_for_mission",
        "activities.insert_command",
        "activities.list",
        "activities.list_active_missions",
        "activities.list_pending_commands",
        "activities.mark_cancelled",
        "activities.mark_read",
        "activities.unread_count",
        "activities.update_command_state",
        "activities.update_status",
        "append_team_mission_conversation_status_event",
        "append_team_mission_event_for_run",
        "append_team_mission_run_event",
        "append_team_mission_structural_event",
        "claim_team_mission_node_start",
        "complete_team_mission_plan",
        "conversation_memory.get_item",
        "conversation_memory.list_edges",
        "conversation_memory.list_items",
        "conversation_memory.update_item",
        "conversation_memory.upsert_edge",
        "ensure_team_mission_conversation",
        "get_message_by_conversation_message_id",
        "get_scoped_system_prompt",
        "get_team_mission_conversation",
        "get_team_mission_conversation_by_session",
        "get_team_mission_node",
        "get_team_mission_result",
        "get_team_mission_run_binding",
        "latest_team_mission_deliverable_for_run",
        "list_team_mission_deliverables",
        "list_team_mission_events",
        "list_team_mission_run_events",
        "list_unread_completions",
        "maintenance.vacuum",
        "messages.all_as_conversation",
        "messages.anchored_view",
        "messages.append",
        "messages.compact_active",
        "messages.list",
        "messages.owning_session_id",
        "messages.page_as_conversation",
        "messages.replace",
        "messages.search",
        "messages.set_current_user_api_content",
        "messages.upsert_team_message",
        "messages.window_around",
        "participants.list_conversation_participants",
        "participants.resolve_participant_id",
        "prune_team_mission_events",
        "runs.has_event_source",
        "reduce_team_mission_graph",
        "reduce_team_mission_run_event",
        "resolve_team_mission_conversation",
        "run_event_maintenance.backfill_activity_ids",
        "run_event_maintenance.backfill_frames",
        "run_event_maintenance.compact",
        "run_event_maintenance.maybe_auto_compact",
        "run_event_maintenance.prune_duplicate_session_info",
        "run_event_maintenance.reference_payloads",
        "runtime_stability.carry_forward",
        "runtime_stability.clear_stream_stale",
        "runtime_stability.get",
        "runtime_stability.record_stream_stale_failure",
        "runtime_stability.write_compression",
        "verification.completion_requirement",
        "verification.mark_edited",
        "verification.record_terminal",
        "verification.status",
        "runs.append_event",
        "runs.fail_orphaned",
        "runs.get",
        "runs.interaction_anchor_seq",
        "runs.list",
        "runs.list_events",
        "runs.list_events_by_activity",
        "runs.list_events_by_mission_activity",
        "runs.list_events_by_run_ids",
        "runs.list_filtered_events",
        "runs.list_tool_events",
        "runs.next_event_seq",
        "runs.register_event_listener",
        "runs.reserve_if_idle",
        "runs.session_status",
        "runs.terminate",
        "runs.upsert",
        "session_index.get",
        "session_index.repair_terminal_active_runs",
        "sessions.create",
        "sessions.end",
        "sessions.get",
        "sessions.get_title",
        "sessions.list_rich",
        "sessions.next_title_in_lineage",
        "sessions.sanitize_title",
        "sessions.set_title",
        "sessions.update_cwd",
        "sessions.update_source",
        "sessions.update_system_prompt",
        "sessions.update_token_counts",
        "team_mission_audit.append",
        "team_mission_audit.list",
        "team_mission_graphs.get_team_mission_graph",
        "team_mission_maintenance.rebase_workspace_paths",
        "team_mission_node_history.list_messages",
        "team_mission_rows.conversation_from_row",
        "team_missions.exists",
        "team_missions.mission_id_for_run",
        "team_missions.mission_ids_for_session_key",
        "team_missions.status",
        "update_scoped_system_prompt",
        "update_session_index_for_mission",
        "update_session_index_pending_state_for_session_key",
        "update_session_meta",
        "update_session_model",
        "upsert_projected_conversation_message",
        "upsert_session",
        "upsert_team_mission",
        "upsert_team_mission_deliverable",
        "upsert_team_mission_edge",
        "upsert_team_mission_node",
        "upsert_team_mission_result",
    }
)

WORKER_TEAM_MISSION_GATEWAY_METHODS = frozenset({
    "team_mission.create",
    "team_mission.team_profile.get",
})


@dataclass
class RunWorker:
    """One ``run_worker`` subprocess + its bookkeeping. Constructed
    by ``WorkerSupervisor._spawn`` — don't instantiate elsewhere."""

    scope: RuntimeScope
    process: asyncio.subprocess.Process
    inbound_queue: asyncio.Queue
    created_at: float
    last_used_at: float
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    read_task: Optional[asyncio.Task] = None
    dispatch_task: Optional[asyncio.Task] = None
    closing: bool = False
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    ready_error: str = ""
    bootstrap_ms: float = 0.0
    bootstrap_stages_ms: dict[str, float] = field(default_factory=dict)

    @property
    def scope_key(self) -> str:
        return self.scope.runtime_scope_key

    @property
    def conversation_id(self) -> str:
        return self.scope.conversation_id

    @property
    def identity(self) -> Tuple[str, str]:
        return self.scope.worker_identity

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
            "conversationId": self.conversation_id or None,
            "workerIdentity": list(self.identity),
            "agentProfileId": self.scope.agent_profile_id or None,
            "hermesHome": self.scope.hermes_home or None,
            "pid": self.process.pid if running else None,
            "running": running,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "returncode": self.process.returncode,
            "ready": self.ready_event.is_set() and not self.ready_error,
            "readyError": self.ready_error or None,
            "bootstrapMs": self.bootstrap_ms,
            "bootstrapStagesMs": dict(self.bootstrap_stages_ms),
        }


class WorkerSupervisor:
    """Process registry keyed by ``scope.worker_identity``.

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
        stdio_limit_bytes: Optional[int] = None,
    ) -> None:
        self._workers: dict[Tuple[str, str], RunWorker] = {}
        self._lock = asyncio.Lock()
        self._on_event = on_event
        self._on_interactive_request = on_interactive_request
        self._on_run_terminal = on_run_terminal
        self._on_log = on_log
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._python = python_executable or sys.executable
        self._stdio_limit_bytes = _normalize_worker_stdio_limit_bytes(stdio_limit_bytes)

    # ── public API ───────────────────────────────────────────────────

    async def ensure(
        self,
        scope: RuntimeScope,
        *,
        env_overrides: Optional[dict[str, str]] = None,
        ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
    ) -> RunWorker:
        if not scope.runtime_scope_key:
            raise RuntimeError("WorkerSupervisor.ensure: runtime_scope_key required")
        identity = scope.worker_identity
        async with self._lock:
            existing = self._workers.get(identity)
            if existing is not None and existing.running():
                existing.mark_used()
                worker = existing
            else:
                if existing is not None:
                    # Process died — drop and respawn.
                    self._workers.pop(identity, None)
                worker = await self._spawn_locked(scope, env_overrides or {})
                self._workers[identity] = worker
        try:
            await asyncio.wait_for(worker.ready_event.wait(), timeout=ready_timeout_s)
        except TimeoutError as exc:
            await self.shutdown(worker.scope_key, worker.conversation_id)
            raise RuntimeError(
                f"run worker bootstrap timed out after {ready_timeout_s:.1f}s"
            ) from exc
        if worker.ready_error:
            await self.shutdown(worker.scope_key, worker.conversation_id)
            raise RuntimeError(f"run worker bootstrap failed: {worker.ready_error}")
        return worker

    async def rebind(self, worker: RunWorker, scope: RuntimeScope) -> RunWorker:
        """Atomically claim an unbound warm worker for one conversation."""
        if not scope.runtime_scope_key or not scope.conversation_id:
            raise ValueError("rebind target requires runtime_scope_key and conversation_id")
        source_identity = worker.identity
        target_identity = scope.worker_identity
        async with self._lock:
            if self._workers.get(source_identity) is not worker or not worker.running():
                raise RuntimeError("warm worker is no longer available")
            target = self._workers.get(target_identity)
            if target is not None and target is not worker and target.running():
                raise RuntimeError(f"worker already bound to {target_identity!r}")
            self._workers.pop(source_identity, None)
            self._workers.pop(target_identity, None)
            worker.scope = scope
            worker.mark_used()
            self._workers[target_identity] = worker
        env_updates = {
            "DOVIE_HERMES_RUNTIME_SCOPE_KEY": scope.runtime_scope_key,
            "DOVIE_CONVERSATION_ID": scope.conversation_id,
            "DOVIE_AGENT_PROFILE_ID": scope.agent_profile_id,
        }
        if not await self._send_frame_to_worker(
            worker,
            RuntimeEnvUpdateFrame(env_updates=env_updates),
        ):
            async with self._lock:
                self._workers.pop(target_identity, None)
            await self._terminate(worker)
            raise RuntimeError("failed to bind warm worker runtime environment")
        _worker_supervisor_log(
            "supervisor-warm-worker-claimed",
            scope_key=scope.runtime_scope_key,
            conversation_id=scope.conversation_id,
            worker_pid=worker.process.pid if worker.process else None,
            bootstrap_ms=worker.bootstrap_ms,
        )
        return worker

    def get(self, scope_key: str, conversation_id: str = "") -> Optional[RunWorker]:
        return self._workers.get((scope_key, conversation_id or ""))

    async def send(
        self,
        scope_key: str,
        conversation_id: str,
        frame: IncomingFrame,
    ) -> bool:
        """Write ``frame`` to the worker's stdin.

        Returns False if the worker is not registered or no longer
        running. Caller decides whether to ``ensure()`` first."""
        worker = self._workers.get((scope_key, conversation_id or ""))
        if worker is None or not worker.running():
            _worker_supervisor_log(
                "supervisor-send-miss",
                scope_key=scope_key,
                conversation_id=conversation_id,
                frame_type=type(frame).__name__,
                run_id=str(getattr(frame, "run_id", "") or ""),
                reason="worker_not_running",
            )
            return False
        if isinstance(frame, (RunStartFrame, RunCancelFrame)):
            _worker_supervisor_log(
                "supervisor-send",
                scope_key=scope_key,
                conversation_id=conversation_id,
                frame_type=type(frame).__name__,
                run_id=str(getattr(frame, "run_id", "") or ""),
                turn_id=str(getattr(frame, "turn_id", "") or ""),
                worker_pid=worker.process.pid if worker.process else None,
            )
        if not await self._send_frame_to_worker(worker, frame):
            _worker_supervisor_log(
                "supervisor-send-write-failed",
                scope_key=scope_key,
                conversation_id=conversation_id,
                frame_type=type(frame).__name__,
                run_id=str(getattr(frame, "run_id", "") or ""),
                worker_pid=worker.process.pid if worker.process else None,
            )
            return False
        worker.mark_used()
        return True

    async def broadcast_runtime_env_update(self, env_updates: dict[str, str]) -> int:
        """Send a runtime env update control frame to every live worker."""
        sent = 0
        async with self._lock:
            workers = list(self._workers.values())
        frame = RuntimeEnvUpdateFrame(env_updates=dict(env_updates or {}))
        for worker in workers:
            if not worker.running():
                continue
            try:
                if await self._send_frame_to_worker(worker, frame):
                    sent += 1
                else:
                    pid = worker.process.pid if worker.process is not None else None
                    _log.warning("worker env update failed pid=%s: stdin closed", pid)
            except Exception as exc:
                pid = worker.process.pid if worker.process is not None else None
                _log.warning("worker env update failed pid=%s: %s", pid, exc)
        return sent

    async def cancel_run(self, scope_key: str, conversation_id: str, run_id: str) -> bool:
        """Gracefully ask the worker that owns ``run_id`` to cancel it."""
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return False
        return await self.send(
            scope_key,
            conversation_id,
            RunCancelFrame(run_id=normalized_run_id),
        )

    async def shutdown(self, scope_key: str, conversation_id: str = "") -> bool:
        async with self._lock:
            worker = self._workers.pop((scope_key, conversation_id or ""), None)
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
            "workers": sorted(
                workers,
                key=lambda item: (
                    str(item.get("scopeKey") or ""),
                    str(item.get("conversationId") or ""),
                ),
            ),
        }

    # ── internals ────────────────────────────────────────────────────

    async def _send_frame_to_worker(
        self,
        worker: RunWorker,
        frame: IncomingFrame,
    ) -> bool:
        data = (encode_incoming(frame) + "\n").encode("utf-8")
        async with worker.send_lock:
            if worker.process.stdin is None or worker.process.stdin.is_closing():
                return False
            try:
                worker.process.stdin.write(data)
                await worker.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return False
        return True

    async def _spawn_locked(
        self, scope: RuntimeScope, env_overrides: dict[str, str],
    ) -> RunWorker:
        # A run worker hosts the model runtime, so provider credentials are
        # intentional authority. Gateway, infra, dashboard and dynamic Hermes
        # secrets are not: route both the parent snapshot and profile overlay
        # through the canonical two-tier subprocess policy.
        env = hermes_subprocess_env(
            inherit_credentials=True,
            extra_env=env_overrides,
        )
        # The new worker runs the LLM in-process; HERMES_HOME selects
        # the per-profile data root just like the legacy sub-sidecar.
        if scope.hermes_home:
            env["HERMES_HOME"] = scope.hermes_home
        env["DOVIE_HERMES_RUNTIME_SCOPE_KEY"] = scope.runtime_scope_key
        if scope.conversation_id:
            env["DOVIE_CONVERSATION_ID"] = scope.conversation_id
        if scope.agent_profile_id:
            env["DOVIE_AGENT_PROFILE_ID"] = scope.agent_profile_id
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
            # Worker stdout is a line-framed JSON protocol, but image turns can
            # surface large event frames while the model input carries native
            # image data. The default asyncio limit is only 64 KiB and crashes
            # readline() before the frame reaches the router.
            limit=self._stdio_limit_bytes,
        )
        now = time.time()
        worker = RunWorker(
            scope=scope,
            process=process,
            inbound_queue=asyncio.Queue(maxsize=self._queue_maxsize),
            created_at=now,
            last_used_at=now,
        )
        worker.read_task = _create_worker_task(
            self._read_loop(worker),
            name=f"run-worker-read[{scope.runtime_scope_key}:{scope.conversation_id}]",
        )
        worker.dispatch_task = _create_worker_task(
            self._dispatch_loop(worker),
            name=f"run-worker-dispatch[{scope.runtime_scope_key}:{scope.conversation_id}]",
        )
        _log.warning(
            "[worker-supervisor] spawned run_worker pid=%s scope=%s conversation=%s",
            process.pid, scope.runtime_scope_key, scope.conversation_id,
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
                try:
                    raw = await stdout.readline()
                except ValueError as exc:
                    if _is_stream_limit_overrun(exc):
                        _log.error(
                            "[worker-supervisor] %s oversized stdout frame dropped "
                            "limit_bytes=%s: %s",
                            worker.scope_key,
                            self._stdio_limit_bytes,
                            exc,
                        )
                        continue
                    raise
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
                    if not worker.ready_event.is_set():
                        worker.ready_error = worker.ready_error or "worker exited before readiness"
                        worker.ready_event.set()
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
                await self._on_event(scope_key, worker.conversation_id, frame)
            elif isinstance(frame, DBRpcRequestFrame):
                await self._handle_db_rpc(worker, frame)
            elif isinstance(frame, InteractiveRequestFrame):
                await self._on_interactive_request(scope_key, worker.conversation_id, frame)
            elif isinstance(frame, RunTerminalFrame):
                _worker_supervisor_log(
                    "supervisor-terminal-received",
                    scope_key=scope_key,
                    conversation_id=worker.conversation_id,
                    run_id=frame.run_id,
                    status=frame.status,
                    turn_id=frame.turn_id,
                    conversation_session_id=frame.conversation_session_id,
                    message=frame.message,
                )
                await self._on_run_terminal(scope_key, worker.conversation_id, frame)
            elif isinstance(frame, LogFrame):
                if self._on_log is not None:
                    await self._on_log(scope_key, worker.conversation_id, frame)
                else:
                    _log.info(
                        "[worker-log] scope=%s level=%s text=%s",
                        scope_key, frame.level, frame.text,
                    )
            elif isinstance(frame, WorkerReadyFrame):
                worker.bootstrap_ms = frame.bootstrap_ms
                worker.bootstrap_stages_ms = dict(frame.stages_ms)
                worker.ready_error = "" if frame.ready else (frame.error or "unknown bootstrap error")
                worker.ready_event.set()
                _worker_supervisor_log(
                    "supervisor-worker-ready",
                    scope_key=scope_key,
                    conversation_id=worker.conversation_id,
                    worker_pid=worker.process.pid if worker.process else None,
                    ready=frame.ready,
                    bootstrap_ms=frame.bootstrap_ms,
                    stages_ms=frame.stages_ms,
                    error=frame.error,
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
                    _log.exception("[worker-supervisor] failed to leave profile context")

    async def _handle_db_rpc(self, worker: RunWorker, frame: DBRpcRequestFrame) -> None:
        reply = await self._execute_worker_jsonrpc(frame, worker=worker)
        await self._send_db_reply(worker, reply)

    async def _send_db_reply(self, worker: RunWorker, reply: DBRpcReplyFrame) -> bool:
        return await self._send_frame_to_worker(worker, reply)

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
            def _invoke_db_method() -> Any:
                db = _db_for_worker_rpc(frame, args, kwargs)
                if db is None:
                    raise RuntimeError("state.db unavailable")
                target: Any = db
                for path_part in db_method_name.split("."):
                    if not path_part or path_part.startswith("_"):
                        target = None
                        break
                    target = getattr(target, path_part, None)
                    if target is None:
                        break
                if not callable(target):
                    raise AttributeError(
                        f"worker database proxy has no method {db_method_name!r}"
                    )
                return target(*args, **kwargs)

            result = await run_sqlite_io(_invoke_db_method)
            return DBRpcReplyFrame(id=req_id, result=serialize_db_value(result))
        except Exception as exc:
            return _db_rpc_error(
                req_id,
                type(exc).__name__,
                str(exc) or repr(exc),
                code=-32000,
            )

    async def _execute_worker_jsonrpc(
        self,
        frame: DBRpcRequestFrame,
        *,
        worker: RunWorker | None = None,
    ) -> DBRpcReplyFrame:
        method = str(frame.method or "")
        if method.startswith("db."):
            return await self._execute_db_rpc(frame)
        if method == "worker.dispatch_agent_async":
            return await self._execute_dispatch_agent_async_rpc(frame, worker=worker)
        if method == "worker.dispatch_team_async":
            return await self._execute_dispatch_team_async_rpc(frame, worker=worker)
        if method == "worker.team_mission_gateway_call":
            return await self._execute_team_mission_gateway_call_rpc(frame, worker=worker)
        return _db_rpc_error(
            str(frame.id or ""),
            "WorkerRPCMethodError",
            f"unsupported worker RPC method {method!r}",
            code=-32601,
        )

    async def _execute_dispatch_agent_async_rpc(
        self,
        frame: DBRpcRequestFrame,
        *,
        worker: RunWorker | None = None,
    ) -> DBRpcReplyFrame:
        req_id = str(frame.id or "")
        params = dict(frame.params) if isinstance(frame.params, dict) else {}
        if worker is not None:
            params.setdefault("_parent_scope_key", worker.scope_key)
            params.setdefault("_parent_hermes_home", worker.hermes_home)
        try:
            from tui_gateway.methods.dispatch import dispatch_agent_async

            result = await dispatch_agent_async(params)
            return DBRpcReplyFrame(id=req_id, result=serialize_db_value(result))
        except Exception as exc:
            return _db_rpc_error(
                req_id,
                type(exc).__name__,
                str(exc) or repr(exc),
                code=-32000,
            )

    async def _execute_team_mission_gateway_call_rpc(
        self,
        frame: DBRpcRequestFrame,
        *,
        worker: RunWorker | None = None,
    ) -> DBRpcReplyFrame:
        req_id = str(frame.id or "")
        params = dict(frame.params) if isinstance(frame.params, dict) else {}
        gateway_method = str(params.get("method") or "").strip()
        gateway_params = params.get("params") if isinstance(params.get("params"), dict) else {}
        if gateway_method not in WORKER_TEAM_MISSION_GATEWAY_METHODS:
            return _db_rpc_error(
                req_id,
                "WorkerRPCMethodError",
                f"team mission gateway method {gateway_method!r} is not allowed over worker IPC",
                code=-32601,
            )
        _log.info(
            "[dovie-team-mission-gateway-call] route=main-worker-rpc method=%s worker_scope=%s conversation_id=%s param_keys=%s",
            gateway_method,
            worker.scope_key if worker is not None else "",
            worker.conversation_id if worker is not None else "",
            sorted(gateway_params.keys()),
        )
        try:
            try:
                from hermes_agent.orchestration.worker_runtime import remember_worker_runtime_loop
                remember_worker_runtime_loop(asyncio.get_running_loop())
            except Exception:
                _log.debug(
                    "[worker-supervisor] failed to remember worker runtime loop before gateway RPC",
                    exc_info=True,
                )
            from tui_gateway import server as _server

            target = _server._methods.get(gateway_method)
            if not callable(target):
                raise RuntimeError(f"Gateway method {gateway_method} is unavailable.")
            def _invoke_gateway_method():
                try:
                    from tui_gateway.transport import bind_transport, reset_transport
                    token = bind_transport(None)
                except Exception:
                    token = None
                    reset_transport = None  # type: ignore[assignment]
                try:
                    return target(f"worker-team-mission:{req_id}", dict(gateway_params))
                finally:
                    if token is not None and reset_transport is not None:
                        try:
                            reset_transport(token)
                        except Exception:
                            _log.exception(
                                "[worker-supervisor] failed to reset transport after gateway RPC"
                            )

            result = await run_sqlite_io(_invoke_gateway_method)
            return DBRpcReplyFrame(id=req_id, result=serialize_db_value(result))
        except Exception as exc:
            return _db_rpc_error(
                req_id,
                type(exc).__name__,
                str(exc) or repr(exc),
                code=-32000,
            )

    async def _execute_dispatch_team_async_rpc(
        self,
        frame: DBRpcRequestFrame,
        *,
        worker: RunWorker | None = None,
    ) -> DBRpcReplyFrame:
        req_id = str(frame.id or "")
        params = dict(frame.params) if isinstance(frame.params, dict) else {}
        if worker is not None:
            params.setdefault("_parent_scope_key", worker.scope_key)
            params.setdefault("_parent_hermes_home", worker.hermes_home)
        try:
            from tui_gateway.methods.dispatch import dispatch_team_async

            result = await dispatch_team_async(params)
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
                            _log.debug(
                                "[worker-supervisor] worker stdin closed before shutdown frame",
                                extra={"scope_key": worker.scope_key, "conversation_id": worker.conversation_id},
                            )
                        try:
                            process.stdin.close()
                        except Exception:
                            _log.debug(
                                "[worker-supervisor] failed to close worker stdin",
                                exc_info=True,
                            )
            except Exception:
                _log.debug(
                    "[worker-supervisor] cooperative worker shutdown failed",
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(process.wait(), timeout=_DEFAULT_SHUTDOWN_TIMEOUT_S)
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    _log.debug(
                        "[worker-supervisor] process already exited before SIGTERM",
                        extra={"scope_key": worker.scope_key, "conversation_id": worker.conversation_id},
                    )
                try:
                    await asyncio.wait_for(process.wait(), timeout=_SIGTERM_GRACE_S)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        _log.debug(
                            "[worker-supervisor] process already exited before SIGKILL",
                            extra={"scope_key": worker.scope_key, "conversation_id": worker.conversation_id},
                        )
                    try:
                        await process.wait()
                    except Exception:
                        _log.debug(
                            "[worker-supervisor] failed while waiting for killed worker process",
                            exc_info=True,
                        )
        # Cancel reader / dispatcher (read loop may already be at EOF).
        for task in (worker.read_task, worker.dispatch_task):
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                _log.debug(
                    "[worker-supervisor] worker task cancelled during shutdown",
                    extra={"scope_key": worker.scope_key, "conversation_id": worker.conversation_id},
                )
            except Exception:
                _log.debug(
                    "[worker-supervisor] worker task failed while shutting down",
                    exc_info=True,
                )


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
    stable = _conversation_session_id_from_rpc(frame, args, kwargs)
    from tui_gateway import server as _server

    if stable:
        return _server._db_for_stable_session(stable)
    return _server._get_db()


def _conversation_session_id_from_rpc(
    frame: DBRpcRequestFrame,
    args: list[Any],
    kwargs: dict[str, Any],
) -> str:
    scope = getattr(frame, "db_scope", None)
    if isinstance(scope, dict):
        stable = str(scope.get("conversation_session_id") or "").strip()
        if stable:
            return stable
    if isinstance(frame, DBRpcRequestFrame):
        raw_scope = getattr(frame, "db_scope", None)
        if isinstance(raw_scope, str) and raw_scope.strip():
            return raw_scope.strip()
    for key in ("conversation_session_id", "session_id", "conversation_session_id", "conversation_id"):
        value = str(kwargs.get(key) or "").strip()
        if value:
            return value
    method = str(frame.method or "")
    if method in {
        "db.get_conversation_message_read_model",
        "db.get_message_by_conversation_message_id",
        "db.messages.all_as_conversation",
        "db.participants.list_conversation_participants",
        "db.participants.get_participant",
        "db.participants.delete_participant",
        "db.participants.update_participant_display",
        "db.participants.ensure_participant",
        "db.participants.ensure_user_participant",
        "db.participants.ensure_leader_participant",
        "db.participants.ensure_member_participant",
        "db.participants.ensure_agent_participant",
        "db.sessions.get",
        "db.session_index.get",
        "db.messages.append",
        "db.append_run_event",
        "db.list_run_events",
        "db.list_runs",
        "db.get_session_run_status",
        "db.next_run_event_seq",
        "db.sessions.create",
        "db.sessions.end",
        "db.runs.terminate",
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
