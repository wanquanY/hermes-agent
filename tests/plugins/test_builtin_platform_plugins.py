from __future__ import annotations

from types import SimpleNamespace

from channels.builtin_platform_plugins import (
    builtin_platform_plugin_specs,
    register_builtin_platform,
    register_builtin_platforms,
)


EXPECTED = {
    "dingtalk",
    "discord",
    "email",
    "feishu",
    "homeassistant",
    "matrix",
    "mattermost",
    "slack",
    "sms",
    "telegram",
    "wecom",
    "wecom_callback",
    "whatsapp",
}


class RecordingContext:
    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}

    def register_platform(self, **kwargs) -> None:
        self.entries[kwargs["name"]] = kwargs


def test_every_upstream_builtin_platform_has_one_canonical_spec() -> None:
    specs = builtin_platform_plugin_specs()
    assert {spec.name for spec in specs} == EXPECTED
    assert len(specs) == len(EXPECTED)


def test_each_spec_registers_through_the_plugin_contract() -> None:
    context = RecordingContext()
    for name in sorted(EXPECTED):
        register_builtin_platform(context, name)

    assert set(context.entries) == EXPECTED
    for name, entry in context.entries.items():
        assert entry["name"] == name
        assert entry["label"]
        assert callable(entry["adapter_factory"])
        assert callable(entry["check_fn"])
        assert callable(entry["is_connected"])
        assert entry["allow_update_command"] is True


def test_related_platforms_can_share_one_manifest_registration() -> None:
    context = RecordingContext()
    register_builtin_platforms(context, ("wecom", "wecom_callback"))
    assert set(context.entries) == {"wecom", "wecom_callback"}


def test_is_connected_is_pure_and_returns_bool(monkeypatch) -> None:
    context = RecordingContext()
    register_builtin_platform(context, "telegram")
    check = context.entries["telegram"]["is_connected"]
    config = SimpleNamespace(token="", api_key="", extra={})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert check(config) is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    assert check(config) is True
