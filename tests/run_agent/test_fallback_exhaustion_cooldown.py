"""Fallback-chain exhaustion is throttled across turns with monotonic time."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.chat_completion_helpers import _FALLBACK_EXHAUSTED_COOLDOWN_S
from agent.error_classifier import FailoverReason
from run_agent import AIAgent


def _make_agent(fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://primary.example/v1",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _client():
    client = MagicMock()
    client.base_url = "https://fallback.example/v1"
    client.api_key = "fallback-key"
    return client


def test_non_retryable_exhaustion_arms_short_cooldown():
    agent = _make_agent([
        {"provider": "openai", "model": "gpt-4o"},
        {"provider": "zai", "model": "glm-4.7"},
    ])
    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_client(), "resolved"),
        ),
        patch("agent.chat_completion_helpers.time.monotonic", return_value=100.0),
    ):
        assert agent._try_activate_fallback()
        assert agent._try_activate_fallback()
        assert not agent._try_activate_fallback()
    assert agent._rate_limited_until == 100.0 + _FALLBACK_EXHAUSTED_COOLDOWN_S


def test_empty_chain_does_not_arm_cooldown():
    agent = _make_agent()
    agent._rate_limited_until = 0
    assert not agent._try_activate_fallback()
    assert agent._rate_limited_until == 0


def test_rate_limit_exhaustion_keeps_longer_window():
    agent = _make_agent([{"provider": "openai", "model": "gpt-4o"}])
    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_client(), "resolved"),
        ),
        patch("agent.chat_completion_helpers.time.monotonic", return_value=100.0),
    ):
        assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        assert not agent._try_activate_fallback(reason=FailoverReason.rate_limit)
    assert agent._rate_limited_until == 160.0


def test_recursive_skips_preserve_rate_limit_reason():
    agent = _make_agent([
        {"provider": "", "model": "invalid"},
        {"provider": "openai", "model": "gpt-4o"},
    ])
    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_client(), "resolved"),
        ),
        patch("agent.chat_completion_helpers.time.monotonic", return_value=200.0),
    ):
        assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        assert not agent._try_activate_fallback(reason=FailoverReason.rate_limit)
    assert agent._rate_limited_until == 260.0


def test_restore_gate_ignores_wall_clock_jumps():
    agent = _make_agent([{"provider": "openai", "model": "gpt-4o"}])
    agent._fallback_activated = True
    agent._fallback_index = 1
    agent._rate_limited_until = 105.0

    with (
        patch("agent.agent_runtime_helpers.time.monotonic", return_value=100.0),
        patch("agent.agent_runtime_helpers.time.time", return_value=10**12),
    ):
        assert not agent._restore_primary_runtime()
        assert agent._fallback_activated
        assert agent._fallback_index == 1

    with (
        patch("agent.agent_runtime_helpers.time.monotonic", return_value=106.0),
        patch("agent.agent_runtime_helpers.time.time", return_value=-10**12),
        patch.object(agent, "_create_openai_client", return_value=MagicMock()),
    ):
        assert agent._restore_primary_runtime()
    assert not agent._fallback_activated
    assert agent._fallback_index == 0
