from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tui_gateway.services import worker_runtime
from tui_gateway.services.runtime_proxy import RuntimeScope
from tui_gateway.services.worker_supervisor import RunWorker


class _Transport:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    async def write_async(self, frame: dict[str, Any]) -> bool:
        self.written.append(frame)
        return True


class _Process:
    pid = 9001
    returncode = None


def _worker(scope: RuntimeScope) -> RunWorker:
    return RunWorker(
        scope=scope,
        process=_Process(),
        inbound_queue=asyncio.Queue(),
        created_at=0.0,
        last_used_at=0.0,
    )


class _Supervisor:
    def __init__(self) -> None:
        self.ensure_calls: list[RuntimeScope] = []
        self.sent: list[tuple[str, str, Any]] = []

    async def ensure(self, scope: RuntimeScope) -> RunWorker:
        self.ensure_calls.append(scope)
        return _worker(scope)

    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
        self.sent.append((scope_key, conversation_id, frame))
        return True


class _Router:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    def record_run_start(self, **kwargs: Any) -> None:
        self.starts.append(dict(kwargs))

    def forget_run(self, _run_id: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_runtime_singletons():
    worker_runtime._reset_for_tests()
    yield
    worker_runtime._reset_for_tests()


async def _submit(monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]):
    supervisor = _Supervisor()
    router = _Router()
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: supervisor)
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: router)
    transport = _Transport()

    handled = await worker_runtime.primary_dispatch(
        {"jsonrpc": "2.0", "id": "rpc-1", "method": "run.submit", "params": params},
        transport,
    )

    assert handled is True
    assert transport.written[0]["result"]["status"] == "queued"
    return supervisor, router, transport


@pytest.mark.asyncio
async def test_run_submit_with_dovie_profile_id_propagates_to_worker_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, router, _transport = await _submit(
        monkeypatch,
        {
            "text": "hello",
            "stored_session_id": "conv-1",
            "runtime_scope_key": "conv-1",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/agent-a-home",
            },
        },
    )

    scope = supervisor.ensure_calls[0]
    assert scope.agent_profile_id == "agent-a"
    assert scope.runtime_scope_key == "profile:agent-a"
    assert scope.conversation_id == "conv-1"
    assert router.starts[0]["conversation_id"] == "conv-1"

    sent_scope, sent_conversation, frame = supervisor.sent[0]
    assert sent_scope == "profile:agent-a"
    assert sent_conversation == "conv-1"
    assert frame.params["agent_profile_id"] == "agent-a"
    assert frame.params["runtime_scope_key"] == "profile:agent-a"
    assert frame.params["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_run_submit_with_agent_profile_id_propagates_to_worker_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, _router, transport = await _submit(
        monkeypatch,
        {
            "text": "hello",
            "stored_session_id": "conv-2",
            "agentProfileId": "agent-b",
        },
    )

    scope = supervisor.ensure_calls[0]
    assert scope.agent_profile_id == "agent-b"
    assert scope.runtime_scope_key == "profile:agent-b"
    assert scope.conversation_id == "conv-2"

    _sent_scope, _sent_conversation, frame = supervisor.sent[0]
    assert frame.params["agent_profile_id"] == "agent-b"
    assert frame.params["agentProfileId"] == "agent-b"
    assert transport.written[0]["result"]["runtime_scope_key"] == "profile:agent-b"
    assert transport.written[0]["result"]["conversation_id"] == "conv-2"
