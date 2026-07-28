"""Anthropic live discovery augments rather than replaces curated aliases."""

from unittest.mock import patch

from hermes_cli import models


def test_curated_aliases_survive_when_live_catalog_omits_them():
    curated = models._PROVIDER_MODELS["anthropic"]

    with patch.object(
        models,
        "_fetch_anthropic_models",
        return_value=["claude-opus-4-8", "claude-sonnet-4-6"],
    ):
        result = models.provider_model_ids("anthropic")

    assert "claude-fable-5" in result
    assert "claude-sonnet-5" in result
    assert result[: len(curated)] == list(curated)


def test_live_only_models_are_appended_and_overlap_is_deduplicated():
    with patch.object(
        models,
        "_fetch_anthropic_models",
        return_value=["claude-opus-4-8", "claude-future-9-99"],
    ):
        result = models.provider_model_ids("anthropic")

    assert result.count("claude-opus-4-8") == 1
    assert result.index("claude-fable-5") < result.index("claude-future-9-99")


def test_live_failure_returns_curated_catalog():
    with patch.object(models, "_fetch_anthropic_models", return_value=None):
        result = models.provider_model_ids("anthropic")

    assert result == list(models._PROVIDER_MODELS["anthropic"])
