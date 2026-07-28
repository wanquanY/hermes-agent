"""Voice provider schema must mirror command/plugin runtime resolution."""

from unittest.mock import patch

from hermes_cli.voice_schema import (
    is_command_provider_block,
    provider_options,
    schema_with_voice_provider_options,
)


def test_default_config_exposes_voice_runtime_knobs():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["tts"]["gemini"]["persona_prompt_file"] == ""
    assert DEFAULT_CONFIG["tts"]["gemini"]["audio_tags"] is False
    assert DEFAULT_CONFIG["tts"]["xai"]["speed"] == 1.0
    assert DEFAULT_CONFIG["tts"]["xai"]["optimize_streaming_latency"] == 0
    assert DEFAULT_CONFIG["tts"]["minimax"]["model"] == "speech-02-hd"
    assert DEFAULT_CONFIG["tts"]["kittentts"]["voice"] == "Jasper"
    assert DEFAULT_CONFIG["stt"]["echo_transcripts"] is True


def test_command_provider_discriminator_matches_runtime_contract():
    assert is_command_provider_block({"command": "speak {text}"}) is True
    assert is_command_provider_block({"type": " COMMAND ", "command": "speak"}) is True
    assert is_command_provider_block({"type": "http", "command": "speak"}) is False
    assert is_command_provider_block({"voice": "alloy"}) is False


def test_options_merge_canonical_legacy_and_current_without_builtin_collision():
    config = {
        "tts": {
            "provider": "orphan-provider",
            "providers": {
                "my-command": {"command": "speak"},
                "EDGE": {"command": "must-not-shadow"},
                "not-command": {"voice": "alloy"},
            },
            "legacy-command": {"type": "command", "command": "legacy"},
        }
    }
    with patch("hermes_cli.voice_schema._registered_provider_names", return_value=[]):
        options = provider_options("tts", ["edge", "openai"], config)

    assert options == [
        "edge",
        "openai",
        "my-command",
        "legacy-command",
        "orphan-provider",
    ]


def test_schema_overlay_surfaces_plugins_without_mutating_base():
    base = {
        "tts.provider": {"type": "select", "options": ["edge"]},
        "stt.provider": {"type": "select", "options": ["local"]},
    }
    with patch(
        "hermes_cli.voice_schema._registered_provider_names",
        side_effect=lambda kind: [f"plugin-{kind}"],
    ):
        fields = schema_with_voice_provider_options(base, {})

    assert fields["tts.provider"]["options"] == ["edge", "plugin-tts"]
    assert fields["stt.provider"]["options"] == ["local", "plugin-stt"]
    assert base["tts.provider"]["options"] == ["edge"]
