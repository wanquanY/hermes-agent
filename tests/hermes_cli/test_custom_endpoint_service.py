"""Custom endpoint ownership, preservation, and secret lifecycle tests."""

from types import SimpleNamespace

from hermes_cli.custom_endpoints import (
    activate_endpoint,
    delete_endpoint,
    endpoint_response,
    write_endpoint,
)
from hermes_cli.web_server import _apply_main_model_assignment


def _body(**overrides):
    values = {
        "id": "edge",
        "name": "Edge",
        "base_url": "https://edge.example/v1/",
        "model": "new-model",
        "api_key": None,
        "context_length": 256_000,
        "discover_models": True,
        "make_default": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_endpoint_edit_preserves_handwritten_fields_and_catalog():
    config = {
        "providers": {
            "edge": {
                "name": "Old",
                "base_url": "https://old.example/v1",
                "model": "old-model",
                "models": {"old-model": {"context_length": 64_000}},
                "api_mode": "responses",
                "key_env": "EDGE_API_KEY",
                "extra_headers": {"X-Tenant": "alpha"},
                "request_overrides": {"store": False},
            }
        }
    }
    endpoint_id, entry = write_endpoint(
        config,
        _body(),
        apply_main_assignment=_apply_main_model_assignment,
    )
    assert endpoint_id == "edge"
    assert entry["api_mode"] == "responses"
    assert entry["key_env"] == "EDGE_API_KEY"
    assert entry["extra_headers"] == {"X-Tenant": "alpha"}
    assert entry["request_overrides"] == {"store": False}
    assert entry["models"]["old-model"]["context_length"] == 64_000
    assert entry["models"]["new-model"]["context_length"] == 256_000


def test_activation_and_delete_scrub_entire_main_endpoint_mirror():
    config = {
        "providers": {
            "edge": {
                "base_url": "https://edge.example/v1",
                "model": "m1",
                "api": "legacy-provider-key",
                "models": {"m1": {}},
            }
        },
        "model": {
            "provider": "old",
            "default": "old-model",
            "api": "stale-main-key",
            "api_mode": "responses",
        },
    }
    provider, model = activate_endpoint(
        config,
        "edge",
        apply_main_assignment=_apply_main_model_assignment,
    )
    assert (provider, model) == ("edge", "m1")
    assert config["model"]["api_key"] == "legacy-provider-key"
    assert "api" not in config["model"]

    delete_endpoint(config, "edge")
    assert "provider" not in config["model"]
    assert "base_url" not in config["model"]
    assert "api_key" not in config["model"]
    assert "api" not in config["model"]
    assert "api_mode" not in config["model"]


def test_endpoint_response_reads_legacy_api_alias_without_exposing_secret():
    response = endpoint_response(
        {
            "providers": {
                "edge": {
                    "base_url": "https://edge.example/v1",
                    "model": "m1",
                    "api": "secret-value",
                }
            }
        }
    )
    endpoint = response["endpoints"][0]
    assert endpoint["has_api_key"] is True
    assert endpoint["api_key_preview"] != "secret-value"


def test_provider_switch_clears_legacy_main_api_alias():
    model = _apply_main_model_assignment(
        {
            "provider": "edge",
            "default": "old",
            "api": "legacy-secret",
            "base_url": "https://edge.example/v1",
        },
        "openrouter",
        "openai/gpt-5.4",
    )
    assert "api" not in model
    assert "base_url" not in model or model["base_url"] == ""
