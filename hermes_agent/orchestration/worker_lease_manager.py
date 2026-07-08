"""Per-conversation worker lease management.

``WorkerSupervisor`` owns subprocess mechanics keyed by
``RuntimeScope.worker_identity``. ``WorkerLeaseManager`` adds the policy layer
the control plane needs: one live worker per conversation/scope lease,
serialized spawn per lease, idle reaping, and crash terminalization.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from agent.dovie_diagnostics import emit_dovie_runtime_diagnostic
from tui_gateway.run_worker import RunTerminalFrame
from tui_gateway.services.runtime_scope import RuntimeScope
from tui_gateway.services.worker_supervisor import RunWorker, WorkerSupervisor

_log = logging.getLogger(__name__)

_TerminalCallback = Callable[[str, str, RunTerminalFrame], Awaitable[None]]
_StateKey = tuple[str, str]
_EnvFingerprint = tuple[tuple[str, str], ...]


def _worker_pool_log(stage: str, **fields: Any) -> None:
    emit_dovie_runtime_diagnostic("dovie-worker-run", stage, fields)


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
    conversation_session_id: str
    turn_id: str = ""
    started_at: float = field(default_factory=time.time)


@dataclass
class _LeaseState:
    conversation_id: str
    worker: RunWorker
    created_at: float
    last_acquired_at: float
    profile_env_fingerprint: _EnvFingerprint = field(default_factory=tuple)
    idle_since: Optional[float] = None
    inflight: dict[str, _RunRecord] = field(default_factory=dict)

    def has_inflight(self) -> bool:
        return bool(self.inflight)


class WorkerLeaseManager:
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
        self._states: dict[_StateKey, _LeaseState] = {}
        self._locks: dict[_StateKey, asyncio.Lock] = {}
        self._run_to_state_key: dict[str, _StateKey] = {}
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
        scope_key: str | None = None,
    ) -> WorkerLease:
        """Return live worker for conversation/scope; spawn if none."""

        conv = self._normalize_conversation_id(conversation_id)
        key = self._state_key(conv, scope_key)
        self._ensure_reap_task()
        lease_lock = await self._lock_for(key)
        async with lease_lock:
            profile_env = self._profile_env_overrides(profile_context)
            profile_env_fingerprint = self._env_fingerprint(profile_env)
            state = self._states.get(key)
            if state is not None and state.worker.running():
                if state.profile_env_fingerprint != profile_env_fingerprint:
                    if state.has_inflight():
                        _log.warning(
                            "[worker-pool] reusing worker with stale profile env while runs are active conv_id=%s scope_key=%s pid=%s",
                            state.conversation_id,
                            state.worker.scope_key,
                            state.worker.process.pid if state.worker.process else None,
                        )
                    else:
                        _worker_pool_log(
                            "pool-lease-env-respawn",
                            conversation_id=conv,
                            scope_key=state.worker.scope_key,
                            worker_conversation_id=state.worker.conversation_id,
                            pid=state.worker.process.pid if state.worker.process else None,
                            old_env_keys=[env_key for env_key, _value in state.profile_env_fingerprint],
                            new_env_keys=sorted(profile_env),
                        )
                        self._states.pop(key, None)
                        await self._supervisor.shutdown(
                            state.worker.scope_key,
                            state.worker.conversation_id,
                        )
                        state = None
                if state is not None:
                    now = time.time()
                    state.last_acquired_at = now
                    state.idle_since = None
                    state.worker.mark_used()
                    _worker_pool_log(
                        "pool-lease-reuse",
                        conversation_id=conv,
                        scope_key=state.worker.scope_key,
                        worker_conversation_id=state.worker.conversation_id,
                        pid=state.worker.process.pid if state.worker.process else None,
                        inflight_runs=sorted(state.inflight),
                        profile_env_keys=sorted(profile_env),
                    )
                    return WorkerLease(conversation_id=conv, worker=state.worker, acquired_at=now)
            if state is not None:
                await self._handle_dead_worker(key, state, reason="worker exited before acquire")

            scope = self._scope_for(conv, profile_context, scope_key=scope_key)
            worker = await self._supervisor.ensure(scope, env_overrides=profile_env)
            now = time.time()
            state = _LeaseState(
                conversation_id=conv,
                worker=worker,
                created_at=now,
                last_acquired_at=now,
                profile_env_fingerprint=profile_env_fingerprint,
            )
            self._states[key] = state
            _worker_pool_log(
                "pool-lease-spawn",
                conversation_id=conv,
                scope_key=worker.scope_key,
                worker_conversation_id=worker.conversation_id,
                pid=worker.process.pid if worker.process else None,
                profile_env_keys=sorted(profile_env),
            )
            return WorkerLease(conversation_id=conv, worker=worker, acquired_at=now)

    async def release(self, conversation_id: str, scope_key: str | None = None) -> None:
        """Mark worker idle. In-flight runs still block idle reaping.

        Without ``scope_key`` this preserves legacy conversation-level
        semantics and releases every scoped worker for the conversation.
        """

        conv = self._normalize_conversation_id(conversation_id)
        for key in await self._state_keys_for(conv, scope_key):
            lease_lock = await self._lock_for(key)
            async with lease_lock:
                state = self._states.get(key)
                if state is not None and state.worker.running():
                    now = time.time()
                    state.idle_since = now
                    state.worker.last_used_at = now

    async def kill(self, conversation_id: str, scope_key: str | None = None) -> bool:
        """Force-terminate worker(s) for conversation/scope."""

        conv = self._normalize_conversation_id(conversation_id)
        found = False
        keys = await self._state_keys_for(conv, scope_key)
        for key in keys:
            lease_lock = await self._lock_for(key)
            async with lease_lock:
                state = self._states.get(key)
                if state is None:
                    continue
                found = True
                await self._fail_inflight_runs(state, reason="worker killed")
                self._states.pop(key, None)
                await self._supervisor.shutdown(
                    state.worker.scope_key,
                    state.worker.conversation_id,
                )
            await self._drop_lock(key)
        if not found:
            for key in keys:
                await self._drop_lock(key)
            return False
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
            self._run_to_state_key.clear()

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
                    "activeRuns": sorted(s.inflight),
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
        conversation_session_id: str,
        turn_id: str = "",
        scope_key: str | None = None,
    ) -> None:
        """Track an in-flight run so reaping and crash recovery are exact."""

        conv = self._normalize_conversation_id(conversation_id)
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return
        key = await self._state_key_for_run_record(conv, scope_key)
        if key is None:
            _worker_pool_log(
                "pool-record-start-miss",
                conversation_id=conv,
                scope_key=scope_key or "",
                run_id=normalized_run_id,
                reason="state_key_not_found",
            )
            return
        lease_lock = await self._lock_for(key)
        async with lease_lock:
            state = self._states.get(key)
            if state is None:
                _worker_pool_log(
                    "pool-record-start-miss",
                    conversation_id=conv,
                    scope_key=scope_key or "",
                    run_id=normalized_run_id,
                    reason="state_not_found",
                )
                return
            before_active_runs = sorted(state.inflight)
            state.inflight[normalized_run_id] = _RunRecord(
                run_id=normalized_run_id,
                conversation_session_id=str(conversation_session_id or conv).strip(),
                turn_id=str(turn_id or "").strip(),
            )
            self._run_to_state_key[normalized_run_id] = key
            _worker_pool_log(
                "pool-record-start",
                conversation_id=conv,
                scope_key=state.worker.scope_key,
                worker_conversation_id=state.worker.conversation_id,
                run_id=normalized_run_id,
                turn_id=str(turn_id or "").strip(),
                before_active_runs=before_active_runs,
                after_active_runs=sorted(state.inflight),
            )

    async def forget_run(self, run_id: str) -> None:
        """Drop a run from pool tracking without publishing a terminal event."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return
        key = self._run_to_state_key.pop(normalized_run_id, None)
        if key is None:
            _worker_pool_log(
                "pool-forget-miss",
                run_id=normalized_run_id,
                reason="state_key_not_found",
            )
            return
        lease_lock = await self._lock_for(key)
        async with lease_lock:
            state = self._states.get(key)
            if state is None:
                _worker_pool_log(
                    "pool-forget-miss",
                    run_id=normalized_run_id,
                    scope_key=key[1],
                    conversation_id=key[0],
                    reason="state_not_found",
                )
                return
            before_active_runs = sorted(state.inflight)
            state.inflight.pop(normalized_run_id, None)
            _worker_pool_log(
                "pool-forget",
                run_id=normalized_run_id,
                scope_key=state.worker.scope_key,
                conversation_id=state.conversation_id,
                before_active_runs=before_active_runs,
                after_active_runs=sorted(state.inflight),
            )

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
        for key, state in candidates:
            lease_lock = await self._lock_for(key)
            removed = False
            async with lease_lock:
                current = self._states.get(key)
                if current is not state:
                    continue
                if not state.worker.running():
                    await self._handle_dead_worker(key, state, reason="worker crashed")
                    removed = True
                elif not state.has_inflight() and state.idle_since is not None:
                    if now - state.idle_since <= self._idle_reap_after_s:
                        continue
                    _log.warning(
                        "[worker-pool] reaping idle worker conv_id=%s scope_key=%s pid=%s idle_for=%.3fs",
                        state.conversation_id,
                        state.worker.scope_key,
                        state.worker.process.pid,
                        now - state.idle_since,
                    )
                    self._states.pop(key, None)
                    await self._supervisor.shutdown(
                        state.worker.scope_key,
                        state.worker.conversation_id,
                    )
                    removed = True
            if removed:
                await self._drop_lock(key)

    async def _handle_dead_worker(
        self,
        key: _StateKey,
        state: _LeaseState,
        *,
        reason: str,
    ) -> None:
        uptime = max(0.0, time.time() - state.created_at)
        inflight_count = len(state.inflight)
        if inflight_count:
            _log.warning(
                "[worker-pool] worker conv_id=%s scope_key=%s crashed after %.3fs inflight=%d",
                state.conversation_id,
                state.worker.scope_key,
                uptime,
                inflight_count,
            )
        await self._fail_inflight_runs(state, reason=reason)
        self._states.pop(key, None)
        await self._supervisor.shutdown(
            state.worker.scope_key,
            state.worker.conversation_id,
        )

    async def _fail_inflight_runs(self, state: _LeaseState, *, reason: str) -> None:
        run_ids = sorted(state.inflight)
        for run_id in run_ids:
            record = state.inflight.get(run_id) or _RunRecord(
                run_id=run_id,
                conversation_session_id=state.conversation_id,
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
                            conversation_session_id=record.conversation_session_id or state.conversation_id,
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
            state.inflight.pop(run_id, None)
            self._run_to_state_key.pop(run_id, None)

    def _publish_failed_run_direct(
        self,
        state: _LeaseState,
        record: _RunRecord,
        *,
        message: str,
    ) -> None:
        try:
            from tui_gateway.services import run_control

            run_control.terminate_run(
                conversation_session_id=record.conversation_session_id or state.conversation_id,
                run_id=record.run_id,
                turn_id=record.turn_id,
                runtime_scope_key=state.worker.scope_key,
                execution_session_id=record.conversation_session_id or state.conversation_id,
                status="failed",
                cause="worker_crashed",  # spec §7.3 — pool reap = WORKER_CRASHED
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
        key = self._run_to_state_key.pop(normalized_run_id, None)
        if key is None:
            for candidate, state in self._states.items():
                if (
                    state.worker.scope_key == scope_key
                    and state.worker.conversation_id == (conversation_id or "")
                ):
                    key = candidate
                    break
        if key is None:
            _worker_pool_log(
                "pool-terminal-miss",
                run_id=normalized_run_id,
                scope_key=scope_key,
                conversation_id=conversation_id,
                reason="state_key_not_found",
            )
            return
        lease_lock = await self._lock_for(key)
        async with lease_lock:
            state = self._states.get(key)
            if state is None:
                _worker_pool_log(
                    "pool-terminal-miss",
                    run_id=normalized_run_id,
                    scope_key=scope_key,
                    conversation_id=conversation_id,
                    reason="state_not_found",
                )
                return
            before_active_runs = sorted(state.inflight)
            state.inflight.pop(normalized_run_id, None)
            if not state.has_inflight() and state.idle_since is None:
                state.idle_since = time.time()
            _worker_pool_log(
                "pool-terminal",
                run_id=normalized_run_id,
                scope_key=state.worker.scope_key,
                conversation_id=state.conversation_id,
                before_active_runs=before_active_runs,
                after_active_runs=sorted(state.inflight),
                idle_since=state.idle_since,
            )

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

    async def _lock_for(self, key: _StateKey) -> asyncio.Lock:
        async with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def _drop_lock(self, key: _StateKey) -> None:
        async with self._lock:
            if key not in self._states:
                self._locks.pop(key, None)

    @staticmethod
    def _state_key(conversation_id: str, scope_key: str | None = None) -> _StateKey:
        return (conversation_id, str(scope_key or "").strip())

    async def _state_keys_for(
        self,
        conversation_id: str,
        scope_key: str | None = None,
    ) -> list[_StateKey]:
        if scope_key is not None:
            return [self._state_key(conversation_id, scope_key)]
        async with self._lock:
            return [
                key for key in sorted(self._states)
                if key[0] == conversation_id
            ]

    async def _state_key_for_run_record(
        self,
        conversation_id: str,
        scope_key: str | None = None,
    ) -> _StateKey | None:
        if scope_key is not None:
            return self._state_key(conversation_id, scope_key)
        async with self._lock:
            keys = [key for key in self._states if key[0] == conversation_id]
        if len(keys) == 1:
            return keys[0]
        legacy_key = self._state_key(conversation_id)
        if legacy_key in keys:
            return legacy_key
        if keys:
            _log.warning(
                "[worker-pool] record_run_start without scope_key is ambiguous conv_id=%s scopes=%s",
                conversation_id,
                [key[1] for key in sorted(keys)],
            )
        return None

    @staticmethod
    def _normalize_conversation_id(conversation_id: str) -> str:
        conv = str(conversation_id or "").strip()
        if not conv:
            raise ValueError("conversation_id required")
        return conv

    @staticmethod
    def _scope_for(
        conversation_id: str,
        profile_context: dict,
        *,
        scope_key: str | None = None,
    ) -> RuntimeScope:
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
            scope_key
            or profile.get("runtime_scope_key")
            or profile.get("runtimeScopeKey")
            or dovie_profile.get("runtime_scope_key")
            or dovie_profile.get("runtimeScopeKey")
            or ""
        ).strip()
        resolved_scope_key = explicit_scope_key or (
            f"profile:{agent_profile_id}" if agent_profile_id else ""
        )
        return RuntimeScope(
            agent_profile_id=agent_profile_id,
            runtime_scope_key=resolved_scope_key,
            conversation_id=conversation_id,
            hermes_home=hermes_home,
        )

    @staticmethod
    def _profile_env_overrides(profile_context: dict) -> dict[str, str]:
        profile = profile_context if isinstance(profile_context, dict) else {}
        dovie_profile = profile.get("dovie_profile")
        if not isinstance(dovie_profile, dict):
            dovie_profile = profile.get("dovieProfile")
        if not isinstance(dovie_profile, dict):
            dovie_profile = {}

        env: dict[str, str] = {}
        for source in (profile.get("env"), dovie_profile.get("env")):
            if not isinstance(source, dict):
                continue
            for raw_key, raw_value in source.items():
                key = str(raw_key or "").strip()
                if not key or raw_value is None:
                    continue
                env[key] = str(raw_value)
        return env

    @staticmethod
    def _env_fingerprint(env: dict[str, str]) -> _EnvFingerprint:
        return tuple(sorted((str(key), str(value)) for key, value in env.items()))
