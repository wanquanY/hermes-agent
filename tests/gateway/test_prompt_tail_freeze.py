from __future__ import annotations

from types import SimpleNamespace

import pytest

import hermes_gateway.agent_turn_context as turn_context_module
import hermes_gateway.session_context as session_context_module
from channels.config import HomeChannel, Platform, PlatformConfig
from channels.session_identity import SessionContext, SessionSource
from hermes_gateway.agent_turn_context import GatewayAgentTurnContextService
from hermes_gateway.config_model import GatewayConfig


def _context(*, message_id: str = "message-1", topic: str = "Design") -> SessionContext:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_name="design-room",
        chat_type="channel",
        user_id="user-1",
        user_name="Ada",
        chat_topic=topic,
        guild_id="guild-1",
        message_id=message_id,
    )
    return SessionContext(
        source=source,
        connected_platforms=[Platform.DISCORD, Platform.TELEGRAM],
        home_channels={
            Platform.DISCORD: HomeChannel(
                platform=Platform.DISCORD,
                chat_id="home-1",
                name="Home",
            )
        },
    )


def test_pinned_prompt_reuses_exact_bytes_until_renderer_input_changes(monkeypatch):
    renders = []

    def unstable_renderer(context, *, redact_pii=False):
        renders.append((context.source.chat_topic, redact_pii))
        return f"render-{len(renders)}"

    monkeypatch.setattr(
        turn_context_module,
        "build_session_context_prompt",
        unstable_renderer,
    )
    runner = SimpleNamespace()
    service = GatewayAgentTurnContextService(runner)

    first = service.pinned_context_prompt(
        context=_context(),
        redact_pii=False,
        session_key="session-key",
    )
    same = service.pinned_context_prompt(
        context=_context(message_id="message-2"),
        redact_pii=False,
        session_key="session-key",
    )
    changed = service.pinned_context_prompt(
        context=_context(message_id="message-3", topic="Engineering"),
        redact_pii=False,
        session_key="session-key",
    )

    assert first == same == "render-1"
    assert changed == "render-2"
    assert renders == [("Design", False), ("Engineering", False)]


def test_discord_prompt_replaces_volatile_message_id_with_static_pointer(monkeypatch):
    monkeypatch.setattr(session_context_module, "_discord_tools_loaded", lambda: True)

    prompt = session_context_module.build_session_context_prompt(_context())

    assert "message-1" not in prompt
    assert "provided per-turn in the incoming user message" in prompt
    assert "[Voice channel now: ...]" in prompt


def test_connected_platforms_are_sorted_for_stable_rendering():
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="telegram"),
            Platform.DISCORD: PlatformConfig(enabled=True, token="discord"),
        }
    )

    assert config.get_connected_platforms() == [
        Platform.DISCORD,
        Platform.TELEGRAM,
    ]


@pytest.mark.asyncio
async def test_first_contact_and_voice_changes_are_user_sidecar_notes(
    monkeypatch,
):
    voice_context = ["Design Room: Ada speaking"]
    adapter = SimpleNamespace(
        get_voice_channel_context=lambda _guild_id: voice_context[0]
    )
    runner = SimpleNamespace(
        adapters={Platform.DISCORD: adapter},
        session_store=SimpleNamespace(has_any_sessions=lambda: False),
    )
    service = GatewayAgentTurnContextService(runner)
    monkeypatch.setenv("TEST_HOME_TARGET", "configured")
    monkeypatch.setattr(turn_context_module, "_discord_tools_loaded", lambda: False)
    source = _context().source
    event = SimpleNamespace(message_id="message-1")
    voice_runtime = SimpleNamespace(get_guild_id=lambda _event: 1)
    notice_runtime = SimpleNamespace(
        deliver_platform_notice=lambda *_args, **_kwargs: None
    )

    first = await service.collect_turn_notes(
        history=[],
        source=source,
        event=event,
        session_key="session-key",
        home_target_env_var=lambda _platform: "TEST_HOME_TARGET",
        platform_notice_for=lambda _runner: notice_runtime,
        voice_runtime_for=lambda _runner: voice_runtime,
    )
    second = await service.collect_turn_notes(
        history=[{"role": "user", "content": "previous"}],
        source=source,
        event=event,
        session_key="session-key",
        home_target_env_var=lambda _platform: "TEST_HOME_TARGET",
        platform_notice_for=lambda _runner: notice_runtime,
        voice_runtime_for=lambda _runner: voice_runtime,
    )
    voice_context[0] = ""
    left = await service.collect_turn_notes(
        history=[{"role": "user", "content": "previous"}],
        source=source,
        event=event,
        session_key="session-key",
        home_target_env_var=lambda _platform: "TEST_HOME_TARGET",
        platform_notice_for=lambda _runner: notice_runtime,
        voice_runtime_for=lambda _runner: voice_runtime,
    )

    assert any("very first message" in note for note in first)
    assert "[Voice channel now: Design Room: Ada speaking]" in first
    assert second == []
    assert left == ["[Voice channel now: not connected to a voice channel]"]
