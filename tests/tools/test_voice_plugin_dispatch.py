"""Contract tests for plugin-owned TTS and transcription dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider


class DemoTTS(TTSProvider):
    def __init__(self):
        self.received = {}

    @property
    def name(self) -> str:
        return "demo_tts"

    @property
    def voice_compatible(self) -> bool:
        return True

    def synthesize(self, text, output_path, **extra):
        self.received = extra
        Path(output_path).write_bytes(text.encode("utf-8"))
        return output_path


class DemoSTT(TranscriptionProvider):
    @property
    def name(self) -> str:
        return "demo_stt"

    def transcribe(self, file_path, **extra):
        return {"success": True, "transcript": Path(file_path).read_text()}


@pytest.fixture(autouse=True)
def _reset_voice_registries(monkeypatch):
    from agent import transcription_registry, tts_registry

    tts_registry._reset_for_tests()
    transcription_registry._reset_for_tests()
    monkeypatch.setattr(
        "hermes_cli.plugins._ensure_plugins_discovered", lambda force=False: None
    )
    yield
    tts_registry._reset_for_tests()
    transcription_registry._reset_for_tests()


def test_tts_plugin_dispatch_honors_precedence_and_writes_audio(tmp_path):
    from agent.tts_registry import register_provider
    from tools.voice_plugin_dispatch import dispatch_tts_plugin

    provider = DemoTTS()
    register_provider(provider)
    output = tmp_path / "voice.mp3"
    result = dispatch_tts_plugin(
        text="hello",
        output_path=str(output),
        provider="demo_tts",
        config={
            "speed": 0.8,
            "demo_tts": {
                "voice_id": "voice-a",
                "model_id": "model-a",
                "speed": 1.2,
                "output_format": "wav",
            },
        },
        builtin_names=frozenset({"edge"}),
        command_provider_shadows=False,
        default_format="mp3",
    )
    assert result == str(output)
    assert output.read_bytes() == b"hello"
    assert provider.received == {
        "voice": "voice-a",
        "model": "model-a",
        "speed": 1.2,
        "format": "wav",
    }

    assert dispatch_tts_plugin(
        text="ignored",
        output_path=str(output),
        provider="demo_tts",
        config={},
        builtin_names=frozenset({"demo_tts"}),
        command_provider_shadows=False,
        default_format="mp3",
    ) is None
    assert dispatch_tts_plugin(
        text="ignored",
        output_path=str(output),
        provider="demo_tts",
        config={},
        builtin_names=frozenset(),
        command_provider_shadows=True,
        default_format="mp3",
    ) is None


def test_stt_plugin_dispatch_returns_standard_envelope(tmp_path):
    from agent.transcription_registry import register_provider
    from tools.voice_plugin_dispatch import dispatch_transcription_plugin

    register_provider(DemoSTT())
    audio = tmp_path / "audio.txt"
    audio.write_text("transcript", encoding="utf-8")
    result = dispatch_transcription_plugin(
        file_path=str(audio),
        provider="demo_stt",
        builtin_names=frozenset({"local"}),
    )
    assert result == {
        "success": True,
        "transcript": "transcript",
        "provider": "demo_stt",
    }
