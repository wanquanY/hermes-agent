from __future__ import annotations

import pytest

from dovie_extension import product_plugins
from plugins.lark_cli.runtime import LarkCliProbe


@pytest.fixture
def lark_product_policy(monkeypatch):
    monkeypatch.setenv("DOVIE_MANAGED_HERMES_GATEWAY", "1")
    monkeypatch.setenv("DOVIE_MANAGED_HERMES_PLUGIN_ALLOWLIST", "lark-cli")


def test_product_plugin_status_is_typed_and_profile_scoped(
    monkeypatch,
    lark_product_policy,
):
    from hermes_cli import config as config_module
    from hermes_cli import plugins_cmd

    monkeypatch.setattr(plugins_cmd, "_plugin_exists", lambda name: name == "lark-cli")
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"lark-cli"})
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())
    monkeypatch.setattr(plugins_cmd, "_get_plugin_toolset_key", lambda name: "lark_cli")
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"platform_toolsets": {"cli": ["file", "lark_cli"]}},
    )
    monkeypatch.setattr(
        "plugins.lark_cli.runtime.LarkCliRuntime.probe",
        lambda self: LarkCliProbe(
            available=True,
            binary="/runtime/bin/lark-cli",
            version="1.0.77",
            compatible=True,
        ),
    )
    verify_calls = []
    monkeypatch.setattr(
        "plugins.lark_cli.service.LarkCliService.status",
        lambda self, verify=False: verify_calls.append(verify) or {
            "ok": True,
            "binding": {"ok": True},
            "auth": {
                "ok": True,
                "result": {"identity": "bot", "verified": True},
            },
        },
    )

    status = product_plugins.product_plugin_status("lark-cli")

    assert status["ok"] is True
    assert status["enabled"] is True
    assert status["toolset"] == "lark_cli"
    assert status["platforms"] == ["cli"]
    assert status["cli"]["compatible"] is True
    assert status["connection"] == {
        "ready": True,
        "binding_state": "bound",
        "identity": "bot",
        "verified": True,
        "next_action": "",
    }
    assert verify_calls == [False]

    product_plugins.product_plugin_status(
        "lark-cli",
        verify_connection=True,
    )
    assert verify_calls == [False, True]


def test_product_plugin_status_rejects_non_product_plugin(
    lark_product_policy,
):
    with pytest.raises(ValueError, match="unsupported Dovie product Plugin"):
        product_plugins.product_plugin_status("spotify")


def test_product_plugin_set_enabled_uses_guarded_hermes_lifecycle(
    monkeypatch,
    lark_product_policy,
):
    from hermes_cli import plugins_cmd

    calls = []
    monkeypatch.setattr(
        plugins_cmd,
        "dashboard_set_agent_plugin_enabled",
        lambda name, enabled: calls.append((name, enabled))
        or {"ok": True, "unchanged": False},
    )
    monkeypatch.setattr(
        product_plugins,
        "product_plugin_status",
        lambda name: {
            "ok": True,
            "plugin_id": name,
            "enabled": True,
            "runtime_state": "enabled",
        },
    )

    response = product_plugins.set_product_plugin_enabled(
        "lark-cli",
        enabled=True,
    )

    assert calls == [("lark-cli", True)]
    assert response["changed"] is True


def test_plugin_enable_repairs_missing_toolset_even_when_already_enabled(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import config as config_module
    from hermes_cli import plugins_cmd

    config = {
        "plugins": {"enabled": ["lark-cli"], "disabled": []},
        "platform_toolsets": {"cli": ["file"]},
    }
    saved = []
    monkeypatch.setattr(plugins_cmd, "_plugin_exists", lambda name: True)
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: {"lark-cli"})
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())
    monkeypatch.setattr(plugins_cmd, "_get_plugin_toolset_key", lambda name: "lark_cli")
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(config_module, "save_config", lambda value: saved.append(value))

    response = plugins_cmd.dashboard_set_agent_plugin_enabled(
        "lark-cli",
        enabled=True,
    )

    assert response["unchanged"] is False
    assert config["platform_toolsets"]["cli"] == ["file", "lark_cli"]
    assert saved == [config]
