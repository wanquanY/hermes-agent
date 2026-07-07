from __future__ import annotations

from channels.platforms.telegram import MAX_COMMANDS_PER_SCOPE
from channels.platforms.telegram import TelegramAdapter
from channels.platforms.telegram import _strip_mdv2
from channels.platforms.telegram import check_telegram_requirements
from channels.platforms.telegram_network import TelegramFallbackTransport
from channels.platforms.telegram_network import parse_fallback_ip_env


def test_channels_telegram_exports_adapter_contract() -> None:
    assert TelegramAdapter.__name__ == "TelegramAdapter"
    assert TelegramAdapter.MAX_MESSAGE_LENGTH == 4096
    assert MAX_COMMANDS_PER_SCOPE == 30
    assert _strip_mdv2(r"\*hello\*") == "hello"
    assert isinstance(check_telegram_requirements(), bool)


def test_channels_telegram_network_exports_fallback_contract() -> None:
    assert TelegramFallbackTransport.__name__ == "TelegramFallbackTransport"
    assert parse_fallback_ip_env("149.154.167.220, bad") == ["149.154.167.220"]
