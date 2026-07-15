"""Phase 3 contracts for credential-safe Hermes child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import patch

from agent.copilot_acp_client import _build_subprocess_env
from tools.environments.local import (
    _is_hermes_internal_secret,
    hermes_subprocess_env,
)


def _secret_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OPENAI_API_KEY": "provider-secret",
        "GH_TOKEN": "github-secret",
        "SLACK_BOT_TOKEN": "gateway-secret",
        "MODAL_TOKEN_SECRET": "infra-secret",
        "AUXILIARY_VISION_API_KEY": "aux-secret",
        "AUXILIARY_VISION_BASE_URL": "https://private.example/v1",
        "AUXILIARY_VISION_MODEL": "vision-model",
        "GATEWAY_RELAY_CUSTOM_TOKEN": "relay-secret",
        "GATEWAY_RELAY_URL": "https://relay.example",
        "SAFE_SETTING": "visible",
    }


def test_dynamic_secret_predicate_is_narrow_and_case_insensitive():
    assert _is_hermes_internal_secret("auxiliary_review_api_key")
    assert _is_hermes_internal_secret("AUXILIARY_REVIEW_BASE_URL")
    assert _is_hermes_internal_secret("gateway_relay_alpha_token")
    assert not _is_hermes_internal_secret("AUXILIARY_REVIEW_MODEL")
    assert not _is_hermes_internal_secret("GATEWAY_RELAY_URL")


def test_non_model_child_strips_both_credential_tiers_and_dynamic_secrets():
    with patch.dict(os.environ, _secret_env(), clear=True):
        child = hermes_subprocess_env(inherit_credentials=False)

    assert child["SAFE_SETTING"] == "visible"
    assert child["AUXILIARY_VISION_MODEL"] == "vision-model"
    assert child["GATEWAY_RELAY_URL"] == "https://relay.example"
    for name in (
        "OPENAI_API_KEY",
        "GH_TOKEN",
        "SLACK_BOT_TOKEN",
        "MODAL_TOKEN_SECRET",
        "AUXILIARY_VISION_API_KEY",
        "AUXILIARY_VISION_BASE_URL",
        "GATEWAY_RELAY_CUSTOM_TOKEN",
    ):
        assert name not in child


def test_model_child_keeps_provider_key_but_never_control_plane_secrets():
    with patch.dict(os.environ, _secret_env(), clear=True):
        child = hermes_subprocess_env(
            inherit_credentials=True,
            extra_env={
                "OPENAI_API_KEY": "explicit-provider",
                "GH_TOKEN": "overlay-bypass",
                "AUXILIARY_PLUGIN_API_KEY": "overlay-aux-bypass",
            },
        )

    assert child["OPENAI_API_KEY"] == "explicit-provider"
    assert "GH_TOKEN" not in child
    assert "AUXILIARY_PLUGIN_API_KEY" not in child
    assert "GATEWAY_RELAY_CUSTOM_TOKEN" not in child


def test_copilot_uses_the_same_model_child_policy():
    with patch.dict(os.environ, _secret_env(), clear=True):
        child = _build_subprocess_env()
    assert child["OPENAI_API_KEY"] == "provider-secret"
    assert "GH_TOKEN" not in child
    assert "AUXILIARY_VISION_API_KEY" not in child


def test_real_child_process_snapshot_contains_no_hermes_secrets():
    with patch.dict(os.environ, _secret_env(), clear=True):
        child_env = hermes_subprocess_env(inherit_credentials=False)
        raw = subprocess.check_output(
            [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
            env=child_env,
            text=True,
        )
    snapshot = json.loads(raw)
    assert snapshot["SAFE_SETTING"] == "visible"
    assert "OPENAI_API_KEY" not in snapshot
    assert "GH_TOKEN" not in snapshot
    assert "AUXILIARY_VISION_API_KEY" not in snapshot
    assert "GATEWAY_RELAY_CUSTOM_TOKEN" not in snapshot


def test_browser_child_adds_only_browser_authority(monkeypatch):
    import tools.browser_tool as browser_tool

    monkeypatch.setenv("BROWSERBASE_API_KEY", "browser-secret")
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    child = browser_tool._browser_subprocess_env()
    assert child["BROWSERBASE_API_KEY"] == "browser-secret"
    assert "GH_TOKEN" not in child
    assert "OPENAI_API_KEY" not in child


def test_cua_driver_child_gets_no_credential_authority(monkeypatch):
    from tools.computer_use.cua_backend import _cua_subprocess_env

    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("GH_TOKEN", "github-secret")

    child = _cua_subprocess_env()

    assert "OPENAI_API_KEY" not in child
    assert "GH_TOKEN" not in child
