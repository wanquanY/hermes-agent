from __future__ import annotations

from channels.platforms.api_server import APIServerAdapter
from channels.platforms.api_server import DEFAULT_PORT
from channels.platforms.api_server import MAX_NORMALIZED_TEXT_LENGTH
from channels.platforms.api_server import _coerce_request_bool
from channels.platforms.api_server import _normalize_chat_content
from channels.platforms.api_server import check_api_server_requirements


def test_channels_api_server_exports_adapter_contract() -> None:
    assert APIServerAdapter.__name__ == "APIServerAdapter"
    assert APIServerAdapter.supports_async_delivery is False
    assert DEFAULT_PORT == 8642
    assert isinstance(check_api_server_requirements(), bool)


def test_channels_api_server_bool_normalization_contract() -> None:
    assert _coerce_request_bool("true") is True
    assert _coerce_request_bool("false") is False
    assert _coerce_request_bool("unknown", default=True) is True


def test_channels_api_server_chat_content_normalization_contract() -> None:
    content = [
        {"type": "text", "text": "hello"},
        {"type": "input_text", "text": "world"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ]

    assert _normalize_chat_content(content) == "hello\nworld"
    assert len(_normalize_chat_content("x" * (MAX_NORMALIZED_TEXT_LENGTH + 1))) == (
        MAX_NORMALIZED_TEXT_LENGTH
    )
