"""Provider capability contract for multimodal tool-result messages."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _agent(provider: str, model: str):
    from run_agent import AIAgent

    agent = MagicMock(spec=AIAgent)
    agent.provider = provider
    agent.model = model
    agent._no_list_tool_content_models = set()
    agent._content_has_image_parts = lambda content: any(
        isinstance(part, dict)
        and part.get("type") in {"image_url", "input_image"}
        for part in content
    )
    agent._provider_supports_vision_tool_messages = lambda: (
        AIAgent._provider_supports_vision_tool_messages(agent)
    )
    agent._tool_result_content_for_active_model = lambda name, result: (
        AIAgent._tool_result_content_for_active_model(agent, name, result)
    )
    return agent


def _multimodal_result():
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "desktop screenshot"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ],
        "text_summary": "desktop screenshot",
    }


def test_xiaomi_profile_loads_with_split_vision_capabilities():
    from providers import get_provider_profile

    profile = get_provider_profile("xiaomi")
    assert profile is not None
    assert profile.supports_vision is True
    assert profile.supports_vision_tool_messages is False
    assert get_provider_profile("mimo") is profile


def test_xiaomi_proactively_downgrades_multimodal_tool_result():
    agent = _agent("xiaomi", "mimo-v2.5")

    with patch.object(agent, "_model_supports_vision", return_value=True):
        content = agent._tool_result_content_for_active_model(
            "computer_use", _multimodal_result()
        )

    assert content == "desktop screenshot"


def test_compatible_vision_provider_keeps_multimodal_tool_result():
    agent = _agent("openrouter", "gpt-4o")

    with patch.object(agent, "_model_supports_vision", return_value=True):
        content = agent._tool_result_content_for_active_model(
            "browser_screenshot", _multimodal_result()
        )

    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)


def test_unknown_provider_preserves_compatibility_default():
    agent = _agent("third-party", "vision-model")
    assert agent._provider_supports_vision_tool_messages() is True
