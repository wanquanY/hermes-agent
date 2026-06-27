"""Per-conversation worker lease management.

``WorkerSupervisor`` owns subprocess mechanics keyed by
``RuntimeScope.worker_identity``. ``WorkerPool`` adds the policy layer
the control plane needs: one live worker per conversation, serialized
spawn per conversation, idle reaping, and crash terminalization.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from tui_gateway.run_worker import RunTerminalFrame
from tui_gateway.services.runtime_proxy import RuntimeScope
from tui_gateway.services.worker_supervisor import RunWorker, WorkerSupervisor

_log = logging.getLogger(__name__)

_TerminalCallback = Callable[[str, str, RunTerminalFrame], Awaitable[None]]


@dataclass(frozen=True)
class WorkerLease:
    """Lease returned to a caller that is about to enqueue a run."""

    conversation_id: str
    worker: RunWorker
    acquired_at: float

    @property
    def scope_key(self) -> str:
        return self.worker.scope_key

    @property
    def worker_conversation_id(self) -> str:
        return self.worker.conversation_id

    def running(self) -> bool:
        return self.worker.running()


@dataclass
class _RunRecord:
    run_id: str
    stored_session_id: str
    turn_id: str = ""
    started_at: float = field(default_factory=time.time)


@dataclass
class _LeaseState:
    conversation_id: str
    worker: RunWorker
    created_at: float
    last_acquired_at: float
    idle_since: Optional[float] = None
    inflight: dict[str, _RunRecord] = field(default_factory=dict)

    def has_inflight(self) -> bool:
        return bool(self.inflight or self.worker.active_runs)


class WorkerPool:
    """Per-conversation worker subprocess pool with lease semantics."""

    def __init__(
        self,
        supervisor: WorkerSupervisor,
        *,
        idle_reap_after_s: float = 300,
        reap_tick_s: float = 30,
    ) -> None:
        if idle_reap_after_s < 0:
            raise ValueError("idle_reap_after_s must be >= 0")
        if reap_tick_s <= 0:
            raise ValueError("reap_tick_s must be > 0")
        self._supervisor = supervisor
        self._idle_reap_after_s = float(idle_reap_after_s)
        self._reap_tick_s = float(reap_tick_s)
        self._states: dict[str, _LeaseState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._run_to_conversation: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._last_reap_at = 0.0
        self._reap_task: Optional[asyncio.Task] = None
        self._terminal_callback: Optional[_TerminalCallback] = getattr(
            supervisor, "_on_run_terminal", None,
        )
        self._wrap_supervisor_terminal_callback()
        self._ensure_reap_task()

    async def get_or_spawn(
        self,
        conversation_id: str,
        profile_context: dict,
    ) -> WorkerLease:
        """Return live worker for conv; spawn if none. Thread-safe."""

        conv = self._normalize_conversation_id(conversation_id)
        self._ensure_reap_task()
        conv_lock = await self._lock_for(conv)
        async with conv_lock:
            state = self._states.get(conv)
            if state is not None and state.worker.running():
                now = time.time()
                state.last_acquired_at = now
                state.idle_since = None
                state.worker.mark_used()
                return WorkerLease(conversation_id=conv, worker=state.worker, acquired_at=now)
            if state is not None:
                await self._handle_dead_worker(conv, state, reason="worker exited before acquire")

            scope = self._scope_for(conv, profile_context)
            worker = await self._supervisor.ensure(scope)
            now = time.time()
            state = _LeaseState(
                conversation_id=conv,
                worker=worker,
                created_at=now,
                last_acquired_at=now,
            )
            self._states[conv] = state
            return WorkerLease(conversation_id=conv, worker=worker, acquired_at=now)

    async def release(self, conversation_id: str) -> None:
        """Mark worker idle. In-flight runs still block idle reaping."""

        conv = self._normalize_conversation_id(conversation_id)
        conv_lock = await self._lock_for(conv)
        async with conv_lock:
            state = self._states.get(conv)
            if state is not None and state.worker.running():
                now = time.time()
                state.idle_since = now
                state.worker.last_used_at = now

    async def kill(self, conversation_id: str) -> bool:
        """Force-terminate worker for conv."""

        conv = self._normalize_conversation_id(conversation_id)
        conv_lock = await self._lock_for(conv)
        found = False
        async with conv_lock:
            state = self._states.get(conv)
            if state is None:
                pass
            else:
                found = True
                await self._fail_inflight_runs(state, reason="worker killed")
                self._states.pop(conv, None)
                await self._supervisor.shutdown(
                    state.worker.scope_key,
                    state.worker.conversation_id,
                )
        if not found:
            await self._drop_lock(conv)
            return False
        await self._drop_lock(conv)
        return True

    async def shutdown(self) -> None:
        """Shut down all workers + reap loop."""

        self._closing = True
        task = self._reap_task
        self._reap_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._supervisor.shutdown_all()
        async with self._lock:
            self._states.clear()
            self._locks.clear()
            self._run_to_conversation.clear()

    def stats(self) -> dict:
        """Return worker counts and reap timing diagnostics."""

        states = list(self._states.values())
        active = [s for s in states if s.worker.running() and s.has_inflight()]
        idle = [
            s for s in states
            if s.worker.running() and s.idle_since is not None and not s.has_inflight()
        ]
        return {
            "source": "dovie-worker-pool",
            "workerCount": len(states),
            "runningWorkerCount": sum(1 for s in states if s.worker.running()),
            "activeWorkerCount": len(active),
            "idleWorkerCount": len(idle),
            "lastReapAt": self._last_reap_at,
            "reapTickSeconds": self._reap_tick_s,
            "idleReapAfterSeconds": self._idle_reap_after_s,
            "reapTaskRunning": self._reap_task is not None and not self._reap_task.done(),
            "workers": [
                {
                    "conversationId": s.conversation_id,
                    "scopeKey": s.worker.scope_key,
                    "pid": s.worker.process.pid if s.worker.running() else None,
                    "running": s.worker.running(),
                    "activeRuns": sorted(set(s.inflight) | set(s.worker.active_runs)),
                    "createdAt": s.created_at,
                    "lastAcquiredAt": s.last_acquired_at,
                    "idleSince": s.idle_since,
                    "returncode": s.worker.process.returncode,
                }
                for s in sorted(states, key=lambda item: item.conversation_id)
            ],
        }

    async def record_run_start(
        self,
        *,
        conversation_id: str,
        run_id: str,
        stored_session_id: str,
        turn_id: str = "",
    ) -> None:
        """Track an in-flight run so reaping and crash recovery are exact."""

        conv = self._normalize_conversation_id(conversation_id)
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return
        conv_lock = await self._lock_for(conv)
        async with conv_lock:
            state = self._states.get(conv)
            if state is None:
                return
            state.inflight[normalized_run_id] = _RunRecord(
                run_id=normalized_run_id,
                stored_session_id=str(stored_session_id or conv).strip(),
                turn_id=str(turn_id or "").strip(),
            )
            state.worker.active_runs.add(normalized_run_id)
            self._run_to_conversation[normalized_run_id] = conv

    async def forget_run(self, run_id: str) -> None:
        """Drop a run from pool tracking without publishing a terminal event."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return
        conv = self._run_to_conversation.pop(normalized_run_id, "")
        if not conv:
            return
        conv_lock = await self._lock_for(conv)
        async with conv_lock:
            state = self._states.get(conv)
            if state is None:
                return
            state.inflight.pop(normalized_run_id, None)
            state.worker.active_runs.discard(normalized_run_id)

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._reap_tick_s)
                await self._reap_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("[worker-pool] reap loop crashed")
            if not self._closing:
                self._ensure_reap_task(force=True)

    async def _reap_once(self) -> None:
        now = time.time()
        self._last_reap_at = now
        candidates = list(self._states.items())
        for conv, state in candidates:
            conv_lock = await self._lock_for(conv)
            removed = False
            async with conv_lock:
                current = self._states.get(conv)
                if current is not state:
                    continue
                if not state.worker.running():
                    await self._handle_dead_worker(conv, state, reason="worker crashed")
                    removed = True
                elif not state.has_inflight() and state.idle_since is not None:
                    if now - state.idle_since <= self._idle_reap_after_s:
                        continue
                    _log.warning(
                        "[worker-pool] reaping idle worker conv_id=%s pid=%s idle_for=%.3fs",
                        conv,
                        state.worker.process.pid,
                        now - state.idle_since,
                    )
                    self._states.pop(conv, None)
                    await self._supervisor.shutdown(
                        state.worker.scope_key,
                        state.worker.conversation_id,
                    )
                    removed = True
            if removed:
                await self._drop_lock(conv)

    async def _handle_dead_worker(
        self,
        conversation_id: str,
        state: _LeaseState,
        *,
        reason: str,
    ) -> None:
        uptime = max(0.0, time.time() - state.created_at)
        inflight_count = len(set(state.inflight) | set(state.worker.active_runs))
        if inflight_count:
            _log.warning(
                "[worker-pool] worker conv_id=%s crashed after %.3fs inflight=%d",
                conversation_id,
                uptime,
                inflight_count,
            )
        await self._fail_inflight_runs(state, reason=reason)
        self._states.pop(conversation_id, None)
        await self._supervisor.shutdown(
            state.worker.scope_key,
            state.worker.conversation_id,
        )

    async def _fail_inflight_runs(self, state: _LeaseState, *, reason: str) -> None:
        run_ids = sorted(set(state.inflight) | set(state.worker.active_runs))
        for run_id in run_ids:
            record = state.inflight.get(run_id) or _RunRecord(
                run_id=run_id,
                stored_session_id=state.conversation_id,
            )
            message = f"{reason}: worker process exited"
            if self._terminal_callback is not None:
                try:
                    await self._terminal_callback(
                        state.worker.scope_key,
                        state.worker.conversation_id,
                        RunTerminalFrame(
                            run_id=run_id,
                            status="failed",
                            stored_session_id=record.stored_session_id or state.conversation_id,
                            turn_id=record.turn_id,
                            message=message,
                        ),
                    )
                except Exception:
                    _log.exception(
                        "[worker-pool] terminal callback failed conv_id=%s run_id=%s",
                        state.conversation_id,
                        run_id,
                    )
            else:
                self._publish_failed_run_direct(state, record, message=message)
            state.worker.active_runs.discard(run_id)
            state.inflight.pop(run_id, None)
            self._run_to_conversation.pop(run_id, None)

    def _publish_failed_run_direct(
        self,
        state: _LeaseState,
        record: _RunRecord,
        *,
        message: str,
    ) -> None:
        try:
            from tui_gateway.services import run_control

            run_control.publish_run_terminal_event(
                stored_session_id=record.stored_session_id or state.conversation_id,
                run_id=record.run_id,
                turn_id=record.turn_id,
                runtime_scope_key=state.worker.scope_key,
                runtime_session_id=record.stored_session_id or state.conversation_id,
                status="failed",
                message=message,
            )
        except Exception:
            _log.exception(
                "[worker-pool] direct terminal publish failed conv_id=%s run_id=%s",
                state.conversation_id,
                record.run_id,
            )

    async def _mark_run_terminal(
        self,
        scope_key: str,
        conversation_id: str,
        run_id: str,
    ) -> None:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return
        conv = self._run_to_conversation.pop(normalized_run_id, "")
        if not conv:
            for candidate, state in self._states.items():
                if (
                    state.worker.scope_key == scope_key
                    and state.worker.conversation_id == (conversation_id or "")
                ):
                    conv = candidate
                    break
        if not conv:
            return
        conv_lock = await self._lock_for(conv)
        async with conv_lock:
            state = self._states.get(conv)
            if state is None:
                return
            state.inflight.pop(normalized_run_id, None)
            state.worker.active_runs.discard(normalized_run_id)
            if not state.has_inflight() and state.idle_since is None:
                state.idle_since = time.time()

    def _wrap_supervisor_terminal_callback(self) -> None:
        if self._terminal_callback is None:
            return

        async def _wrapped(
            scope_key: str,
            conversation_id: str,
            frame: RunTerminalFrame,
        ) -> None:
            try:
                await self._terminal_callback(scope_key, conversation_id, frame)
            finally:
                await self._mark_run_terminal(scope_key, conversation_id, frame.run_id)

        setattr(self._supervisor, "_on_run_terminal", _wrapped)

    def _ensure_reap_task(self, *, force: bool = False) -> None:
        if self._closing:
            return
        if self._reap_task is not None and not self._reap_task.done() and not force:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reap_task = loop.create_task(self._reap_loop(), name="worker-pool-reap")

    async def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        async with self._lock:
            lock = self._locks.get(conversation_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[conversation_id] = lock
            return lock

    async def _drop_lock(self, conversation_id: str) -> None:
        async with self._lock:
            if conversation_id not in self._states:
                self._locks.pop(conversation_id, None)

    @staticmethod
    def _normalize_conversation_id(conversation_id: str) -> str:
        conv = str(conversation_id or "").strip()
        if not conv:
            raise ValueError("conversation_id required")
        return conv

    @staticmethod
    def _scope_for(conversation_id: str, profile_context: dict) -> RuntimeScope:
        profile = profile_context if isinstance(profile_context, dict) else {}
        dovie_profile = profile.get("dovie_profile")
        if not isinstance(dovie_profile, dict):
            dovie_profile = profile.get("dovieProfile")
        if not isinstance(dovie_profile, dict):
            dovie_profile = {}
        agent_profile_id = str(
            profile.get("agent_profile_id")
            or profile.get("agentProfileId")
            or profile.get("id")
            or dovie_profile.get("agent_profile_id")
            or dovie_profile.get("agentProfileId")
            or dovie_profile.get("id")
            or ""
        ).strip()
        hermes_home = str(
            profile.get("hermes_home")
            or profile.get("hermesHomePath")
            or profile.get("hermes_home_path")
            or profile.get("runtime_home_path")
            or profile.get("runtimeHomePath")
            or dovie_profile.get("hermes_home")
            or dovie_profile.get("hermesHomePath")
            or dovie_profile.get("hermes_home_path")
            or dovie_profile.get("runtime_home_path")
            or dovie_profile.get("runtimeHomePath")
            or ""
        ).strip()
        explicit_scope_key = str(
            profile.get("runtime_scope_key")
            or profile.get("runtimeScopeKey")
            or dovie_profile.get("runtime_scope_key")
            or dovie_profile.get("runtimeScopeKey")
            or ""
        ).strip()
        if explicit_scope_key.startswith(("profile:", "team:", "draft:")):
            scope_key = explicit_scope_key
        elif agent_profile_id:
            scope_key = f"profile:{agent_profile_id}"
        else:
            scope_key = explicit_scope_key
        return RuntimeScope(
            agent_profile_id=agent_profile_id,
            runtime_scope_key=scope_key,
            conversation_id=conversation_id,
            hermes_home=hermes_home,
        )
