"""Wire-contract tests for recently added provider/model compatibility."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module", autouse=True)
def _load_provider_plugins():
    import model_tools  # noqa: F401


def _profile(name: str):
    from providers import get_provider_profile

    profile = get_provider_profile(name)
    assert profile is not None
    return profile


def test_custom_gpt56_uses_top_level_reasoning_effort_only():
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model="gpt-5.6-luna",
        messages=[{"role": "user", "content": "ping"}],
        provider_profile=_profile("custom"),
        reasoning_config={"enabled": True, "effort": "medium"},
    )

    assert kwargs["reasoning_effort"] == "medium"
    assert "reasoning" not in kwargs.get("extra_body", {})


def test_custom_gpt56_ultra_clamps_to_supported_max():
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model="gpt-5.6-sol-pro",
        messages=[{"role": "user", "content": "ping"}],
        provider_profile=_profile("custom"),
        reasoning_config={"enabled": True, "effort": "ultra"},
    )

    assert kwargs["reasoning_effort"] == "max"


def test_custom_disabled_reasoning_uses_ollama_compatible_off_switch():
    extra_body, top_level = _profile("custom").build_api_kwargs_extras(
        reasoning_config={"enabled": False},
    )

    assert extra_body == {"think": False}
    assert top_level == {}


@pytest.mark.parametrize("effort", ("minimal", "low", "medium", "high"))
def test_glm52_lower_efforts_map_to_native_high(effort: str):
    extra_body, top_level = _profile("zai").build_api_kwargs_extras(
        model="z-ai/glm-5.2",
        reasoning_config={"enabled": True, "effort": effort},
    )

    assert extra_body == {"thinking": {"type": "enabled"}}
    assert top_level == {"reasoning_effort": "high"}


@pytest.mark.parametrize("effort", ("xhigh", "max", "ultra"))
def test_glm52_strong_efforts_map_to_native_max(effort: str):
    _, top_level = _profile("zai").build_api_kwargs_extras(
        model="glm-5p2",
        reasoning_config={"enabled": True, "effort": effort},
    )

    assert top_level == {"reasoning_effort": "max"}


def test_opencode_go_kimi_never_sends_conflicting_reasoning_controls():
    extra_body, top_level = _profile("opencode-go").build_api_kwargs_extras(
        model="kimi-k2.6",
        reasoning_config={"enabled": True, "effort": "high"},
    )

    assert extra_body == {}
    assert top_level == {"reasoning_effort": "high"}


def test_opencode_go_caps_mimo_output_to_model_limit():
    assert _profile("opencode-go").get_max_tokens("xiaomi/mimo-v2.5-pro") == 131072


def test_ollama_cloud_strong_efforts_map_to_max():
    _, top_level = _profile("ollama-cloud").build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "ultra"},
    )

    assert top_level == {"reasoning_effort": "max"}
