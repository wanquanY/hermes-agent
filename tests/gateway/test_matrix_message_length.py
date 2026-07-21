"""Matrix outbound message length configuration contracts."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from channels.config import PlatformConfig


def _make_adapter(**extra):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    return MatrixAdapter(
        PlatformConfig(
            enabled=True,
            token="syt_test_token",
            extra={
                "homeserver": "https://matrix.example.org",
                "user_id": "@bot:example.org",
                **extra,
            },
        )
    )


def test_matrix_default_and_override_limits(monkeypatch):
    monkeypatch.delenv("MATRIX_MAX_MESSAGE_LENGTH", raising=False)
    default = _make_adapter()
    assert default.max_message_length == 16_000
    assert default._split_threshold == 15_900

    configured = _make_adapter(max_message_length=12_000)
    assert configured.max_message_length == 12_000
    assert configured._split_threshold == 11_900


def test_matrix_config_beats_env_and_values_are_clamped(monkeypatch):
    monkeypatch.setenv("MATRIX_MAX_MESSAGE_LENGTH", "20000")
    assert _make_adapter().max_message_length == 20_000
    assert _make_adapter(max_message_length=10_000).max_message_length == 10_000
    assert _make_adapter(max_message_length=100).max_message_length == 500
    assert _make_adapter(max_message_length=999_999).max_message_length == 65_535


def test_matrix_invalid_limit_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MATRIX_MAX_MESSAGE_LENGTH", "not-a-number")
    assert _make_adapter().max_message_length == 16_000


def test_matrix_yaml_hook_and_plugin_registration(monkeypatch):
    from plugins.platforms.matrix.adapter import (
        DEFAULT_MAX_MESSAGE_LENGTH,
        _apply_yaml_config,
        register,
    )

    monkeypatch.delenv("MATRIX_MAX_MESSAGE_LENGTH", raising=False)
    assert _apply_yaml_config({}, {"max_message_length": 12_000}) == {
        "max_message_length": 12_000
    }
    assert _make_adapter().max_message_length == 12_000

    ctx = MagicMock()
    register(ctx)
    kwargs = ctx.register_platform.call_args.kwargs
    assert kwargs["max_message_length"] == DEFAULT_MAX_MESSAGE_LENGTH
    assert kwargs["apply_yaml_config_fn"] is _apply_yaml_config


def test_matrix_send_uses_configured_limit():
    adapter = _make_adapter(max_message_length=5_000)
    adapter._client = MagicMock()
    adapter._client.send_message_event = AsyncMock(return_value="evt")

    async def run():
        with patch.object(
            adapter,
            "truncate_message",
            wraps=adapter.truncate_message,
        ) as truncate:
            await adapter.send("!room:example.org", "x" * 12_000)
            assert truncate.call_args.args[1] == 5_000

    asyncio.run(run())
