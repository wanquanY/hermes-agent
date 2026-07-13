"""GPT-5.6 is registered consistently across catalogs and runtime metadata."""

from decimal import Decimal

import pytest


GPT56_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-sol-pro",
    "gpt-5.6-terra",
    "gpt-5.6-terra-pro",
    "gpt-5.6-luna",
    "gpt-5.6-luna-pro",
)


def test_all_gpt56_variants_are_in_native_and_codex_catalogs():
    from hermes_cli.codex_models import DEFAULT_CODEX_MODELS
    from hermes_cli.models import _PROVIDER_MODELS

    assert set(GPT56_MODELS) <= set(DEFAULT_CODEX_MODELS)
    assert set(GPT56_MODELS) <= set(_PROVIDER_MODELS["openai"])


@pytest.mark.parametrize("model", GPT56_MODELS)
def test_gpt56_direct_and_codex_context_windows(model: str, monkeypatch):
    from agent.model_metadata import get_model_context_length

    monkeypatch.setattr(
        "agent.models_dev.lookup_models_dev_context",
        lambda provider, model_name: None,
    )

    assert get_model_context_length(model, provider="openai") == 1_050_000
    assert get_model_context_length(model, provider="openai-codex") == 272_000


@pytest.mark.parametrize(
    ("model", "input_price", "output_price", "cache_read", "cache_write"),
    (
        ("gpt-5.6-sol", "5", "30", "0.5", "6.25"),
        ("gpt-5.6-terra", "2.5", "15", "0.25", "3.125"),
        ("gpt-5.6-luna", "1", "6", "0.1", "1.25"),
    ),
)
def test_gpt56_official_pricing(
    model: str,
    input_price: str,
    output_price: str,
    cache_read: str,
    cache_write: str,
):
    from agent.usage_pricing import get_pricing_entry

    for provider in ("openai", "openai-api"):
        entry = get_pricing_entry(model, provider=provider)
        assert entry is not None
        assert entry.input_cost_per_million == Decimal(input_price)
        assert entry.output_cost_per_million == Decimal(output_price)
        assert entry.cache_read_cost_per_million == Decimal(cache_read)
        assert entry.cache_write_cost_per_million == Decimal(cache_write)


def test_gpt56_pro_variants_share_tier_pricing():
    from agent.usage_pricing import get_pricing_entry

    for tier in ("sol", "terra", "luna"):
        base = get_pricing_entry(f"gpt-5.6-{tier}", provider="openai")
        pro = get_pricing_entry(f"gpt-5.6-{tier}-pro", provider="openai")
        assert pro is base


def test_codex_gpt56_compaction_autoraise_is_route_scoped_and_optional():
    from agent.auxiliary_client import _compression_threshold_for_model

    assert _compression_threshold_for_model(
        "gpt-5.6-luna", "openai-codex"
    ) == 0.85
    assert _compression_threshold_for_model("gpt-5.6-luna", "openai") is None
    assert _compression_threshold_for_model(
        "gpt-5.6-luna",
        "openai-codex",
        allow_codex_gpt55_autoraise=False,
    ) is None
