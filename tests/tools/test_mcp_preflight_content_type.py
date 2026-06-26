from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest


class _FakeAsyncClient:
    instances: list["_FakeAsyncClient"] = []
    head_result = None
    get_result = None
    head_exc: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def head(self, url, headers=None):
        self.requests.append(("HEAD", url, dict(headers or {})))
        if self.__class__.head_exc is not None:
            raise self.__class__.head_exc
        return self.__class__.head_result

    async def get(self, url, headers=None):
        self.requests.append(("GET", url, dict(headers or {})))
        return self.__class__.get_result


def _response(status: int, content_type: str | None):
    headers = {}
    if content_type is not None:
        headers["content-type"] = content_type
    return SimpleNamespace(status_code=status, headers=headers)


def _install_fake_client(monkeypatch, *, head, get=None, head_exc=None):
    head_value = head
    get_value = get
    head_exc_value = head_exc

    class Client(_FakeAsyncClient):
        instances = []
        head_result = head_value
        get_result = get_value
        head_exc = head_exc_value

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return Client


@pytest.mark.asyncio
async def test_preflight_rejects_successful_non_mcp_content_type(monkeypatch):
    from tools.mcp_tool import MCPServerTask, NonMcpEndpointError

    client_cls = _install_fake_client(
        monkeypatch,
        head=_response(200, "text/html; charset=utf-8"),
    )
    server = MCPServerTask("web-root")

    with pytest.raises(NonMcpEndpointError, match="web-root.*text/html"):
        await server._preflight_content_type(
            "https://example.test/",
            headers={"authorization": "Bearer test"},
            ssl_verify=False,
            timeout=0.25,
        )

    client = client_cls.instances[0]
    assert client.kwargs["verify"] is False
    assert client.kwargs["follow_redirects"] is True
    assert client.requests == [
        ("HEAD", "https://example.test/", {"authorization": "Bearer test"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/event-stream; charset=utf-8"],
)
async def test_preflight_allows_mcp_content_types(monkeypatch, content_type):
    from tools.mcp_tool import MCPServerTask

    _install_fake_client(monkeypatch, head=_response(200, content_type))
    await MCPServerTask("ok")._preflight_content_type("https://example.test/mcp")


@pytest.mark.asyncio
async def test_preflight_get_fallback_rejects_non_mcp_response(monkeypatch):
    from tools.mcp_tool import MCPServerTask, NonMcpEndpointError

    client_cls = _install_fake_client(
        monkeypatch,
        head=_response(405, "text/html"),
        get=_response(200, "text/plain"),
    )
    server = MCPServerTask("plain")

    with pytest.raises(NonMcpEndpointError, match="text/plain"):
        await server._preflight_content_type("https://example.test/mcp")

    assert [method for method, *_ in client_cls.instances[0].requests] == [
        "HEAD",
        "GET",
    ]


@pytest.mark.asyncio
async def test_preflight_lets_network_errors_fall_through(monkeypatch):
    from tools.mcp_tool import MCPServerTask

    _install_fake_client(
        monkeypatch,
        head=None,
        head_exc=httpx.ConnectError("connection refused"),
    )
    await MCPServerTask("net")._preflight_content_type("https://example.test/mcp")


@pytest.mark.asyncio
async def test_run_fast_fails_non_mcp_endpoint_without_retry(monkeypatch):
    from tools.mcp_tool import MCPServerTask, NonMcpEndpointError

    calls = {"run_http": 0}

    async def fake_preflight(self, *args, **kwargs):
        raise NonMcpEndpointError("not an MCP endpoint")

    async def fake_run_http(self, config):
        calls["run_http"] += 1
        raise AssertionError("run_http should not be called")

    monkeypatch.setattr(MCPServerTask, "_preflight_content_type", fake_preflight)
    monkeypatch.setattr(MCPServerTask, "_run_http", fake_run_http)

    server = MCPServerTask("bad")
    await server.run({"url": "https://example.test/"})

    assert isinstance(server._error, NonMcpEndpointError)
    assert server._ready.is_set()
    assert calls["run_http"] == 0
