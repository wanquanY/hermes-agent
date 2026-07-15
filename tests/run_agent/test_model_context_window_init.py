from unittest.mock import patch

from run_agent import AIAgent


def test_registry_context_window_prevents_remote_model_probe():
    """An authoritative runtime descriptor must make agent init network-free."""

    with (
        patch(
            "agent.model_metadata._resolve_endpoint_context_length",
            side_effect=AssertionError("remote model discovery must not run"),
        ),
        patch("run_agent.get_tool_definitions", return_value=[]),
    ):
        agent = AIAgent(
            model="deepseek-v4-pro",
            provider="custom",
            base_url="https://api.doviemate.com/api/v1/llm-proxy/v1",
            api_key="test-token",
            api_mode="chat_completions",
            model_context_window=200_000,
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
        )

    assert agent._config_context_length == 200_000
    assert agent.context_compressor.context_length == 200_000
    assert agent._session_init_model_config["context_length"] == 200_000
