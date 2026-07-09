"""Tests for gateway /footer runtime metadata command."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import gateway.run as gateway_run
import hermes_gateway.footer_command as footer_command
from channels.platforms.base import MessageEvent
from hermes_gateway.config import Platform
from hermes_gateway.session import SessionSource


def _make_event(text="/footer", platform=Platform.TELEGRAM):
    source = SessionSource(
        platform=platform,
        user_id="user-1",
        chat_id="chat-1",
        user_name="tester",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


@pytest.mark.asyncio
async def test_footer_status_reports_effective_platform_config(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "display:\n"
        "  runtime_footer:\n"
        "    enabled: true\n"
        "    fields: [model]\n"
        "  platforms:\n"
        "    telegram:\n"
        "      runtime_footer:\n"
        "        fields: [cwd]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(footer_command, "GATEWAY_HOME", hermes_home)

    result = await footer_command.footer_command_for(_make_runner()).handle_footer_command(_make_event("/footer status"))

    assert "ON" in result
    assert "cwd" in result
    assert "telegram" in result


@pytest.mark.asyncio
async def test_footer_on_persists_global_runtime_footer_flag(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  runtime_footer:\n    enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(footer_command, "GATEWAY_HOME", hermes_home)
    monkeypatch.setattr(footer_command, "resolve_gateway_model", lambda config=None: "openai/gpt-5.4")

    result = await footer_command.footer_command_for(_make_runner()).handle_footer_command(_make_event("/footer on"))

    assert "ON" in result
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["display"]["runtime_footer"]["enabled"] is True


@pytest.mark.asyncio
async def test_footer_toggle_creates_display_config_when_missing(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text("agent:\n  model: openai/gpt-5.4\n", encoding="utf-8")
    monkeypatch.setattr(footer_command, "GATEWAY_HOME", hermes_home)

    result = await footer_command.footer_command_for(_make_runner()).handle_footer_command(_make_event("/footer"))

    assert "ON" in result
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["display"]["runtime_footer"]["enabled"] is True


@pytest.mark.asyncio
async def test_footer_rejects_unknown_argument(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("display: {}\n", encoding="utf-8")
    monkeypatch.setattr(footer_command, "GATEWAY_HOME", hermes_home)

    result = await footer_command.footer_command_for(_make_runner()).handle_footer_command(_make_event("/footer maybe"))

    assert "/footer" in result
