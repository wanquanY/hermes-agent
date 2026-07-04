from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from agent.dovie_attribution import build_dovie_attribution_headers
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars
from tui_gateway.run_worker import (
    RunStartFrame,
    WorkerRunBackend,
    _build_default_handler,
    encode_incoming,
)
from tui_gateway.services.agent_run_backend import AgentRunBackend
from tui_gateway.services import worker_runtime
from tui_gateway.services.runtime_proxy import RuntimeScope
from tui_gateway.services.worker_supervisor import RunWorker


def _context(query_id: str) -> dict[str, Any]:
    return {
        "cloud_query": {
            "query_id": query_id,
            "root_query_id": "root-query-1",
            "agent_run_id": f"agent-run-{query_id}",
            "query_context_token": f"context-token-{query_id}",
        },
        "root_agent_profile_id": "profile-root",
        "executing_agent_profile_id": f"profile-{query_id}",
        "agent_role": "team_member",
        "sourceSessionId": "conversation-1",
        "sourceRunId": f"run-{query_id}",
        "sourceTurnId": f"turn-{query_id}",
        "sourceClientMessageId": f"client-message-{query_id}",
    }


def _context_json(query_id: str) -> str:
    return json.dumps(_context(query_id), ensure_ascii=False, separators=(",", ":"))


@pytest.fixture(autouse=True)
def _clear_dovie_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_DOVIE_PRODUCT_CONTEXT", raising=False)
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)
    worker_runtime._reset_for_tests()
    yield
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)
    worker_runtime._reset_for_tests()


class _Transport:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    async def write_async(self, frame: dict[str, Any]) -> bool:
        self.written.append(frame)
        return True


class _Process:
    pid = 9701
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
        self.sent: list[tuple[str, str, Any]] = []

    async def ensure(self, scope: RuntimeScope, *, env_overrides=None) -> RunWorker:
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


@pytest.mark.asyncio
async def test_primary_dispatch_run_start_payload_carries_dovie_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _Supervisor()
    router = _Router()
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: supervisor)
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: router)
    transport = _Transport()

    handled = await worker_runtime.primary_dispatch(
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "method": "run.submit",
            "params": {
                "text": "hello",
                "stored_session_id": "conv-1",
                "runtime_scope_key": "profile:member-a",
                "agent_profile_id": "member-a",
                "dovie_product_context": _context("query-ipc"),
            },
        },
        transport,
    )

    assert handled is True
    assert transport.written[0]["result"]["status"] == "queued"
    assert len(supervisor.sent) == 1

    _scope_key, _conversation_id, frame = supervisor.sent[0]
    expected = _context_json("query-ipc")
    assert frame.dovie_product_context == expected
    assert frame.params["dovie_product_context"] == expected

    wire_payload = json.loads(encode_incoming(frame))
    assert wire_payload["dovie_product_context"] == expected
    assert wire_payload["params"]["dovie_product_context"] == expected


class _Proto:
    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def emit(self, frame: Any) -> None:
        self.frames.append(frame)

    async def emit_log(self, _level: str, _text: str) -> None:
        return None


class _Responder:
    async def resolve(self, _frame: Any) -> bool:
        return True


class _RecordingBackend(WorkerRunBackend):
    def __init__(self) -> None:
        self.seen_context = ""
        self.seen_headers: dict[str, str] = {}

    async def start(self, _frame: RunStartFrame, _emit: Any) -> None:
        self.seen_context = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        self.seen_headers = build_dovie_attribution_headers()


@pytest.mark.asyncio
async def test_worker_run_start_sets_dovie_context_for_handler_scope() -> None:
    backend = _RecordingBackend()
    handler = _build_default_handler(backend, _Responder(), set())

    await handler(
        _Proto(),
        RunStartFrame(
            run_id="run-ctx",
            turn_id="turn-ctx",
            stored_session_id="conv-ctx",
            prompt="hello",
            params={"dovie_product_context": _context_json("query-handler")},
            dovie_product_context=_context_json("query-handler"),
        ),
    )

    assert backend.seen_context == _context_json("query-handler")
    assert backend.seen_headers["X-Dovie-Query-Id"] == "query-handler"
    assert backend.seen_headers["X-Dovie-Root-Agent-Profile-Id"] == "profile-root"
    assert backend.seen_headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-query-handler"
    assert backend.seen_headers["X-Dovie-Agent-Role"] == "team_member"


@pytest.mark.asyncio
async def test_worker_run_start_clears_dovie_context_after_turn() -> None:
    backend = _RecordingBackend()
    handler = _build_default_handler(backend, _Responder(), set())

    await handler(
        _Proto(),
        RunStartFrame(
            run_id="run-clear",
            turn_id="turn-clear",
            stored_session_id="conv-clear",
            prompt="hello",
            params={"dovie_product_context": _context_json("query-clear")},
            dovie_product_context=_context_json("query-clear"),
        ),
    )

    assert backend.seen_headers["X-Dovie-Query-Id"] == "query-clear"
    assert get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "default") == ""
    assert build_dovie_attribution_headers() == {}


@pytest.mark.asyncio
async def test_agent_run_backend_thread_sees_worker_handler_dovie_context() -> None:
    seen: dict[str, Any] = {}

    def runner(frame: RunStartFrame, _cancel: threading.Event) -> None:
        seen["run_id"] = frame.run_id
        seen["thread_name"] = threading.current_thread().name
        seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        seen["headers"] = build_dovie_attribution_headers()

    backend = AgentRunBackend(runner=runner)
    proto = _Proto()
    tokens = set_session_vars(dovie_product_context=_context_json("query-thread"))
    try:
        await backend.start(
            RunStartFrame(
                run_id="run-thread",
                turn_id="turn-thread",
                stored_session_id="conv-thread",
                prompt="hello",
                params={"dovie_product_context": _context_json("query-thread")},
                dovie_product_context=_context_json("query-thread"),
            ),
            proto.emit,
        )
    finally:
        clear_session_vars(tokens)

    assert seen["run_id"] == "run-thread"
    assert seen["thread_name"].startswith("agent-run[run-thread]")
    assert seen["context"] == _context_json("query-thread")
    assert seen["headers"]["X-Dovie-Query-Id"] == "query-thread"
    assert seen["headers"]["X-Dovie-Root-Agent-Profile-Id"] == "profile-root"


@pytest.mark.asyncio
async def test_concurrent_agent_run_backend_threads_keep_dovie_context_isolated() -> None:
    barrier = threading.Barrier(2)
    seen: dict[str, str] = {}
    seen_lock = threading.Lock()

    def runner(frame: RunStartFrame, _cancel: threading.Event) -> None:
        barrier.wait(timeout=2.0)
        headers = build_dovie_attribution_headers()
        with seen_lock:
            seen[frame.run_id] = headers["X-Dovie-Query-Id"]

    async def run_turn(run_id: str, query_id: str) -> None:
        backend = AgentRunBackend(runner=runner)
        proto = _Proto()
        tokens = set_session_vars(dovie_product_context=_context_json(query_id))
        try:
            await backend.start(
                RunStartFrame(
                    run_id=run_id,
                    turn_id=f"turn-{query_id}",
                    stored_session_id=f"conv-{query_id}",
                    prompt="hello",
                    params={"dovie_product_context": _context_json(query_id)},
                    dovie_product_context=_context_json(query_id),
                ),
                proto.emit,
            )
        finally:
            clear_session_vars(tokens)

    await asyncio.gather(
        run_turn("run-a-thread", "query-a-thread"),
        run_turn("run-b-thread", "query-b-thread"),
    )

    assert seen == {
        "run-a-thread": "query-a-thread",
        "run-b-thread": "query-b-thread",
    }


@pytest.mark.asyncio
async def test_agent_run_backend_thread_context_is_cleared_between_turns() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    def runner(frame: RunStartFrame, _cancel: threading.Event) -> None:
        seen.append(
            (
                frame.run_id,
                get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", ""),
                build_dovie_attribution_headers(),
            )
        )

    backend = AgentRunBackend(runner=runner)
    proto = _Proto()
    first_context = _context_json("query-first-thread")
    tokens = set_session_vars(dovie_product_context=first_context)
    try:
        await backend.start(
            RunStartFrame(
                run_id="run-first-thread",
                turn_id="turn-first-thread",
                stored_session_id="conv-first-thread",
                prompt="first",
                params={"dovie_product_context": first_context},
                dovie_product_context=first_context,
            ),
            proto.emit,
        )
    finally:
        clear_session_vars(tokens)

    assert get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "default") == ""

    await backend.start(
        RunStartFrame(
            run_id="run-after-clear",
            turn_id="turn-after-clear",
            stored_session_id="conv-after-clear",
            prompt="second",
            params={},
        ),
        proto.emit,
    )

    assert seen[0][0] == "run-first-thread"
    assert seen[0][1] == first_context
    assert seen[0][2]["X-Dovie-Query-Id"] == "query-first-thread"
    assert seen[1] == ("run-after-clear", "", {})


class _ConcurrentBackend(WorkerRunBackend):
    def __init__(self) -> None:
        self._arrived = 0
        self._ready = asyncio.Event()
        self.seen: dict[str, str] = {}

    async def start(self, frame: RunStartFrame, _emit: Any) -> None:
        self._arrived += 1
        if self._arrived == 2:
            self._ready.set()
        await asyncio.wait_for(self._ready.wait(), timeout=1.0)
        await asyncio.sleep(0)
        self.seen[frame.run_id] = build_dovie_attribution_headers()["X-Dovie-Query-Id"]


@pytest.mark.asyncio
async def test_concurrent_worker_turns_keep_dovie_context_isolated() -> None:
    backend = _ConcurrentBackend()
    handler = _build_default_handler(backend, _Responder(), set())
    proto = _Proto()

    await asyncio.gather(
        handler(
            proto,
            RunStartFrame(
                run_id="run-a",
                turn_id="turn-a",
                stored_session_id="conv-a",
                prompt="a",
                params={"dovie_product_context": _context_json("query-a")},
                dovie_product_context=_context_json("query-a"),
            ),
        ),
        handler(
            proto,
            RunStartFrame(
                run_id="run-b",
                turn_id="turn-b",
                stored_session_id="conv-b",
                prompt="b",
                params={"dovie_product_context": _context_json("query-b")},
                dovie_product_context=_context_json("query-b"),
            ),
        ),
    )

    assert backend.seen == {
        "run-a": "query-a",
        "run-b": "query-b",
    }
