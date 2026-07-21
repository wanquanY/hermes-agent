"""Provider-owned vision defaults and capability routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_deepinfra_default_vision_model_comes_from_provider_hook():
    from agent.auxiliary_client import _resolve_provider_vision_default

    profile = MagicMock()
    profile.default_vision_model.return_value = "catalog/vision-model"
    with patch("providers.get_provider_profile", return_value=profile):
        assert _resolve_provider_vision_default("deepinfra") == "catalog/vision-model"


def test_static_provider_vision_default_precedes_plugin_lookup():
    from agent.auxiliary_client import _resolve_provider_vision_default

    with patch("providers.get_provider_profile") as lookup:
        assert _resolve_provider_vision_default("xiaomi") == "mimo-v2.5"
    lookup.assert_not_called()


def test_provider_profile_fetch_models_honors_endpoint_override():
    from providers.base import ProviderProfile

    profile = ProviderProfile(name="custom", base_url="https://default.invalid/v1")
    response = MagicMock()
    response.read.return_value = b'{"data":[{"id":"vision-1"}]}'
    response.__enter__.return_value = response
    with patch(
        "hermes_cli.urllib_security.open_credentialed_url",
        return_value=response,
    ) as open_url:
        assert profile.fetch_models(base_url="https://override.example/v1") == [
            "vision-1"
        ]

    request = open_url.call_args.args[0]
    assert request.full_url == "https://override.example/v1/models"
