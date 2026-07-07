from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

import pytest

from agent.dovie_attribution import build_dovie_attribution_headers
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars
from tui_gateway.run_worker import (
    RunStartFrame,
    RunTerminalFrame,
    WorkerRunBackend,
    _build_default_handler,
    encode_incoming,
)
from tui_gateway.services import worker_runtime
from tui_gateway.services.agent_run_backend import AgentRunBackend
from tui_gateway.services.runtime_scope import RuntimeScope
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


def _read_dovie_from_nested_thread(depth: int = 4, *, default: str = "") -> str:
    values: list[str] = []
    errors: list[BaseException] = []

    def run(level: int) -> None:
        try:
            if level <= 0:
                values.append(get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", default))
                return
            child = threading.Thread(target=lambda: run(level - 1), daemon=True)
            child.start()
            child.join(timeout=2.0)
            if child.is_alive():
                errors.append(TimeoutError(f"nested thread level {level} did not finish"))
        except BaseException as exc:  # noqa: BLE001 - surface thread assertion failures.
            errors.append(exc)

    top = threading.Thread(target=lambda: run(depth), daemon=True)
    top.start()
    top.join(timeout=2.0)
    if top.is_alive():
        raise TimeoutError("top-level thread did not finish")
    if errors:
        raise errors[0]
    assert len(values) == 1
    return values[0]


def test_process_env_dovie_context_is_visible_across_nested_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_DOVIE_PRODUCT_CONTEXT", "sentinel-env")

    assert _read_dovie_from_nested_thread(depth=6) == "sentinel-env"


def test_set_session_vars_writes_dovie_context_to_process_env_for_threads() -> None:
    tokens = set_session_vars(dovie_product_context="sentinel-set-session")
    try:
        assert os.environ["HERMES_DOVIE_PRODUCT_CONTEXT"] == "sentinel-set-session"
        assert _read_dovie_from_nested_thread(depth=5) == "sentinel-set-session"
    finally:
        clear_session_vars(tokens)


def test_clear_session_vars_removes_dovie_context_for_new_threads() -> None:
    tokens = set_session_vars(dovie_product_context="sentinel-clear")
    clear_session_vars(tokens)

    assert _read_dovie_from_nested_thread(depth=3, default="default") == "default"
    assert build_dovie_attribution_headers() == {}


def test_dovie_context_clear_restores_missing_process_env() -> None:
    assert os.environ.get("HERMES_DOVIE_PRODUCT_CONTEXT") is None

    tokens = set_session_vars(dovie_product_context="turn-context")
    try:
        assert os.environ["HERMES_DOVIE_PRODUCT_CONTEXT"] == "turn-context"
    finally:
        clear_session_vars(tokens)

    assert os.environ.get("HERMES_DOVIE_PRODUCT_CONTEXT") is None


def test_dovie_context_clear_restores_existing_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_DOVIE_PRODUCT_CONTEXT", "outer-context")

    tokens = set_session_vars(dovie_product_context="turn-context")
    try:
        assert os.environ["HERMES_DOVIE_PRODUCT_CONTEXT"] == "turn-context"
    finally:
        clear_session_vars(tokens)

    assert os.environ["HERMES_DOVIE_PRODUCT_CONTEXT"] == "outer-context"


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
    assert get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "default") == "default"
    assert _read_dovie_from_nested_thread(depth=2, default="default") == "default"
    assert build_dovie_attribution_headers() == {}


@pytest.mark.asyncio
async def test_agent_run_backend_refuses_second_turn_while_first_is_active() -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(_frame: RunStartFrame, _cancel: threading.Event) -> None:
        started.set()
        assert release.wait(timeout=5.0)

    backend = AgentRunBackend(runner=runner)
    proto = _Proto()
    first_task = asyncio.create_task(
        backend.start(
            RunStartFrame(
                run_id="run-active",
                turn_id="turn-active",
                stored_session_id="conv-active",
                prompt="first",
            ),
            proto.emit,
        )
    )

    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, started.wait, 2.0)

    await backend.start(
        RunStartFrame(
            run_id="run-refused",
            turn_id="turn-refused",
            stored_session_id="conv-refused",
            prompt="second",
        ),
        proto.emit,
    )

    refused = [
        frame
        for frame in proto.frames
        if isinstance(frame, RunTerminalFrame) and frame.run_id == "run-refused"
    ]
    assert refused == [
        RunTerminalFrame(
            run_id="run-refused",
            status="failed",
            stored_session_id="conv-refused",
            turn_id="turn-refused",
            message="another run already active in this worker",
        )
    ]

    release.set()
    await asyncio.wait_for(first_task, timeout=5.0)
