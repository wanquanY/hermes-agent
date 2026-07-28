from __future__ import annotations

from channels.platforms.base import BasePlatformAdapter
from channels.platforms.base import MessageEvent
from channels.platforms.base import SendResult
from channels.platforms.base import utf16_len


def test_channels_platform_base_exports_core_contracts() -> None:
    assert BasePlatformAdapter.__name__ == "BasePlatformAdapter"
    assert MessageEvent(text="/help").get_command() == "help"
    assert SendResult(success=True).success is True


def test_channels_platform_base_preserves_utf16_length_semantics() -> None:
    assert utf16_len("abc") == 3
    assert utf16_len("😀") == 2
