"""Generic platform HTTP callback ingress contracts."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from channels.config import PlatformConfig
from channels.platforms.api_server import APIServerAdapter


class _CallbackAdapter:
    def __init__(self, *, verify_ok=True, verify_code=""):
        self.verify_ok = verify_ok
        self.verify_code = verify_code
        self.dispatched = []
        self.verifier_thread_id = None

    def verify_http_event_request(self, auth_header):
        self.auth_header = auth_header
        self.verifier_thread_id = threading.get_ident()
        return self.verify_ok, self.verify_code

    async def dispatch_http_event(self, payload):
        self.dispatched.append(payload)
        return {"ok": True}


def _adapter(*, api_key=""):
    return APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": api_key} if api_key else {})
    )


def _app(adapter, callback_adapter=None):
    app = web.Application()
    app["api_server_adapter"] = adapter
    if callback_adapter is not None:
        app["platform_event_adapters"] = {"google_chat": callback_adapter}
    app.router.add_post(
        "/api/platforms/{platform}/events",
        adapter._handle_platform_event_callback,
    )
    return app


@pytest.mark.asyncio
async def test_dispatches_authorized_platform_event_without_api_server_key():
    adapter = _adapter(api_key="private-api-key")
    callback_adapter = _CallbackAdapter()
    event_loop_thread = threading.get_ident()

    async with TestClient(TestServer(_app(adapter, callback_adapter))) as client:
        response = await client.post(
            "/api/platforms/google-chat/events",
            headers={"Authorization": "Bearer platform-token"},
            json={"type": "MESSAGE", "message": {"text": "hi"}},
        )
        body = await response.json()

    assert response.status == 200
    assert body == {"ok": True}
    assert callback_adapter.auth_header == "Bearer platform-token"
    assert callback_adapter.dispatched == [
        {"type": "MESSAGE", "message": {"text": "hi"}}
    ]
    assert callback_adapter.verifier_thread_id != event_loop_thread


@pytest.mark.asyncio
async def test_callback_verifier_failure_is_fail_closed():
    adapter = _adapter()

    class _CrashingVerifier(_CallbackAdapter):
        def verify_http_event_request(self, auth_header):
            raise RuntimeError("certificate service unavailable")

    async with TestClient(
        TestServer(_app(adapter, _CrashingVerifier()))
    ) as client:
        response = await client.post(
            "/api/platforms/google_chat/events",
            headers={"Authorization": "Bearer platform-token"},
            json={"type": "MESSAGE"},
        )
        body = await response.json()

    assert response.status == 401
    assert body["error"]["code"] == "platform_event_verifier_error"


@pytest.mark.asyncio
async def test_callback_rejects_invalid_auth_and_malformed_payload():
    adapter = _adapter()
    denied = _CallbackAdapter(verify_ok=False, verify_code="invalid_bearer")
    async with TestClient(TestServer(_app(adapter, denied))) as client:
        response = await client.post(
            "/api/platforms/google_chat/events",
            headers={"Authorization": "Bearer bad"},
            json={"type": "MESSAGE"},
        )
        assert response.status == 401
        assert (await response.json())["error"]["code"] == "invalid_bearer"

    allowed = _CallbackAdapter()
    async with TestClient(TestServer(_app(adapter, allowed))) as client:
        response = await client.post(
            "/api/platforms/google_chat/events",
            headers={"Authorization": "Bearer good"},
            data="{",
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_callback_requires_connected_capable_adapter():
    adapter = _adapter()
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/platforms/google_chat/events",
            json={"type": "MESSAGE"},
        )
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "platform_unavailable"


def test_non_ascii_bearer_token_returns_401_and_exact_unicode_key_matches():
    adapter = _adapter(api_key="sk-tést-kéy")
    request = MagicMock()
    request.headers = {"Authorization": "Bearer ské-wrong"}
    assert adapter._check_auth(request).status == 401

    request.headers = {"Authorization": "Bearer sk-tést-kéy"}
    assert adapter._check_auth(request) is None


def test_route_table_exposes_sessions_and_platform_callbacks():
    routes = {(method, path) for method, path, _ in _adapter()._http_route_table()}

    assert ("GET", "/api/sessions") in routes
    assert ("POST", "/api/sessions/{session_id}/chat/stream") in routes
    assert ("POST", "/api/platforms/{platform}/events") in routes
