from __future__ import annotations

from channels.platforms import YuanbaoAdapter as ExportedYuanbaoAdapter
from channels.platforms.yuanbao import DEFAULT_API_DOMAIN
from channels.platforms.yuanbao import DEFAULT_WS_GATEWAY_URL
from channels.platforms.yuanbao import MarkdownProcessor
from channels.platforms.yuanbao import YuanbaoAdapter
from channels.platforms.yuanbao import get_active_adapter
from channels.platforms.yuanbao_media import guess_mime_type
from channels.platforms.yuanbao_proto import HERMES_INSTANCE_ID


def test_channels_yuanbao_exports_adapter_contract() -> None:
    assert ExportedYuanbaoAdapter is YuanbaoAdapter
    assert YuanbaoAdapter.__name__ == "YuanbaoAdapter"
    assert DEFAULT_WS_GATEWAY_URL.startswith("wss://")
    assert DEFAULT_API_DOMAIN.startswith("https://")
    assert get_active_adapter() is None


def test_channels_yuanbao_markdown_contract() -> None:
    assert MarkdownProcessor.strip_outer_markdown_fence("```md\nhello\n```") == "hello"


def test_channels_yuanbao_helper_contracts() -> None:
    assert str(HERMES_INSTANCE_ID)
    assert guess_mime_type("image.png") == "image/png"
