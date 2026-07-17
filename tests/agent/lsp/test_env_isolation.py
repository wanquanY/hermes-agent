"""Credential-boundary tests for LSP runtimes and installers."""

from agent.lsp.client import _lsp_subprocess_env
from agent.lsp.install import _lsp_installer_env


def _set_parent_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("AUXILIARY_REVIEW_API_KEY", "auxiliary-secret")


def test_lsp_server_keeps_explicit_config_without_parent_credentials(monkeypatch):
    _set_parent_secrets(monkeypatch)

    env = _lsp_subprocess_env({"LSP_FEATURE_FLAG": "enabled"})

    assert env["LSP_FEATURE_FLAG"] == "enabled"
    assert "OPENAI_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "AUXILIARY_REVIEW_API_KEY" not in env


def test_lsp_installer_keeps_build_target_without_parent_credentials(monkeypatch):
    _set_parent_secrets(monkeypatch)

    env = _lsp_installer_env({"GOBIN": "/tmp/hermes-lsp-bin"})

    assert env["GOBIN"] == "/tmp/hermes-lsp-bin"
    assert "OPENAI_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "AUXILIARY_REVIEW_API_KEY" not in env
