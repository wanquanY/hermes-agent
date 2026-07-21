"""Gateway STT config tests — honor stt.enabled: false from config.yaml."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from hermes_gateway.config import GatewayConfig, Platform, load_gateway_config
from channels.platforms.base import MessageEvent, MessageType
from hermes_gateway.session import SessionSource


def test_gateway_config_stt_disabled_from_dict_nested():
    config = GatewayConfig.from_dict({"stt": {"enabled": False}})
    assert config.stt_enabled is False


def test_gateway_config_stt_echo_from_dict_nested():
    config = GatewayConfig.from_dict({"stt": {"echo_transcripts": False}})
    assert config.stt_echo_transcripts is False


def test_load_gateway_config_bridges_stt_enabled_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"stt": {"enabled": False}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.stt_enabled is False


def test_load_gateway_config_bridges_canonical_gateway_stt(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "gateway": {
                    "stt": {"enabled": False, "echo_transcripts": False},
                    "stt_echo_transcripts": False,
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.stt_enabled is False
    assert config.stt_echo_transcripts is False


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_surfaces_path_when_stt_disabled():
    from hermes_gateway.runner import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)
    runner._has_setup_skill = lambda: True  # Should NOT be consulted in disabled branch.

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("transcribe_audio should not be called when STT is disabled"),
    ), patch(
        "hermes_gateway.inbound_media.probe_audio_duration",
        new=AsyncMock(return_value="0:12"),
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "/tmp/voice.ogg" in result
    assert "voice message" in result.lower()
    assert "(duration: 0:12)" in result
    assert "caption" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_omits_duration_on_probe_failure():
    from hermes_gateway.runner import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)

    with patch(
        "hermes_gateway.inbound_media.probe_audio_duration",
        new=AsyncMock(return_value=None),
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "",
            ["/tmp/voice.ogg"],
        )

    assert "/tmp/voice.ogg" in result
    assert "duration" not in result.lower()
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_avoids_bogus_no_provider_message_for_backend_key_errors():
    from hermes_gateway.runner import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": False, "error": "VOICE_TOOLS_OPENAI_KEY not set"},
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "No STT provider is configured" not in result
    assert "[voice message could not be transcribed]" in result
    assert "caption" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_strips_empty_content_placeholder():
    """A successful transcription whose caption is the empty-content placeholder
    must strip that redundant placeholder while preserving the text contract.

    The Discord adapter delivers a captionless voice note as the literal
    ``"(The user sent a message with no text content)"`` placeholder. When STT
    succeeds, the gateway should return just the transcript prefix as the
    enriched message text.
    """
    from hermes_gateway.runner import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "hello from a captionless voice note",
            "provider": "local_command",
        },
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "(The user sent a message with no text content)",
            ["/tmp/voice.ogg"],
        )

    # The redundant placeholder is stripped, leaving only the transcript prefix.
    assert "hello from a captionless voice note" in result
    assert "(The user sent a message with no text content)" not in result
    assert transcripts == ["hello from a captionless voice note"]


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_transcribes_queued_voice_event():
    from hermes_gateway.runner import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=["/tmp/queued-voice.ogg"],
        media_types=["audio/ogg"],
    )

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "queued voice transcript",
            "provider": "local_command",
        },
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert result == '"queued voice transcript"'
