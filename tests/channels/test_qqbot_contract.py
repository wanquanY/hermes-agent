from __future__ import annotations

from channels.platforms.qqbot import QQAdapter
from channels.platforms.qqbot import ChunkedUploader
from channels.platforms.qqbot import check_qq_requirements
from channels.platforms.qqbot import build_connect_url
from channels.platforms.qqbot import parse_approval_button_data
from channels.platforms.qqbot.chunked_upload import format_size


def test_channels_qqbot_exports_adapter_contract() -> None:
    assert QQAdapter.__name__ == "QQAdapter"
    assert QQAdapter.MAX_MESSAGE_LENGTH == 4000
    assert isinstance(check_qq_requirements(), bool)


def test_channels_qqbot_exports_upload_and_keyboard_contracts() -> None:
    assert ChunkedUploader.__name__ == "ChunkedUploader"
    assert format_size(1024) == "1.0 KB"
    assert parse_approval_button_data("approve:agent:main:qqbot:c2c:UID:allow-once") == (
        "agent:main:qqbot:c2c:UID",
        "allow-once",
    )


def test_channels_qqbot_exports_onboard_contract() -> None:
    url = build_connect_url("bind-token")

    assert "bind-token" in url
