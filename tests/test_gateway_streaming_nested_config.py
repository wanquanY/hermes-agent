"""Regression test for #25676 — nested gateway.streaming config must be loaded."""
from pathlib import Path
from unittest.mock import patch, MagicMock
import json

import pytest
import yaml


def _load_with_yaml_dict(yaml_dict: dict):
    """Patch filesystem so load_gateway_config() sees *yaml_dict* as config.yaml."""
    from hermes_gateway.config import load_gateway_config

    fake_home = Path("/tmp/fake_hermes_home_25676")

    def fake_exists(self):
        return str(self).endswith("config.yaml")

    with patch("hermes_gateway.config.get_hermes_home", return_value=fake_home), \
         patch.object(Path, "exists", fake_exists), \
         patch("builtins.open", create=True) as mock_file:
        mock_file.return_value.__enter__ = lambda s: s
        mock_file.return_value.__exit__ = MagicMock(return_value=False)
        with patch("yaml.safe_load", return_value=yaml_dict):
            return load_gateway_config()


class TestStreamingConfigNested:
    def test_top_level_streaming(self):
        cfg = _load_with_yaml_dict({"streaming": {"enabled": True, "transport": "draft"}})
        assert cfg.streaming.enabled is True
        assert cfg.streaming.transport == "draft"

    def test_nested_gateway_streaming(self):
        """Regression for #25676."""
        cfg = _load_with_yaml_dict({"gateway": {"streaming": {"enabled": True, "transport": "draft"}}})
        assert cfg.streaming.enabled is True
        assert cfg.streaming.transport == "draft"

    def test_top_level_takes_precedence(self):
        cfg = _load_with_yaml_dict({
            "streaming": {"enabled": True, "transport": "edit"},
            "gateway": {"streaming": {"enabled": False, "transport": "draft"}},
        })
        assert cfg.streaming.enabled is True
        assert cfg.streaming.transport == "edit"

    def test_mode_alias_enables_streaming(self):
        cfg = _load_with_yaml_dict({"gateway": {"streaming": {"mode": "auto"}}})
        assert cfg.streaming.enabled is True
        assert cfg.streaming.transport == "auto"

    def test_yaml_boolean_mode_off_disables_streaming(self):
        cfg = _load_with_yaml_dict({"gateway": {"streaming": {"mode": False}}})
        assert cfg.streaming.enabled is False
        assert cfg.streaming.transport == "off"

    def test_transport_alone_does_not_enable_streaming(self):
        cfg = _load_with_yaml_dict({"gateway": {"streaming": {"transport": "draft"}}})
        assert cfg.streaming.enabled is False
        assert cfg.streaming.transport == "draft"

    def test_scalar_gateway_block_is_ignored_safely(self):
        cfg = _load_with_yaml_dict({"gateway": "disabled"})
        assert cfg.streaming.enabled is False


class TestNestedGatewayCompatibility:
    def test_canonical_gateway_fields_reach_runtime_model(self):
        cfg = _load_with_yaml_dict(
            {
                "gateway": {
                    "quick_commands": {"ping": "pong"},
                    "session_reset": {"mode": "none"},
                    "group_sessions_per_user": False,
                    "thread_sessions_per_user": True,
                    "reset_triggers": ["/fresh"],
                    "always_log_local": False,
                    "unauthorized_dm_behavior": "ignore",
                }
            }
        )

        assert cfg.quick_commands == {"ping": "pong"}
        assert cfg.default_reset_policy.mode == "none"
        assert cfg.group_sessions_per_user is False
        assert cfg.thread_sessions_per_user is True
        assert cfg.reset_triggers == ["/fresh"]
        assert cfg.always_log_local is False
        assert cfg.unauthorized_dm_behavior == "ignore"

    def test_top_level_key_presence_wins_even_when_empty(self):
        cfg = _load_with_yaml_dict(
            {
                "session_reset": {},
                "stt": {},
                "gateway": {
                    "session_reset": {"mode": "none"},
                    "stt": {"enabled": False},
                },
            }
        )

        assert cfg.default_reset_policy.mode == "both"
        assert cfg.stt_enabled is True
