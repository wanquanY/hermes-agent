from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tui_gateway.run_worker import RunCancelFrame
from tui_gateway.services.runtime_scope import RuntimeScope
from tui_gateway.services.worker_supervisor import RunWorker, WorkerSupervisor


class _FakeStdin:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def is_closing(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        return None


class _FakeProcess:
    _next_pid = 7000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = None
        self.stdin = _FakeStdin()


def _supervisor() -> WorkerSupervisor:
    async def _event(_scope: str, _conv: str, _frame: Any) -> None:
        return None

    return WorkerSupervisor(
        on_event=_event,
        on_interactive_request=_event,
        on_run_terminal=_event,
        on_log=_event,
    )


def _worker(scope: RuntimeScope) -> RunWorker:
    return RunWorker(
        scope=scope,
        process=_FakeProcess(),
        inbound_queue=asyncio.Queue(),
        created_at=time.time(),
        last_used_at=time.time(),
    )


def test_runtime_scope_worker_identity_is_tuple_of_scope_and_conv() -> None:
    scope = RuntimeScope(
        agent_profile_id="profile-1",
        runtime_scope_key="profile:profile-1",
        conversation_id="conv-1",
    )

    assert scope.worker_identity == ("profile:profile-1", "conv-1")


def test_runtime_scope_default_conv_id_empty() -> None:
    scope = RuntimeScope(runtime_scope_key="profile:profile-1")

    assert scope.conversation_id == ""
    assert scope.worker_identity == ("profile:profile-1", "")


@pytest.mark.asyncio
async def test_supervisor_ensure_with_different_conv_spawns_different_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _supervisor()
    spawned: list[RuntimeScope] = []

    async def _spawn(scope: RuntimeScope, _env: dict[str, str]) -> RunWorker:
        spawned.append(scope)
        return _worker(scope)

    monkeypatch.setattr(supervisor, "_spawn_locked", _spawn)

    first = await supervisor.ensure(RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-1"))
    second = await supervisor.ensure(RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-2"))

    assert first is not second
    assert [scope.worker_identity for scope in spawned] == [
        ("profile:p", "conv-1"),
        ("profile:p", "conv-2"),
    ]


@pytest.mark.asyncio
async def test_supervisor_ensure_with_same_conv_reuses_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _supervisor()
    spawned = 0

    async def _spawn(scope: RuntimeScope, _env: dict[str, str]) -> RunWorker:
        nonlocal spawned
        spawned += 1
        return _worker(scope)

    monkeypatch.setattr(supervisor, "_spawn_locked", _spawn)

    scope = RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-1")
    first = await supervisor.ensure(scope)
    second = await supervisor.ensure(scope)

    assert second is first
    assert spawned == 1


@pytest.mark.asyncio
async def test_supervisor_send_routes_by_compound_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _supervisor()

    async def _spawn(scope: RuntimeScope, _env: dict[str, str]) -> RunWorker:
        return _worker(scope)

    monkeypatch.setattr(supervisor, "_spawn_locked", _spawn)

    first = await supervisor.ensure(RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-1"))
    second = await supervisor.ensure(RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-2"))

    ok = await supervisor.send("profile:p", "conv-2", RunCancelFrame(run_id="run-2"))

    assert ok is True
    assert first.process.stdin.lines == []
    assert second.process.stdin.lines
    assert b'"run-2"' in second.process.stdin.lines[0]


@pytest.mark.asyncio
async def test_supervisor_shutdown_only_kills_specific_conv_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _supervisor()
    terminated: list[tuple[str, str]] = []

    async def _spawn(scope: RuntimeScope, _env: dict[str, str]) -> RunWorker:
        return _worker(scope)

    async def _terminate(worker: RunWorker) -> None:
        terminated.append(worker.identity)
        worker.process.returncode = -15

    monkeypatch.setattr(supervisor, "_spawn_locked", _spawn)
    monkeypatch.setattr(supervisor, "_terminate", _terminate)

    await supervisor.ensure(RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-1"))
    survivor = await supervisor.ensure(RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-2"))

    assert await supervisor.shutdown("profile:p", "conv-1") is True

    assert terminated == [("profile:p", "conv-1")]
    assert supervisor.get("profile:p", "conv-1") is None
    assert supervisor.get("profile:p", "conv-2") is survivor
