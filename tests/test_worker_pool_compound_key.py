from __future__ import annotations

import asyncio
import time

import pytest

from tui_gateway.services.runtime_scope import RuntimeScope
from tui_gateway.services.worker_pool import WorkerPool
from tui_gateway.services.worker_supervisor import RunWorker


class _FakeProcess:
    _next_pid = 8000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = None

    async def wait(self) -> int | None:
        return self.returncode


class _FakeSupervisor:
    def __init__(self) -> None:
        self.ensure_calls: list[RuntimeScope] = []
        self.shutdown_calls: list[tuple[str, str]] = []
        self.shutdown_all_called = False
        self.workers: dict[tuple[str, str], RunWorker] = {}

    async def ensure(self, scope: RuntimeScope, *, env_overrides=None) -> RunWorker:
        self.ensure_calls.append(scope)
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


def _profile(profile_id: str, hermes_home: str, scope_key: str) -> dict:
    return {
        "agent_profile_id": profile_id,
        "agentProfileId": profile_id,
        "id": profile_id,
        "hermes_home": hermes_home,
        "hermesHomePath": hermes_home,
        "runtime_scope_key": scope_key,
        "runtimeScopeKey": scope_key,
        "dovie_profile": {
            "id": profile_id,
            "hermesHomePath": hermes_home,
            "runtimeScopeKey": scope_key,
        },
    }


@pytest.mark.asyncio
async def test_worker_pool_distinct_workers_for_distinct_scopes_same_conv() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-1"
        leader_scope = f"team:{conv_id}:leader-conversation"
        member_scope = f"member-chat:{conv_id}:member-1"

        leader = await pool.get_or_spawn(
            conv_id,
            _profile("leader", "/profiles/leader", leader_scope),
            scope_key=leader_scope,
        )
        member = await pool.get_or_spawn(
            conv_id,
            _profile("member-1", "/profiles/member-1", member_scope),
            scope_key=member_scope,
        )

        assert leader.worker is not member.worker
        assert leader.worker.process.pid != member.worker.process.pid
        assert len(supervisor.ensure_calls) == 2
        assert {call.runtime_scope_key for call in supervisor.ensure_calls} == {
            leader_scope,
            member_scope,
        }
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_pool_reuses_worker_for_same_scope_same_conv() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-1"
        scope = f"member-chat:{conv_id}:member-1"
        profile = _profile("member-1", "/profiles/member-1", scope)

        first = await pool.get_or_spawn(conv_id, profile, scope_key=scope)
        await pool.release(conv_id, scope_key=scope)
        second = await pool.get_or_spawn(conv_id, profile, scope_key=scope)

        assert second.worker is first.worker
        assert len(supervisor.ensure_calls) == 1
        assert pool.stats()["workerCount"] == 1
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_member_chat_gets_member_profile_worker_not_leader() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-2d2f5ae3"
        leader_scope = f"team:{conv_id}:leader-conversation"
        member_scope = f"member-chat:{conv_id}:8f52b4a1"
        leader_home = "/tmp/hermes/profiles/leader"
        member_home = "/tmp/hermes/profiles/agent-2"

        leader = await pool.get_or_spawn(
            conv_id,
            _profile("leader-profile", leader_home, leader_scope),
            scope_key=leader_scope,
        )
        await pool.release(conv_id, scope_key=leader_scope)
        member = await pool.get_or_spawn(
            conv_id,
            _profile("agent-2", member_home, member_scope),
            scope_key=member_scope,
        )

        assert leader.worker is not member.worker
        assert leader.worker.hermes_home == leader_home
        assert member.worker.hermes_home == member_home
        assert leader.scope_key == leader_scope
        assert member.scope_key == member_scope
        assert supervisor.ensure_calls[0].hermes_home == leader_home
        assert supervisor.ensure_calls[1].hermes_home == member_home
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_release_without_scope_key_releases_all_conv_workers() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-1"
        leader_scope = f"team:{conv_id}:leader-conversation"
        member_scope = f"member-chat:{conv_id}:member-1"

        await pool.get_or_spawn(
            conv_id,
            _profile("leader", "/profiles/leader", leader_scope),
            scope_key=leader_scope,
        )
        await pool.get_or_spawn(
            conv_id,
            _profile("member-1", "/profiles/member-1", member_scope),
            scope_key=member_scope,
        )
        await pool.release(conv_id)

        assert all(state.idle_since is not None for state in pool._states.values())
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_release_with_scope_key_releases_only_that_worker() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-1"
        leader_scope = f"team:{conv_id}:leader-conversation"
        member_scope = f"member-chat:{conv_id}:member-1"

        await pool.get_or_spawn(
            conv_id,
            _profile("leader", "/profiles/leader", leader_scope),
            scope_key=leader_scope,
        )
        await pool.get_or_spawn(
            conv_id,
            _profile("member-1", "/profiles/member-1", member_scope),
            scope_key=member_scope,
        )
        await pool.release(conv_id, scope_key=member_scope)

        assert pool._states[(conv_id, leader_scope)].idle_since is None
        assert pool._states[(conv_id, member_scope)].idle_since is not None
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_kill_with_scope_key_only_kills_that_worker() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-1"
        leader_scope = f"team:{conv_id}:leader-conversation"
        member_scope = f"member-chat:{conv_id}:member-1"

        leader = await pool.get_or_spawn(
            conv_id,
            _profile("leader", "/profiles/leader", leader_scope),
            scope_key=leader_scope,
        )
        member = await pool.get_or_spawn(
            conv_id,
            _profile("member-1", "/profiles/member-1", member_scope),
            scope_key=member_scope,
        )

        assert await pool.kill(conv_id, scope_key=member_scope) is True

        assert leader.running()
        assert not member.running()
        assert (conv_id, leader_scope) in pool._states
        assert (conv_id, member_scope) not in pool._states
        assert supervisor.shutdown_calls == [(member_scope, conv_id)]
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_kill_without_scope_key_kills_all_conv_workers() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-1"
        leader_scope = f"team:{conv_id}:leader-conversation"
        member_scope = f"member-chat:{conv_id}:member-1"

        leader = await pool.get_or_spawn(
            conv_id,
            _profile("leader", "/profiles/leader", leader_scope),
            scope_key=leader_scope,
        )
        member = await pool.get_or_spawn(
            conv_id,
            _profile("member-1", "/profiles/member-1", member_scope),
            scope_key=member_scope,
        )

        assert await pool.kill(conv_id) is True

        assert not leader.running()
        assert not member.running()
        assert pool._states == {}
        assert supervisor.shutdown_calls == [
            (member_scope, conv_id),
            (leader_scope, conv_id),
        ]
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_pool_state_keyed_by_compound_tuple() -> None:
    supervisor = _FakeSupervisor()
    pool = WorkerPool(supervisor, reap_tick_s=60)
    try:
        conv_id = "team-conversation-1"
        leader_scope = f"team:{conv_id}:leader-conversation"
        member_scope = f"member-chat:{conv_id}:member-1"

        await pool.get_or_spawn(
            conv_id,
            _profile("leader", "/profiles/leader", leader_scope),
            scope_key=leader_scope,
        )
        await pool.get_or_spawn(
            conv_id,
            _profile("member-1", "/profiles/member-1", member_scope),
            scope_key=member_scope,
        )

        assert set(pool._states) == {
            (conv_id, leader_scope),
            (conv_id, member_scope),
        }
        assert all(isinstance(key, tuple) and len(key) == 2 for key in pool._states)
    finally:
        await pool.shutdown()
