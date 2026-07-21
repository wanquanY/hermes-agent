"""Discord fail-closed defaults remain actionable for operators."""

from __future__ import annotations

import logging

from hermes_gateway.config import PlatformConfig
from channels.platforms.discord import DiscordAdapter


def _adapter() -> DiscordAdapter:
    return DiscordAdapter(PlatformConfig(enabled=True, token="***"))


def test_fail_closed_default_logs_once(monkeypatch, caplog):
    adapter = _adapter()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    for variable in (
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(variable, raising=False)

    with caplog.at_level(logging.WARNING):
        adapter._warn_if_fail_closed_default()
        adapter._warn_if_fail_closed_default()

    messages = [record.message for record in caplog.records]
    warnings = [
        message
        for message in messages
        if "Discord messages are being denied" in message
    ]
    assert len(warnings) == 1
    assert "DISCORD_ALLOWED_USERS" in warnings[0]
    assert "DISCORD_ALLOW_ALL_USERS=true" in warnings[0]


def test_fail_closed_warning_skips_explicit_channel_policy(monkeypatch, caplog):
    adapter = _adapter()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "12345")
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    with caplog.at_level(logging.WARNING):
        adapter._warn_if_fail_closed_default()

    assert "no allowlist is configured" not in caplog.text


def test_setup_existing_token_describes_fail_closed_default(monkeypatch):
    from hermes_cli import setup

    messages: list[str] = []
    answers = iter([False, False])
    monkeypatch.setattr(
        setup,
        "get_env_value",
        lambda key: "token" if key == "DISCORD_BOT_TOKEN" else "",
    )
    monkeypatch.setattr(setup, "print_header", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        setup,
        "print_info",
        lambda message="", **_kwargs: messages.append(str(message)),
    )
    monkeypatch.setattr(
        setup,
        "prompt_yes_no",
        lambda *_args, **_kwargs: next(answers),
    )

    setup._setup_discord()

    rendered = "\n".join(messages)
    assert "anyone can use your bot" not in rendered
    assert "fail-closed default" in rendered
    assert "DISCORD_ALLOW_ALL_USERS=true" in rendered
