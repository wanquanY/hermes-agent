"""Hermes model capability projection stays adapter-accurate."""

from hermes_cli import model_capability_inventory as inventory
from providers import get_provider_profile


def test_projection_prunes_controls_when_catalog_declares_no_reasoning(
    monkeypatch,
):
    monkeypatch.setattr(
        inventory,
        "_models_dev_capabilities",
        lambda provider_id, model_id: {"reasoning_enabled": False},
    )
    monkeypatch.setattr(
        inventory,
        "_provider_capabilities",
        lambda provider_id, model_id, **kwargs: {
            "reasoning_efforts": ["none", "high"],
            "default_reasoning_effort": "high",
            "reasoning_format": "reasoning_details",
        },
    )

    assert inventory.model_capability_metadata("relay", "plain-model") == {
        "reasoning_enabled": False,
    }


def test_projection_uses_upstream_provider_for_relay_model(monkeypatch):
    monkeypatch.setattr(
        inventory,
        "_models_dev_capabilities",
        lambda provider_id, model_id: {},
    )

    capabilities = inventory.model_capability_metadata(
        "nous",
        "anthropic/claude-sonnet-4.6",
    )

    assert capabilities["reasoning_enabled"] is True
    assert capabilities["reasoning_efforts"]


def test_openrouter_mandatory_reasoning_has_no_off_choice():
    profile = get_provider_profile("openrouter")

    capabilities = profile.model_capabilities("anthropic/claude-sonnet-4.6")

    assert capabilities["reasoning_enabled"] is True
    assert "none" not in capabilities["reasoning_efforts"]
    assert capabilities["default_reasoning_effort"] == "high"


def test_minimax_capabilities_follow_selected_api_surface():
    profile = get_provider_profile("minimax")

    anthropic = profile.model_capabilities(
        "MiniMax-M2.7",
        api_mode="anthropic_messages",
    )
    openai = profile.model_capabilities(
        "MiniMax-M3",
        base_url="https://api.minimax.io/v1",
        api_mode="chat_completions",
    )

    assert anthropic["reasoning_efforts"] == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert anthropic["reasoning_format"] == "thinking_blocks"
    assert openai["reasoning_efforts"] == ["none", "enabled"]
    assert openai["reasoning_format"] == "reasoning_content"


def test_opencode_go_capabilities_match_family_adapter():
    profile = get_provider_profile("opencode-go")

    kimi = profile.model_capabilities("kimi-k2.6")
    unsupported = profile.model_capabilities("mimo-v2.5")

    assert kimi["reasoning_enabled"] is True
    assert kimi["reasoning_efforts"] == [
        "none",
        "enabled",
        "low",
        "medium",
        "high",
    ]
    assert unsupported == {}


def test_xai_only_exposes_effort_when_transport_accepts_it():
    profile = get_provider_profile("xai")

    adjustable = profile.model_capabilities("grok-4.5")
    model_default_only = profile.model_capabilities("grok-4")

    assert adjustable["reasoning_efforts"] == ["none", "low", "medium", "high"]
    assert model_default_only == {}
