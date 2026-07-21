"""Lossless failure policy for context-summary generation."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import (
    ContextCompressor,
    _is_summary_access_or_quota_error,
)


class ProviderError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _compressor() -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        return ContextCompressor(
            model="main-model",
            quiet_mode=True,
            protect_first_n=2,
            protect_last_n=2,
            abort_on_summary_failure=False,
        )


def _messages(count: int = 12) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message-{index} " + "x" * 200,
        }
        for index in range(count)
    ]


@pytest.mark.parametrize(
    "message",
    [
        "insufficient_quota",
        "quota exceeded",
        "out of funds",
        "out of credits",
        "out of extra usage",
        "no API key was found for the configured provider",
    ],
)
def test_terminal_access_and_quota_markers_are_classified(message):
    assert _is_summary_access_or_quota_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "rate limit exceeded; retry later",
        "quota exceeded, please retry after the window resets",
        "request timed out",
        "API key documentation was not found",
    ],
)
def test_transient_or_ambiguous_failures_are_not_terminal(message):
    assert _is_summary_access_or_quota_error(Exception(message)) is False


def test_permanent_quota_failure_preserves_original_messages():
    compressor = _compressor()
    messages = _messages()
    error = ProviderError(
        "out of extra usage",
        status_code=400,
    )

    with patch("agent.context_compressor.call_llm", side_effect=error):
        result = compressor.compress(messages, current_tokens=999_999, force=True)

    assert result == messages
    assert compressor._last_summary_auth_failure is True
    assert compressor._last_compress_aborted is True
    assert compressor._last_summary_fallback_used is False


def test_reset_window_quota_keeps_best_effort_retry_policy():
    compressor = _compressor()
    messages = _messages()
    error = ProviderError(
        "quota exceeded, please retry after the window resets",
        status_code=402,
    )

    with patch("agent.context_compressor.call_llm", side_effect=error):
        result = compressor.compress(messages, current_tokens=999_999, force=True)

    assert result != messages
    assert compressor._last_summary_auth_failure is False
    assert compressor._last_compress_aborted is False
    assert compressor._last_summary_fallback_used is True


def test_terminal_network_failure_preserves_original_messages_and_cooldown_flag():
    compressor = _compressor()
    messages = _messages()

    with patch(
        "agent.context_compressor.call_llm",
        side_effect=ConnectionError("peer closed connection"),
    ), patch(
        "agent.context_compressor._is_connection_error",
        return_value=True,
    ):
        result = compressor.compress(messages, current_tokens=999_999, force=True)

    assert result == messages
    assert compressor._last_summary_network_failure is True
    assert compressor._last_compress_aborted is True

    # An automatic retry during cooldown must retain the failure class; it
    # cannot fall through to the destructive static handoff.
    second = compressor.compress(messages, current_tokens=999_999)
    assert second == messages
    assert compressor._last_compress_aborted is True


def test_success_clears_failure_classification():
    compressor = _compressor()
    compressor._last_summary_auth_failure = True
    compressor._last_summary_network_failure = True
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))]
    )

    with patch("agent.context_compressor.call_llm", return_value=response):
        assert compressor._generate_summary(_messages(2)) is not None

    assert compressor._last_summary_auth_failure is False
    assert compressor._last_summary_network_failure is False
