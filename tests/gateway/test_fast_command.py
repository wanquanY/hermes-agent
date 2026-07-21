"""Tests for gateway /fast support and Priority Processing routing."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import hermes_gateway.runner as gateway_run
import hermes_gateway.fast_command as fast_command
import hermes_gateway.gateway_runtime_config as gateway_runtime_config
from hermes_gateway.config import Platform
from channels.platforms.base import MessageEvent
from hermes_gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "persist_user_message": persist_user_message,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner._session_service_tier_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def test_turn_route_injects_priority_processing_without_changing_runtime():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_runtime_config.runtime_config_for(runner).resolve_turn_agent_config("hi", "gpt-5.4", runtime_kwargs)

    assert route["runtime"]["provider"] == "openrouter"
    assert route["runtime"]["api_mode"] == "chat_completions"
    assert route["request_overrides"] == {"service_tier": "priority"}


def test_turn_route_skips_priority_processing_for_unsupported_models():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_runtime_config.runtime_config_for(runner).resolve_turn_agent_config("hi", "gpt-5.3-codex", runtime_kwargs)

    assert route["request_overrides"] == {}


@pytest.mark.asyncio
async def test_handle_fast_command_defaults_to_current_session(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(fast_command, "GATEWAY_HOME", tmp_path)
    monkeypatch.setattr(fast_command, "load_gateway_config", lambda: {})
    monkeypatch.setattr(fast_command, "resolve_gateway_model", lambda config=None: "gpt-5.4")

    response = await fast_command.fast_command_for(runner).handle_fast_command(_make_event("/fast fast"))

    assert "FAST" in response
    assert runner._service_tier == "priority"

    session_key = runner._session_key_for_source(_make_source())
    assert runner._session_service_tier_overrides[session_key] == "priority"
    assert not (tmp_path / "config.yaml").exists()


@pytest.mark.asyncio
async def test_handle_fast_command_persists_only_with_global(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(fast_command, "GATEWAY_HOME", tmp_path)
    monkeypatch.setattr(fast_command, "load_gateway_config", lambda: {})
    monkeypatch.setattr(fast_command, "resolve_gateway_model", lambda config=None: "gpt-5.4")

    response = await fast_command.fast_command_for(runner).handle_fast_command(
        _make_event("/fast fast --global")
    )

    assert "FAST" in response
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["agent"]["service_tier"] == "fast"
    session_key = runner._session_key_for_source(_make_source())
    assert session_key not in runner._session_service_tier_overrides


def test_session_fast_overrides_are_isolated_and_explicit_normal_wins(monkeypatch):
    runner = _make_runner()
    service = fast_command.fast_command_for(runner)
    monkeypatch.setattr(service, "load_service_tier", lambda: "priority")

    service.set_session_service_tier_override("session-a", None)
    service.set_session_service_tier_override("session-b", "priority")

    assert service.resolve_session_service_tier(session_key="session-a") is None
    assert service.resolve_session_service_tier(session_key="session-b") == "priority"
    assert service.resolve_session_service_tier(session_key="session-c") == "priority"


@pytest.mark.asyncio
async def test_run_agent_passes_priority_processing_to_gateway_agent(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
    monkeypatch.setattr(gateway_runtime_config, "_hermes_home", tmp_path)
    monkeypatch.setattr(fast_command, "GATEWAY_HOME", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_runtime_config, "resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_runtime_config,
        "resolve_runtime_agent_kwargs",
        lambda _home: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    _CapturingAgent.last_init = None
    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["service_tier"] == "priority"
    assert _CapturingAgent.last_init["request_overrides"] == {"service_tier": "priority"}
