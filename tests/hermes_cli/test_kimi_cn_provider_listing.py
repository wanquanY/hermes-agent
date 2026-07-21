"""Kimi international/CN provider identity and picker contracts."""

import os
from unittest.mock import patch

from hermes_cli.model_switch import list_authenticated_providers
from hermes_cli.providers import resolve_provider_full


@patch.dict(os.environ, {"KIMI_CN_API_KEY": "sk-cn-fake"}, clear=False)
def test_kimi_cn_appears_when_only_cn_key_set(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
    providers = list_authenticated_providers(current_provider="kimi-coding-cn")
    rows = {row["slug"]: row for row in providers}
    assert rows["kimi-coding-cn"]["is_current"] is True
    assert "kimi-coding" not in rows


@patch.dict(os.environ, {"KIMI_API_KEY": "sk-intl-fake"}, clear=False)
def test_kimi_intl_appears_when_only_intl_key_set(monkeypatch):
    monkeypatch.delenv("KIMI_CN_API_KEY", raising=False)
    providers = list_authenticated_providers(current_provider="kimi-coding")
    rows = {row["slug"]: row for row in providers}
    assert rows["kimi-coding"]["is_current"] is True
    assert "kimi-coding-cn" not in rows


@patch.dict(
    os.environ,
    {"KIMI_API_KEY": "sk-intl-fake", "KIMI_CN_API_KEY": "sk-cn-fake"},
    clear=False,
)
def test_both_kimi_profiles_appear_without_alias_rows():
    providers = list_authenticated_providers(current_provider="kimi-coding")
    rows = {row["slug"]: row for row in providers}
    assert "kimi-coding" in rows
    assert "kimi-coding-cn" in rows
    assert not {"kimi", "moonshot", "kimi-cn", "moonshot-cn"} & rows.keys()


def test_resolve_provider_full_preserves_kimi_cn_identity():
    provider = resolve_provider_full("kimi-coding-cn", None, None)
    assert provider is not None
    assert provider.id == "kimi-coding-cn"
    assert provider.base_url == "https://api.moonshot.cn/v1"
    assert provider.api_key_env_vars == ("KIMI_CN_API_KEY",)
