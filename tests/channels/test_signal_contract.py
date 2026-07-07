from __future__ import annotations

from channels.platforms.signal import SignalAdapter
from channels.platforms.signal import MAX_MESSAGE_LENGTH
from channels.platforms.signal import check_signal_requirements
from channels.platforms.signal_rate_limit import SIGNAL_RPC_ERROR_RATELIMIT
from channels.platforms.signal_rate_limit import _is_signal_rate_limit_error


def test_channels_signal_exports_adapter_contract() -> None:
    assert SignalAdapter.__name__ == "SignalAdapter"
    assert MAX_MESSAGE_LENGTH == 8000
    assert SignalAdapter.SUPPORTS_MESSAGE_EDITING is False
    assert isinstance(check_signal_requirements(), bool)


def test_channels_signal_formatting_contract() -> None:
    assert SignalAdapter._markdown_to_signal("**hello**") == ("hello", ["0:5:BOLD"])


def test_channels_signal_rate_limit_contract() -> None:
    assert _is_signal_rate_limit_error({"code": SIGNAL_RPC_ERROR_RATELIMIT}) is True
    assert _is_signal_rate_limit_error({"code": 123, "message": "other"}) is False
