from __future__ import annotations

from channels.platforms.weixin import WeixinAdapter
from channels.platforms.weixin import _is_stale_session_ret
from channels.platforms.weixin import _split_text_for_weixin_delivery
from channels.platforms.weixin import check_weixin_requirements


def test_channels_weixin_exports_adapter_contract() -> None:
    assert WeixinAdapter.__name__ == "WeixinAdapter"
    assert WeixinAdapter.MAX_MESSAGE_LENGTH == 2000
    assert isinstance(check_weixin_requirements(), bool)


def test_channels_weixin_text_split_contract() -> None:
    assert _split_text_for_weixin_delivery("", 2000) == []
    assert _split_text_for_weixin_delivery("hello", 2000) == ["hello"]


def test_channels_weixin_stale_session_contract() -> None:
    assert _is_stale_session_ret(-2, None, "unknown error") is True
    assert _is_stale_session_ret(-2, None, "freq limit") is False
