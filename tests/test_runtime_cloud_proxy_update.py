from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any

import pytest

from tui_gateway.methods import runtime_cloud_proxy
from tui_gateway.run_worker import (
    RuntimeEnvUpdateFrame,
    _StubBackend,
    _build_default_handler,
    decode_incoming,
    encode_incoming,
)
from tui_gateway.services.runtime_scope import RuntimeScope
from tui_gateway.services.worker_supervisor import RunWorker, WorkerSupervisor


class _FakeSupervisor:
    def __init__(self, notified: int = 0) -> None:
        self.notified = notified
        self.updates: list[dict[str, str]] = []

    async def broadcast_runtime_env_update(self, env_updates: dict[str, str]) -> int:
        self.updates.append(dict(env_updates))
        return self.notified


class _FakeResponder:
    async def resolve(self, _frame) -> bool:
        return False


class _FakeProto:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    async def emit_log(self, level: str, text: str) -> None:
        self.logs.append((level, text))

    async def emit(self, _frame) -> None:
        return None


class _FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.lines.append(data.decode("utf-8"))

    async def drain(self) -> None:
        return None


class _FakeProcess:
    _next_pid = 3000

    def __init__(self, *, running: bool = True) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = None if running else 1
        self.stdin = _FakeStdin()


def _fingerprint(token: str, origin: str) -> str:
    return hashlib.sha256(f"{token}|{origin}".encode()).hexdigest()[:12]


def _managed_proxy_env(origin: str) -> dict[str, str]:
    origin = origin.rstrip("/")
    return {
        "DOVIE_MINERU_PROXY_URL": f"{origin}/api/v1/llm-proxy/v1/document-parse",
        "DOVIE_SERPER_PROXY_URL": f"{origin}/api/v1/llm-proxy/v1/serper-search",
        "DOVIE_WEB_PARSE_PROXY_URL": f"{origin}/api/v1/llm-proxy/v1/web-page-parse",
        "DOVIE_IMAGE_GENERATE_PROXY_URL": f"{origin}/api/v1/llm-proxy/v1/image-generate",
        "DOVIE_VIDEO_GENERATE_PROXY_URL": f"{origin}/api/v1/llm-proxy/v1/video-generate",
        "DOVIE_SKILL_CATEGORIES_URL": f"{origin}/api/v1/llm-proxy/v1/skill-market/categories",
    }


def _install_fake_supervisor(monkeypatch: pytest.MonkeyPatch, supervisor: _FakeSupervisor) -> None:
    monkeypatch.setattr(
        "tui_gateway.services.worker_runtime.worker_supervisor",
        lambda: supervisor,
    )


@pytest.mark.asyncio
async def test_runtime_cloud_proxy_update_sets_main_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _FakeSupervisor()
    _install_fake_supervisor(monkeypatch, supervisor)
    monkeypatch.delenv("DOVIE_LLM_RUNTIME_TOKEN", raising=False)
    monkeypatch.delenv("DOVIE_API_ORIGIN", raising=False)
    for key in _managed_proxy_env("https://old.example.test"):
        monkeypatch.delenv(key, raising=False)

    result = await runtime_cloud_proxy.apply_runtime_cloud_proxy_update(
        {"runtime_token": "token-1", "api_origin": "https://api.example.test"}
    )

    assert result["ok"] is True
    assert os.environ["DOVIE_LLM_RUNTIME_TOKEN"] == "token-1"
    assert os.environ["DOVIE_API_ORIGIN"] == "https://api.example.test"
    assert (
        os.environ["DOVIE_MINERU_PROXY_URL"]
        == "https://api.example.test/api/v1/llm-proxy/v1/document-parse"
    )
    assert supervisor.updates == [
        {
            "DOVIE_LLM_RUNTIME_TOKEN": "token-1",
            "DOVIE_API_ORIGIN": "https://api.example.test",
            **_managed_proxy_env("https://api.example.test"),
        }
    ]


@pytest.mark.asyncio
async def test_runtime_cloud_proxy_update_broadcasts_to_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _FakeSupervisor(notified=2)
    _install_fake_supervisor(monkeypatch, supervisor)

    result = await runtime_cloud_proxy.apply_runtime_cloud_proxy_update(
        {"runtimeToken": "worker-token", "apiOrigin": "https://runtime.example.test"}
    )

    assert result["workers_notified"] == 2
    assert supervisor.updates == [
        {
            "DOVIE_LLM_RUNTIME_TOKEN": "worker-token",
            "DOVIE_API_ORIGIN": "https://runtime.example.test",
            **_managed_proxy_env("https://runtime.example.test"),
        }
    ]


@pytest.mark.asyncio
async def test_runtime_cloud_proxy_update_returns_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _FakeSupervisor()
    _install_fake_supervisor(monkeypatch, supervisor)

    result = await runtime_cloud_proxy.apply_runtime_cloud_proxy_update(
        {"runtime_token": "finger-token", "api_origin": "https://origin.test"}
    )

    assert result["fingerprint"] == _fingerprint("finger-token", "https://origin.test")


@pytest.mark.asyncio
async def test_runtime_cloud_proxy_update_with_empty_token_clears_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _FakeSupervisor()
    _install_fake_supervisor(monkeypatch, supervisor)
    monkeypatch.setenv("DOVIE_LLM_RUNTIME_TOKEN", "old-token")
    monkeypatch.setenv("DOVIE_API_ORIGIN", "https://origin.test")

    result = await runtime_cloud_proxy.apply_runtime_cloud_proxy_update(
        {"runtime_token": "", "api_origin": "https://origin.test"}
    )

    assert "DOVIE_LLM_RUNTIME_TOKEN" not in os.environ
    assert os.environ["DOVIE_API_ORIGIN"] == "https://origin.test"
    assert (
        os.environ["DOVIE_MINERU_PROXY_URL"]
        == "https://origin.test/api/v1/llm-proxy/v1/document-parse"
    )
    assert supervisor.updates[-1]["DOVIE_LLM_RUNTIME_TOKEN"] == ""
    assert result["fingerprint"] == _fingerprint("", "https://origin.test")


@pytest.mark.asyncio
async def test_runtime_cloud_proxy_update_with_empty_origin_clears_managed_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _FakeSupervisor()
    _install_fake_supervisor(monkeypatch, supervisor)
    monkeypatch.setenv("DOVIE_API_ORIGIN", "https://old-origin.test")
    for key, value in _managed_proxy_env("https://old-origin.test").items():
        monkeypatch.setenv(key, value)

    result = await runtime_cloud_proxy.apply_runtime_cloud_proxy_update(
        {"api_origin": ""}
    )

    assert result["ok"] is True
    assert "DOVIE_API_ORIGIN" not in os.environ
    for key in _managed_proxy_env("https://old-origin.test"):
        assert key not in os.environ
        assert supervisor.updates[-1][key] == ""
    assert supervisor.updates[-1]["DOVIE_API_ORIGIN"] == ""


@pytest.mark.asyncio
async def test_runtime_cloud_proxy_update_workers_pick_up_new_token_on_next_getenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOVIE_LLM_RUNTIME_TOKEN", raising=False)
    handler = _build_default_handler(_StubBackend(), _FakeResponder(), set())
    proto = _FakeProto()

    await handler(
        proto,
        RuntimeEnvUpdateFrame(
            env_updates={
                "DOVIE_LLM_RUNTIME_TOKEN": "new-worker-token",
                "DOVIE_API_ORIGIN": "https://worker-origin.test",
            }
        ),
    )

    assert os.getenv("DOVIE_LLM_RUNTIME_TOKEN") == "new-worker-token"
    assert os.getenv("DOVIE_API_ORIGIN") == "https://worker-origin.test"
    assert proto.logs[-1][0] == "info"


@pytest.mark.asyncio
async def test_runtime_cloud_proxy_update_handles_no_workers_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _FakeSupervisor(notified=0)
    _install_fake_supervisor(monkeypatch, supervisor)

    result = await runtime_cloud_proxy.apply_runtime_cloud_proxy_update(
        {"runtime_token": "token-with-no-workers", "api_origin": "https://origin.test"}
    )

    assert result == {
        "ok": True,
        "fingerprint": _fingerprint("token-with-no-workers", "https://origin.test"),
        "workers_notified": 0,
    }


def test_runtime_env_update_frame_round_trips_through_codec() -> None:
    frame = RuntimeEnvUpdateFrame(
        env_updates={"DOVIE_LLM_RUNTIME_TOKEN": "codec-token", "EMPTY": ""}
    )

    encoded = encode_incoming(frame)
    decoded = decode_incoming(encoded)

    assert json.loads(encoded) == {
        "op": "runtime.env.update",
        "env_updates": {"DOVIE_LLM_RUNTIME_TOKEN": "codec-token", "EMPTY": ""},
    }
    assert decoded == frame


@pytest.mark.asyncio
async def test_worker_supervisor_broadcast_runtime_env_update_writes_live_workers() -> None:
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )
    first = RunWorker(
        scope=RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-1"),
        process=_FakeProcess(running=True),
        inbound_queue=asyncio.Queue(),
        created_at=time.time(),
        last_used_at=time.time(),
    )
    stopped = RunWorker(
        scope=RuntimeScope(runtime_scope_key="profile:p", conversation_id="conv-2"),
        process=_FakeProcess(running=False),
        inbound_queue=asyncio.Queue(),
        created_at=time.time(),
        last_used_at=time.time(),
    )
    supervisor._workers[first.identity] = first
    supervisor._workers[stopped.identity] = stopped

    notified = await supervisor.broadcast_runtime_env_update(
        {"DOVIE_LLM_RUNTIME_TOKEN": "broadcast-token"}
    )

    assert notified == 1
    assert len(first.process.stdin.lines) == 1
    assert decode_incoming(first.process.stdin.lines[0]) == RuntimeEnvUpdateFrame(
        env_updates={"DOVIE_LLM_RUNTIME_TOKEN": "broadcast-token"}
    )
    assert stopped.process.stdin.lines == []


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None
