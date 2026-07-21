"""Cross-transport tests for reasoning-model stale timeout policy."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    ("model", "expected"),
    (
        ("openai/o3-mini", 300.0),
        ("anthropic/claude-opus-4-8", 240.0),
        ("anthropic/claude-sonnet-5", 180.0),
        ("deepseek/deepseek-v4-flash", 600.0),
        ("x-ai/grok-4.5", 300.0),
        ("llama-4-70b-o1-preview", None),
        ("gpt-4o", None),
    ),
)
def test_reasoning_floor_is_anchored_to_model_slug(model, expected):
    from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor

    assert get_reasoning_stale_timeout_floor(model) == expected


@pytest.mark.parametrize(
    ("model_id", "expected"),
    (
        ("us.anthropic.claude-opus-4-8-v1:0", 240.0),
        ("eu.anthropic.claude-sonnet-4-6-v1:0", 180.0),
        ("apac.anthropic.claude-sonnet-4-5-v1:0", 180.0),
        ("au.deepseek.r1-v1:0", 600.0),
        ("global.amazon.nova-pro-v1:0", None),
    ),
)
def test_bedrock_profile_normalization_reaches_same_policy(model_id, expected):
    from agent.chat_completion_helpers import _bedrock_reasoning_stale_floor

    assert _bedrock_reasoning_stale_floor(model_id) == expected


def test_stream_default_applies_reasoning_floor(monkeypatch):
    from agent.chat_completion_helpers import _derive_stream_stale_timeout

    monkeypatch.delenv("HERMES_STREAM_STALE_TIMEOUT", raising=False)
    agent = SimpleNamespace(
        provider="bedrock",
        model="us.anthropic.claude-opus-4-8-v1:0",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
    )
    with patch(
        "agent.chat_completion_helpers.get_provider_stale_timeout",
        return_value=None,
    ):
        assert _derive_stream_stale_timeout(
            agent,
            {"modelId": agent.model, "messages": []},
        ) == 240.0


def test_explicit_stream_timeout_remains_authoritative(monkeypatch):
    from agent.chat_completion_helpers import _derive_stream_stale_timeout

    monkeypatch.delenv("HERMES_STREAM_STALE_TIMEOUT", raising=False)
    agent = SimpleNamespace(
        provider="bedrock",
        model="us.anthropic.claude-opus-4-8-v1:0",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
    )
    with patch(
        "agent.chat_completion_helpers.get_provider_stale_timeout",
        return_value=30.0,
    ):
        assert _derive_stream_stale_timeout(
            agent,
            {"modelId": agent.model, "messages": []},
        ) == 30.0
