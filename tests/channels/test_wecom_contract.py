from __future__ import annotations

from channels.platforms.wecom import WeComAdapter
from channels.platforms.wecom import _coerce_list
from channels.platforms.wecom import check_wecom_requirements
from channels.platforms.wecom_callback import WecomCallbackAdapter
from channels.platforms.wecom_callback import check_wecom_callback_requirements
from channels.platforms.wecom_crypto import SignatureError
from channels.platforms.wecom_crypto import WeComCryptoError


def test_channels_wecom_exports_adapter_contract() -> None:
    assert WeComAdapter.__name__ == "WeComAdapter"
    assert WeComAdapter.MAX_MESSAGE_LENGTH == 4000
    assert WeComAdapter.SUPPORTS_MESSAGE_EDITING is False
    assert isinstance(check_wecom_requirements(), bool)


def test_channels_wecom_helper_contract() -> None:
    assert _coerce_list("a, b,,") == ["a", "b"]


def test_channels_wecom_callback_exports_contract() -> None:
    assert WecomCallbackAdapter.__name__ == "WecomCallbackAdapter"
    assert isinstance(check_wecom_callback_requirements(), bool)
    assert issubclass(SignatureError, WeComCryptoError)
