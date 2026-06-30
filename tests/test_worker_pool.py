from __future__ import annotations

import asyncio
import time

import pytest

from tui_gateway.run_worker import RunTerminalFrame
from tui_gateway.services.runtime_proxy import RuntimeScope
from tui_gateway.services.worker_pool import WorkerPool
from tui_gateway.services.worker_supervisor import RunWorker


class _FakeProcess:
    _next_pid = 1000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = None

    async def wait(self) -> int | None:
        return self.returncode


class _FakeSupervisor:
    def __init__(self, *, ensure_delay_s: float = 0.0) -> None:
        self.ensure_delay_s = ensure_delay_s
        self.ensure_calls: list[RuntimeScope] = []
        self.ensure_envs: list[dict[str, str]] = []
        self.shutdown_calls: list[tuple[str, str]] = []
        self.shutdown_all_called = False
        self.workers: dict[tuple[str, str], RunWorker] = {}
        self.terminal_events: list[tuple[str, str, RunTerminalFrame]] = []
        self._on_run_terminal = self._record_terminal

    async def ensure(self, scope: RuntimeScope, *, env_overrides=None) -> RunWorker:
        self.ensure_calls.append(scope)
        self.ensure_envs.append(dict(env_overrides or {}))
        if self.ensure_delay_s:
            await asyncio.sleep(self.ensure_delay_s)
        existing = self.workers.get(scope.worker_identity)
        if existing is not None and existing.running():
            existing.mark_used()
            return existing
        worker = RunWorker(
            scope=scope,
            process=_FakeProcess(),
            inbound_queue=asyncio.Queue(),
            created_at=time.time(),
            last_used_at=time.time(),
        )
        self.workers[scope.worker_identity] = worker
        return worker

    async def shutdown(self, scope_key: str, conversation_id: str = "") -> bool:
        self.shutdown_calls.append((scope_key, conversation_id or ""))
        worker = self.workers.pop((scope_key, conversation_id or ""), None)
        if worker is None:
            return False
        worker.process.returncode = -15
        return True

    async def shutdown_all(self) -> None:
        self.shutdown_all_called = True
        for worker in self.workers.values():
            worker.process.returncode = -15
        self.workers.clear()

    async def _record_terminal(
        self,
        scope_key: str,
        conversation_id: str,
        frame: RunTerminalFrame,
    ) -> None:
        self.terminal_events.append((scope_key, conversation_id, frame))


def _profile(env: dict[str, str] | None = None) -> dict:
    return {
        "agent_profile_id": "profile-1",
        "hermes_home": "/tmp/hermes-profile-1",
        "dovie_profile": {
            "id": "profile-1",
            "hermesHomePath": "/tmp/hermes-profile-1",
            "env": dict(env or {}),
        },
    }


@pytest.mark.asyncio
async def test_get_or_spawn_new_conv_spawns_worker() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        lease = await pool.get_or_spawn("conv-1", _profile())

        assert lease.conversation_id == "conv-1"
        assert lease.scope_key == "profile:profile-1"
        assert lease.worker_conversation_id == "conv-1"
        assert lease.running()
        assert len(supervisor.ensure_calls) == 1
        assert supervisor.ensure_calls[0].runtime_scope_key == "profile:profile-1"
        assert supervisor.ensure_calls[0].conversation_id == "conv-1"
        assert supervisor.ensure_calls[0].worker_identity == ("profile:profile-1", "conv-1")
        assert supervisor.ensure_calls[0].agent_profile_id == "profile-1"
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_get_or_spawn_reuses_existing_worker_same_conv() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        first = await pool.get_or_spawn("conv-1", _profile())
        await pool.release("conv-1")
        second = await pool.get_or_spawn("conv-1", _profile())

        assert second.worker is first.worker
        assert len(supervisor.ensure_calls) == 1
        assert pool.stats()["workerCount"] == 1
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_get_or_spawn_passes_profile_env_to_worker_supervisor() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        await pool.get_or_spawn(
            "conv-1",
            _profile({
                "DOVIE_BACKEND_BRIDGE_URL": "http://127.0.0.1:4567/api/dovie/invoke",
                "DOVIE_BACKEND_BRIDGE_TOKEN": "bridge-token",
            }),
        )

        assert supervisor.ensure_envs == [
            {
                "DOVIE_BACKEND_BRIDGE_URL": "http://127.0.0.1:4567/api/dovie/invoke",
                "DOVIE_BACKEND_BRIDGE_TOKEN": "bridge-token",
            }
        ]
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_get_or_spawn_respawns_idle_worker_when_profile_env_changes() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        first = await pool.get_or_spawn("conv-1", _profile())
        await pool.release("conv-1")

        second = await pool.get_or_spawn(
            "conv-1",
            _profile({
                "DOVIE_BACKEND_BRIDGE_URL": "http://127.0.0.1:4567/api/dovie/invoke",
                "DOVIE_BACKEND_BRIDGE_TOKEN": "bridge-token",
            }),
        )

        assert second.worker is not first.worker
        assert not first.running()
        assert supervisor.shutdown_calls == [("profile:profile-1", "conv-1")]
        assert supervisor.ensure_envs == [
            {},
            {
                "DOVIE_BACKEND_BRIDGE_URL": "http://127.0.0.1:4567/api/dovie/invoke",
                "DOVIE_BACKEND_BRIDGE_TOKEN": "bridge-token",
            },
        ]
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_concurrent_get_or_spawn_same_conv_single_spawn() -> None:
    supervisor = _FakeSupervisor(ensure_delay_s=0.01)
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        leases = await asyncio.gather(
            *(pool.get_or_spawn("conv-1", _profile()) for _ in range(10))
        )

        assert len(supervisor.ensure_calls) == 1
        assert len({lease.worker.process.pid for lease in leases}) == 1
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_idle_worker_reaped_after_threshold() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, idle_reap_after_s=0.01, reap_tick_s=60)
    try:
        await pool.get_or_spawn("conv-1", _profile())
        await pool.release("conv-1")
        pool._states[("conv-1", "")].idle_since = time.time() - 10

        await pool._reap_once()

        assert supervisor.shutdown_calls == [("profile:profile-1", "conv-1")]
        assert pool.stats()["workerCount"] == 0
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_active_worker_not_reaped_while_run_inflight() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, idle_reap_after_s=0.01, reap_tick_s=60)
    try:
        await pool.get_or_spawn("conv-1", _profile())
        await pool.record_run_start(
            conversation_id="conv-1",
            run_id="run-1",
            stored_session_id="conv-1",
            turn_id="turn-1",
        )
        await pool.release("conv-1")
        pool._states[("conv-1", "")].idle_since = time.time() - 10

        await pool._reap_once()

        assert supervisor.shutdown_calls == []
        assert pool.stats()["activeWorkerCount"] == 1
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_crash_marks_inflight_runs_failed() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        lease = await pool.get_or_spawn("conv-1", _profile())
        await pool.record_run_start(
            conversation_id="conv-1",
            run_id="run-1",
            stored_session_id="conv-1",
            turn_id="turn-1",
        )
        lease.worker.process.returncode = 1

        await pool._reap_once()

        assert supervisor.shutdown_calls == [("profile:profile-1", "conv-1")]
        assert pool.stats()["workerCount"] == 0
        assert len(supervisor.terminal_events) == 1
        scope_key, conversation_id, frame = supervisor.terminal_events[0]
        assert scope_key == "profile:profile-1"
        assert conversation_id == "conv-1"
        assert frame.run_id == "run-1"
        assert frame.status == "failed"
        assert frame.stored_session_id == "conv-1"
        assert frame.turn_id == "turn-1"
        assert "worker crashed" in frame.message
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_pool_emits_events_with_profile_based_scope_key() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        lease = await pool.get_or_spawn("conv-1", _profile())
        await pool.record_run_start(
            conversation_id="conv-1",
            run_id="run-1",
            stored_session_id="conv-1",
            turn_id="turn-1",
        )
        lease.worker.process.returncode = 1

        await pool._reap_once()

        scope_key, conversation_id, frame = supervisor.terminal_events[0]
        assert scope_key == "profile:profile-1"
        assert conversation_id == "conv-1"
        assert frame.stored_session_id == "conv-1"
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_shutdown_kills_all_workers_and_cancels_reap_task() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    await pool.get_or_spawn("conv-1", _profile())
    await pool.get_or_spawn("conv-2", _profile())
    task = pool._reap_task

    await pool.shutdown()

    assert supervisor.shutdown_all_called is True
    assert task is not None
    assert task.done()
    assert pool.stats()["workerCount"] == 0
