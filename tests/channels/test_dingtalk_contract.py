from __future__ import annotations

from types import SimpleNamespace

from channels.platforms.dingtalk import DINGTALK_TYPE_MAPPING
from channels.platforms.dingtalk import MAX_MESSAGE_LENGTH
from channels.platforms.dingtalk import DingTalkAdapter
from channels.platforms.dingtalk import _DINGTALK_WEBHOOK_RE
from channels.platforms.dingtalk import check_dingtalk_requirements


def test_channels_dingtalk_exports_adapter_contract() -> None:
    assert DingTalkAdapter.__name__ == "DingTalkAdapter"
    assert DingTalkAdapter.MAX_MESSAGE_LENGTH == MAX_MESSAGE_LENGTH
    assert DINGTALK_TYPE_MAPPING["picture"] == "image"
    assert isinstance(check_dingtalk_requirements(), bool)


def test_channels_dingtalk_extract_text_contract() -> None:
    msg = SimpleNamespace(text={"content": "  hello  "}, rich_text=None)
    assert DingTalkAdapter._extract_text(msg) == "hello"

    rich_msg = SimpleNamespace(
        text="",
        rich_text=[{"text": "part1"}, {"text": "part2"}, {"image": "url"}],
    )
    assert DingTalkAdapter._extract_text(rich_msg) == "part1 part2"


def test_channels_dingtalk_webhook_allowlist_contract() -> None:
    assert _DINGTALK_WEBHOOK_RE.match(
        "https://api.dingtalk.com/robot/send?access_token=x"
    )
    assert _DINGTALK_WEBHOOK_RE.match(
        "https://oapi.dingtalk.com/robot/send?access_token=x"
    )
    assert not _DINGTALK_WEBHOOK_RE.match("http://api.dingtalk.com/robot/send")
    assert not _DINGTALK_WEBHOOK_RE.match("https://api.dingtalk.com.evil.example/")
