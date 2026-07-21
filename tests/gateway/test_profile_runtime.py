"""Isolation and lifecycle tests for the profile runtime service."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import secret_scope
from agent.secret_scope import (
    build_profile_subprocess_env,
    get_profile_env,
    get_secret,
)
from channels.config import Platform, PlatformConfig, platform_binds_port
from channels.session_identity import SessionSource
from hermes_gateway.pairing import PairingStore
from hermes_gateway.profile_routing import ProfileRoute
from hermes_gateway.profile_runtime import (
    GatewayProfileRuntimeService,
    SecondaryPortBindingConfigError,
    profile_runtime_scope,
)


@pytest.fixture(autouse=True)
def _reset_multiplex_state():
    secret_scope.set_multiplex_active(False)
    yield
    secret_scope.set_multiplex_active(False)


def test_profile_scope_is_context_local_and_restores_home(tmp_path, monkeypatch):
    original_home = tmp_path / "default"
    profile_home = tmp_path / "profiles" / "ops"
    original_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("TOKEN=profile-secret\n")
    monkeypatch.setenv("HERMES_HOME", str(original_home))
    monkeypatch.setenv("TOKEN", "process-secret")

    with profile_runtime_scope(profile_home):
        from hermes_constants import get_hermes_home

        assert get_hermes_home() == profile_home
        assert get_secret("TOKEN") == "profile-secret"

    from hermes_constants import get_hermes_home

    assert get_hermes_home() == original_home


def test_profile_subprocess_env_does_not_leak_process_credentials(
    tmp_path,
    monkeypatch,
):
    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "OPENAI_API_KEY=profile-key\nWHATSAPP_MODE=bot\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    secret_scope.set_multiplex_active(True)

    with profile_runtime_scope(profile_home):
        env = build_profile_subprocess_env({"WHATSAPP_REPLY_PREFIX": "[ops]"})

    assert env["OPENAI_API_KEY"] == "profile-key"
    assert env["WHATSAPP_MODE"] == "bot"
    assert env["WHATSAPP_REPLY_PREFIX"] == "[ops]"
    assert env["HERMES_HOME"] == str(profile_home)
    assert env["PATH"] == "/usr/bin"
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_channel_yaml_and_env_remain_profile_local(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / ".env").write_text(
        "SLACK_STRICT_MENTION=true\nOPENAI_API_KEY=first-key\n",
        encoding="utf-8",
    )
    (second / ".env").write_text(
        "SLACK_STRICT_MENTION=false\nOPENAI_API_KEY=second-key\n",
        encoding="utf-8",
    )
    (first / "config.yaml").write_text(
        "telegram:\n  allowed_chats: [first-chat]\n",
        encoding="utf-8",
    )
    (second / "config.yaml").write_text(
        "telegram:\n  allowed_chats: [second-chat]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    secret_scope.set_multiplex_active(True)

    from hermes_gateway.config import load_gateway_config

    with profile_runtime_scope(first):
        first_config = load_gateway_config()
        assert get_profile_env("OPENAI_API_KEY") == "first-key"
        assert get_profile_env("SLACK_STRICT_MENTION") == "true"
    with profile_runtime_scope(second):
        second_config = load_gateway_config()
        assert get_profile_env("OPENAI_API_KEY") == "second-key"
        assert get_profile_env("SLACK_STRICT_MENTION") == "false"

    assert first_config.platforms[Platform.TELEGRAM].extra["allowed_chats"] == [
        "first-chat"
    ]
    assert second_config.platforms[Platform.TELEGRAM].extra["allowed_chats"] == [
        "second-chat"
    ]
    assert "TELEGRAM_ALLOWED_CHATS" not in __import__("os").environ


def test_runtime_agent_settings_follow_profile_scope(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "config.yaml").write_text(
        "agent:\n  system_prompt: first-prompt\n"
        "provider_routing:\n  only: [first-provider]\n",
        encoding="utf-8",
    )
    (second / "config.yaml").write_text(
        "agent:\n  system_prompt: second-prompt\n"
        "provider_routing:\n  only: [second-provider]\n",
        encoding="utf-8",
    )

    from hermes_gateway.gateway_runtime_config import GatewayRuntimeConfigService

    service = GatewayRuntimeConfigService(SimpleNamespace())
    with profile_runtime_scope(first):
        assert service.load_ephemeral_system_prompt() == "first-prompt"
        assert service.load_provider_routing() == {"only": ["first-provider"]}
    with profile_runtime_scope(second):
        assert service.load_ephemeral_system_prompt() == "second-prompt"
        assert service.load_provider_routing() == {"only": ["second-provider"]}


def test_route_source_stamps_profile_before_session_or_auth(monkeypatch, tmp_path):
    route = ProfileRoute(
        name="ops",
        platform="discord",
        profile="ops",
        scope_id="guild-1",
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True, profile_routes=[route])
    )
    runtime = GatewayProfileRuntimeService(runner)
    monkeypatch.setattr(
        runtime,
        "profile_home",
        lambda profile, require_exists=True: tmp_path / profile,
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        scope_id="guild-1",
    )

    routed = runtime.route_source(source)

    assert source.profile is None
    assert routed.profile == "ops"


def test_pairing_stores_are_physically_isolated(tmp_path):
    first = PairingStore(base_dir=tmp_path / "first")
    second = PairingStore(base_dir=tmp_path / "second")
    first._approve_user("discord", "user-1")

    assert first.is_approved("discord", "user-1")
    assert not second.is_approved("discord", "user-1")


@pytest.mark.parametrize(
    ("platform", "extra", "expected"),
    [
        ("api_server", {}, True),
        ("feishu", {}, False),
        ("feishu", {"connection_mode": "webhook"}, True),
        ("discord", {}, False),
    ],
)
def test_port_binding_ownership(platform, extra, expected):
    assert platform_binds_port(platform, extra) is expected


@pytest.mark.asyncio
async def test_secondary_profile_rejects_process_listener(monkeypatch, tmp_path):
    runner = SimpleNamespace()
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._profile_adapters = {}
    runtime = GatewayProfileRuntimeService(runner)
    config = SimpleNamespace(
        platforms={
            Platform.API_SERVER: PlatformConfig(enabled=True),
        }
    )
    monkeypatch.setattr(
        "hermes_gateway.config.load_gateway_config",
        lambda: config,
    )

    with pytest.raises(SecondaryPortBindingConfigError):
        await runtime._start_profile_adapters(
            "ops",
            Path(tmp_path),
            {},
        )
