"""Global model switches leave endpoint configuration internally coherent."""

from types import SimpleNamespace

import yaml

from hermes_gateway.model_command import _persist_model_switch


def _result(**overrides):
    values = {
        "new_model": "local-model",
        "target_provider": "custom",
        "base_url": "",
        "api_mode": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _seed(path):
    path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "old-model",
                    "provider": "custom",
                    "base_url": "https://stale.example/v1",
                    "api_key": "secret",
                    "api_mode": "anthropic_messages",
                }
            }
        ),
        encoding="utf-8",
    )


def test_custom_switch_clears_stale_endpoint_fields(tmp_path):
    config_path = tmp_path / "config.yaml"
    _seed(config_path)

    _persist_model_switch(config_path, _result())

    model = yaml.safe_load(config_path.read_text(encoding="utf-8"))["model"]
    assert model["default"] == "local-model"
    assert "base_url" not in model
    assert "api_mode" not in model
    assert "api_key" not in model


def test_custom_switch_persists_resolved_endpoint_protocol(tmp_path):
    config_path = tmp_path / "config.yaml"
    _seed(config_path)

    _persist_model_switch(
        config_path,
        _result(
            base_url="https://fresh.example/v1",
            api_mode="anthropic_messages",
        ),
    )

    model = yaml.safe_load(config_path.read_text(encoding="utf-8"))["model"]
    assert model["base_url"] == "https://fresh.example/v1"
    assert model["api_mode"] == "anthropic_messages"


def test_named_provider_clears_inline_endpoint_credentials(tmp_path):
    config_path = tmp_path / "config.yaml"
    _seed(config_path)

    _persist_model_switch(
        config_path,
        _result(
            target_provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
        ),
    )

    model = yaml.safe_load(config_path.read_text(encoding="utf-8"))["model"]
    assert model["provider"] == "anthropic"
    assert "base_url" not in model
    assert "api_mode" not in model
    assert "api_key" not in model
