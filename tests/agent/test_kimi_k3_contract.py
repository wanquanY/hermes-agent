"""Kimi K3 metadata and Moonshot schema contracts."""

from agent.model_metadata import get_model_context_length
from agent.moonshot_schema import (
    is_moonshot_model,
    sanitize_moonshot_tool_parameters,
)
from hermes_cli.models import _PROVIDER_MODELS


def test_kimi_k3_context_and_catalogs():
    assert get_model_context_length(
        "kimi-k3",
        base_url="https://api.kimi.com/coding/v1",
        allow_network_discovery=False,
    ) == 1_048_576
    assert is_moonshot_model("k3") is True
    assert is_moonshot_model("moonshotai/k3-turbo") is True
    for provider in ("kimi-coding", "kimi-coding-cn", "moonshot", "opencode-go"):
        assert "kimi-k3" in _PROVIDER_MODELS[provider]


def test_moonshot_requires_arrays_on_nested_objects():
    schema = sanitize_moonshot_tool_parameters(
        {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                    "required": "invalid",
                }
            },
            "required": ["options", "dangling"],
        }
    )
    assert schema["required"] == ["options"]
    assert schema["properties"]["options"]["required"] == []


def test_moonshot_invalid_schema_fallback_is_complete_object():
    assert sanitize_moonshot_tool_parameters(None) == {
        "type": "object",
        "properties": {},
        "required": [],
    }
