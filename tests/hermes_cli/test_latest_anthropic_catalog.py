"""Keep latest Anthropic capabilities aligned across picker surfaces."""


def test_latest_anthropic_models_are_in_canonical_provider_catalogs():
    from hermes_cli.models import OPENROUTER_MODELS, _PROVIDER_MODELS

    openrouter_ids = {model_id for model_id, _ in OPENROUTER_MODELS}
    assert {
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4.8",
        "anthropic/claude-opus-4.8-fast",
        "anthropic/claude-sonnet-5",
    } <= openrouter_ids
    assert {
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
    } <= set(_PROVIDER_MODELS["anthropic"])
    assert "claude-sonnet-5" in _PROVIDER_MODELS["copilot"]
    assert "us.anthropic.claude-sonnet-5" in _PROVIDER_MODELS["bedrock"]


def test_latest_anthropic_output_and_context_limits_are_consistent():
    from agent.anthropic_adapter import _get_anthropic_max_output
    from agent.model_metadata import get_model_context_length

    for model in ("claude-fable-5", "claude-opus-4-8", "claude-sonnet-5"):
        assert get_model_context_length(
            model,
            provider="anthropic",
            allow_network_discovery=False,
        ) == 1_000_000
        assert _get_anthropic_max_output(model) == 128_000


def test_copilot_sonnet_5_alias_is_canonical():
    from hermes_cli.models import normalize_copilot_model_id

    assert normalize_copilot_model_id("anthropic/claude-sonnet-5") == (
        "claude-sonnet-5"
    )


def test_alibaba_coding_plan_catalog_matches_supported_qwen_models():
    from agent.model_metadata import get_model_context_length
    from hermes_cli.models import _PROVIDER_MODELS

    coding_plan = _PROVIDER_MODELS["alibaba-coding-plan"]
    assert "qwen3.7-plus" in coding_plan
    assert "qwen3-max-2026-01-23" in coding_plan
    assert "qwen3.7-max" not in coding_plan
    assert "qwen3.7-plus" in _PROVIDER_MODELS["alibaba"]
    assert get_model_context_length(
        "qwen3.7-plus",
        provider="alibaba-coding-plan",
        allow_network_discovery=False,
    ) == 1_048_576
    assert get_model_context_length(
        "qwen3-max-2026-01-23",
        provider="alibaba-coding-plan",
        allow_network_discovery=False,
    ) == 262_144
