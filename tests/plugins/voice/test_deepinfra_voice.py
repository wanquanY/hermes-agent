"""DeepInfra voice plugin contracts."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugins.voice.deepinfra import (
    DeepInfraTTSProvider,
    DeepInfraTranscriptionProvider,
    register,
)


class _FakeSpeechResponse:
    def stream_to_file(self, path):
        with open(path, "wb") as handle:
            handle.write(b"audio")


class _FakeOpenAIClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.speech_create = MagicMock(return_value=_FakeSpeechResponse())
        self.transcription_create = MagicMock(return_value=SimpleNamespace(text="hello"))
        self.audio = SimpleNamespace(
            speech=SimpleNamespace(create=self.speech_create),
            transcriptions=SimpleNamespace(create=self.transcription_create),
        )
        self.instances.append(self)

    def close(self):
        self.closed = True


def test_tts_uses_profile_credentials_config_and_openai_compatible_api(tmp_path):
    provider = DeepInfraTTSProvider()
    output = tmp_path / "speech.mp3"
    fake_openai = SimpleNamespace(OpenAI=_FakeOpenAIClient)

    with (
        patch("plugins.voice.deepinfra._api_key", return_value="profile-key"),
        patch(
            "plugins.voice.deepinfra._provider_config",
            return_value={"model": "vendor/tts", "voice": "voice-a"},
        ),
        patch.dict("sys.modules", {"openai": fake_openai}),
    ):
        result = provider.synthesize("hello", str(output), speed=1.1)

    client = _FakeOpenAIClient.instances[-1]
    assert result == str(output)
    assert output.read_bytes() == b"audio"
    assert client.kwargs["api_key"] == "profile-key"
    assert client.speech_create.call_args.kwargs == {
        "model": "vendor/tts",
        "voice": "voice-a",
        "input": "hello",
        "response_format": "mp3",
        "speed": 1.1,
    }
    assert client.closed is True


def test_stt_returns_standard_envelope_and_closes_client(tmp_path):
    provider = DeepInfraTranscriptionProvider()
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"wav")
    fake_openai = SimpleNamespace(OpenAI=_FakeOpenAIClient)

    with (
        patch("plugins.voice.deepinfra._api_key", return_value="profile-key"),
        patch(
            "plugins.voice.deepinfra._provider_config",
            return_value={"model": "vendor/stt"},
        ),
        patch.dict("sys.modules", {"openai": fake_openai}),
    ):
        result = provider.transcribe(str(audio), language="en")

    client = _FakeOpenAIClient.instances[-1]
    assert result == {"success": True, "transcript": "hello", "provider": "deepinfra"}
    assert client.transcription_create.call_args.kwargs["model"] == "vendor/stt"
    assert client.transcription_create.call_args.kwargs["language"] == "en"
    assert client.closed is True


def test_register_exposes_both_typed_capabilities():
    ctx = SimpleNamespace(
        register_tts_provider=MagicMock(),
        register_transcription_provider=MagicMock(),
    )
    register(ctx)
    assert isinstance(ctx.register_tts_provider.call_args.args[0], DeepInfraTTSProvider)
    assert isinstance(
        ctx.register_transcription_provider.call_args.args[0],
        DeepInfraTranscriptionProvider,
    )
