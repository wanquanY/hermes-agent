"""API HTTP/SSE boundaries must never expose provider credentials."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from channels.config import PlatformConfig
from channels.platforms.api_server import APIServerAdapter
from channels.platforms.api_server_support import (
    _openai_error,
    _redact_api_error_text,
)
from hermes_agent.application.active_work_registry import ActiveWorkRegistry


def _adapter() -> APIServerAdapter:
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._active_work_registry = ActiveWorkRegistry()
    return adapter


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/api/jobs", adapter._handle_list_jobs)
    return app


def test_redactor_is_forced_and_openai_envelope_uses_it():
    secret = "sk-api-server-leak-1234567890"
    with patch("agent.redact._REDACT_ENABLED", False):
        redacted = _redact_api_error_text(
            f"auth failed OPENAI_API_KEY={secret}"
        )
        envelope = _openai_error(
            f"auth failed OPENAI_API_KEY={secret}"
        )

    assert secret not in redacted
    assert secret not in json.dumps(envelope)
    assert "OPENAI_API_KEY=" in redacted
    assert len(_redact_api_error_text("x" * 100, limit=20)) == 20


@pytest.mark.asyncio
async def test_chat_failure_body_and_headers_are_redacted():
    secret = "sk-chat-leak-1234567890"
    adapter = _adapter()
    result = {
        "final_response": "",
        "completed": False,
        "partial": False,
        "failed": True,
        "error": f"provider failed OPENAI_API_KEY={secret}",
        "messages": [],
    }
    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run_agent:
        run_agent.return_value = (
            result,
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            body = await response.text()

    assert response.status == 502
    assert secret not in body
    assert secret not in response.headers.get("X-Hermes-Error", "")


@pytest.mark.asyncio
async def test_responses_error_fallback_is_redacted():
    secret = "sk-responses-leak-1234567890"
    adapter = _adapter()
    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run_agent:
        run_agent.return_value = (
            {
                "final_response": "",
                "error": f"provider failed OPENAI_API_KEY={secret}",
                "messages": [],
            },
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "hermes-agent", "input": "hello"},
            )
            body = await response.text()

    assert response.status == 200
    assert secret not in body
    assert "OPENAI_API_KEY=" in body


@pytest.mark.asyncio
async def test_cron_endpoint_exception_is_redacted(monkeypatch):
    secret = "sk-cron-leak-1234567890"
    adapter = _adapter()
    module = adapter._cron_api_module()
    monkeypatch.setattr(module, "_CRON_AVAILABLE", True)
    monkeypatch.setattr(
        module,
        "_cron_list",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(f"OPENAI_API_KEY={secret}")
        ),
    )
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.get("/api/jobs")
        body = await response.text()

    assert response.status == 500
    assert secret not in body
    assert "OPENAI_API_KEY=" in body
