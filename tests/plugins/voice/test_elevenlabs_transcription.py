"""ElevenLabs Scribe voice plugin contracts."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugins.voice.elevenlabs import ElevenLabsTranscriptionProvider


def test_scribe_request_and_response_contract(tmp_path):
    provider = ElevenLabsTranscriptionProvider()
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"opus")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"text": "transcribed"},
        text="",
    )

    with (
        patch(
            "plugins.voice.elevenlabs.get_profile_env",
            side_effect=lambda name, default="": (
                "secret" if name == "ELEVENLABS_API_KEY" else default
            ),
        ),
        patch(
            "plugins.voice.elevenlabs._config",
            return_value={
                "model_id": "scribe_v2",
                "language_code": "eng",
                "tag_audio_events": True,
                "diarize": True,
            },
        ),
        patch("requests.post", return_value=response) as post,
    ):
        result = provider.transcribe(str(audio))

    assert result == {
        "success": True,
        "transcript": "transcribed",
        "provider": "elevenlabs",
    }
    assert post.call_args.kwargs["headers"] == {"xi-api-key": "secret"}
    assert post.call_args.kwargs["data"] == {
        "model_id": "scribe_v2",
        "tag_audio_events": "true",
        "diarize": "true",
        "language_code": "eng",
    }
    assert post.call_args.kwargs["timeout"] == 120


def test_http_error_is_normalized_without_leaking_transport_exception(tmp_path):
    provider = ElevenLabsTranscriptionProvider()
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"wav")
    response = SimpleNamespace(
        status_code=401,
        json=lambda: {"detail": {"message": "invalid key"}},
        text="secret response body",
    )
    with (
        patch("plugins.voice.elevenlabs.get_profile_env", return_value="secret"),
        patch("plugins.voice.elevenlabs._config", return_value={}),
        patch("requests.post", return_value=response),
    ):
        result = provider.transcribe(str(audio))

    assert result["success"] is False
    assert result["provider"] == "elevenlabs"
    assert "HTTP 401" in result["error"]
    assert "invalid key" in result["error"]
