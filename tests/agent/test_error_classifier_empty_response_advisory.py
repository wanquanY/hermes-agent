"""Regression tests for empty-response advisory classification."""

import pytest

from agent.error_classifier import FailoverReason, classify_api_error


class _ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"message": message}}


@pytest.mark.parametrize("status_code", [None, 400, 500, 502, 503, 529])
def test_empty_response_advisory_never_requests_compression(status_code):
    error = _ProviderError(
        "Provider returned an empty response; this may happen with very low max_tokens",
        status_code,
    )

    classified = classify_api_error(error, approx_tokens=190_000)

    assert classified.reason == FailoverReason.server_error
    assert classified.retryable is True
    assert classified.should_compress is False
