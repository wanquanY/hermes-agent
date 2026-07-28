"""API model-route behavior and profile isolation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope
from channels.config import PlatformConfig
from channels.platforms.api_server import APIServerAdapter
from channels.platforms.api_server_model_routes import parse_api_model_routes
from channels.platforms.api_server_routing import _api_request_profile
from hermes_agent.application.active_work_registry import ActiveWorkRegistry
from hermes_gateway.config import GatewayConfig


@pytest.fixture(autouse=True)
def _reset_multiplex_state():
    secret_scope.set_multiplex_active(False)
    yield
    secret_scope.set_multiplex_active(False)


def _adapter(routes) -> APIServerAdapter:
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"model_routes": routes})
    )
    adapter._active_work_registry = ActiveWorkRegistry()
    return adapter


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/models", adapter._handle_models)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    return app


def test_route_parser_is_typed_strict_and_secret_safe():
    routes = parse_api_model_routes(
        {
            "valid": {
                "model": "provider/model",
                "provider": "provider",
                "api_key": "route-secret",
                "unknown": "discard-me",
            },
            "missing-model": {"provider": "provider"},
            "invalid": "provider/model",
        }
    )

    assert set(routes) == {"valid"}
    assert routes["valid"].model == "provider/model"
    assert routes["valid"].provider == "provider"
    assert routes["valid"].api_key == "route-secret"
    assert "route-secret" not in repr(routes["valid"])
    assert not hasattr(routes["valid"], "unknown")


@pytest.mark.asyncio
async def test_models_endpoint_lists_aliases_without_route_credentials():
    adapter = _adapter(
        {
            "client-model": {
                "model": "provider/real-model",
                "api_key": "route-secret",
            }
        }
    )
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.get("/v1/models")
        payload = await response.json()

    assert response.status == 200
    entries = {entry["id"]: entry for entry in payload["data"]}
    assert entries["client-model"]["root"] == "provider/real-model"
    assert "route-secret" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/v1/chat/completions",
            {
                "model": "client-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        (
            "/v1/responses",
            {"model": "client-model", "input": "hello"},
        ),
    ],
)
async def test_openai_handlers_pass_resolved_route(path, body):
    adapter = _adapter(
        {"client-model": {"model": "provider/real-model"}}
    )
    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run_agent:
        run_agent.return_value = (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(path, json=body)

    assert response.status == 200
    route = run_agent.call_args.kwargs["route"]
    assert route.alias == "client-model"
    assert route.model == "provider/real-model"


def test_create_agent_route_uses_shared_provider_resolver(monkeypatch):
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        "hermes_agent.gateway.runtime_config.resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "default",
            "api_key": "default-key",
            "base_url": "https://default.invalid/v1",
        },
    )
    monkeypatch.setattr(
        "hermes_agent.gateway.runtime_config.resolve_runtime_agent_kwargs_for_provider",
        lambda provider, **kwargs: {
            "provider": provider,
            "api_key": kwargs["explicit_api_key"] or "resolved-key",
            "base_url": kwargs["explicit_base_url"] or "https://resolved.invalid/v1",
        },
    )
    monkeypatch.setattr(
        "hermes_agent.gateway.runtime_config.resolve_gateway_model",
        lambda *_: "default/model",
    )
    monkeypatch.setattr(
        "hermes_agent.gateway.runtime_config.load_gateway_runtime_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "hermes_agent.gateway.runtime_config.load_reasoning_config",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "hermes_agent.gateway.runtime_config.load_fallback_model",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_: set(),
    )
    adapter = _adapter(
        {
            "client-model": {
                "model": "provider/real-model",
                "provider": "routed-provider",
                "api_key": "route-key",
                "base_url": "https://route.invalid/v1",
            }
        }
    )
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

    agent = adapter._create_agent(route=adapter._resolve_route("client-model"))

    assert isinstance(agent, FakeAgent)
    assert captured["model"] == "provider/real-model"
    assert captured["provider"] == "routed-provider"
    assert captured["api_key"] == "route-key"
    assert captured["base_url"] == "https://route.invalid/v1"


def test_session_override_beats_static_route(monkeypatch):
    adapter = _adapter(
        {"client-model": {"model": "route/model", "api_key": "route-key"}}
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False),
        _session_model_overrides={
            "session-1": {
                "model": "session/model",
                "api_key": "session-key",
            }
        },
        _session_key_for_source=lambda _source: "canonical-session-1",
    )

    model, runtime = adapter._apply_session_override(
        "default/model",
        {"api_key": "default-key"},
        adapter._session_model_override_for("session-1"),
    )

    assert model == "session/model"
    assert runtime["api_key"] == "session-key"


def test_model_routes_are_loaded_from_current_profile(monkeypatch, tmp_path):
    default_home = tmp_path / "default"
    ops_home = tmp_path / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    (ops_home / "config.yaml").write_text(
        "platforms:\n"
        "  api_server:\n"
        "    enabled: false\n"
        "    extra:\n"
        "      model_routes:\n"
        "        ops-client:\n"
        "          model: ops/real-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: ops_home if name == "ops" else default_home,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: name in {"default", "ops"},
    )
    adapter = _adapter(
        {"default-client": {"model": "default/real-model"}}
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True),
        _session_model_overrides={},
    )
    secret_scope.set_multiplex_active(True)

    token = _api_request_profile.set("ops")
    try:
        with adapter._profile_scope("ops"):
            assert adapter._resolve_route("ops-client").model == "ops/real-model"
            assert adapter._resolve_route("default-client") is None
            assert adapter._advertised_model_name() == "ops"
    finally:
        _api_request_profile.reset(token)
