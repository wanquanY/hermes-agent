"""Voice plugins must surface through discovery, schema, and tools picker."""

from unittest.mock import patch

from plugins.voice.deepinfra import DeepInfraTTSProvider


def test_tts_picker_rows_come_from_typed_registry():
    from agent import tts_registry
    from hermes_cli import tools_config

    tts_registry._reset_for_tests()
    tts_registry.register_provider(DeepInfraTTSProvider())
    try:
        with patch("hermes_cli.plugins._ensure_plugins_discovered"):
            rows = tools_config._plugin_tts_providers()
    finally:
        tts_registry._reset_for_tests()

    assert len(rows) == 1
    assert rows[0]["tts_provider"] == "deepinfra"
    assert rows[0]["tts_plugin_name"] == "deepinfra"
    assert rows[0]["env_vars"][0]["key"] == "DEEPINFRA_API_KEY"


def test_visible_tts_providers_include_plugin_rows(monkeypatch):
    from hermes_cli import tools_config

    monkeypatch.setattr(
        tools_config,
        "_plugin_tts_providers",
        lambda: [{"name": "Plugin Voice", "tts_provider": "plugin-voice"}],
    )
    with patch(
        "hermes_cli.tools_config.get_nous_subscription_features"
    ) as features:
        features.return_value.nous_auth_present = False
        visible = tools_config._visible_providers(
            tools_config.TOOL_CATEGORIES["tts"],
            {},
        )

    assert any(row.get("tts_provider") == "plugin-voice" for row in visible)


def test_bundled_plugin_discovery_registers_voice_capabilities(monkeypatch):
    from agent import transcription_registry, tts_registry
    from hermes_cli import plugins as plugins_module
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    tts_registry._reset_for_tests()
    transcription_registry._reset_for_tests()
    try:
        manager.discover_and_load()
        assert tts_registry.get_provider("deepinfra") is not None
        assert transcription_registry.get_provider("deepinfra") is not None
        assert transcription_registry.get_provider("elevenlabs") is not None
    finally:
        tts_registry._reset_for_tests()
        transcription_registry._reset_for_tests()
