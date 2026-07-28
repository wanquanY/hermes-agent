"""ElevenLabs Scribe STT provider."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.secret_scope import get_profile_env
from agent.transcription_provider import TranscriptionProvider
from utils import is_truthy_value

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.elevenlabs.io/v1"


def _config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        root = load_config().get("stt")
        section = root.get("elevenlabs") if isinstance(root, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


class ElevenLabsTranscriptionProvider(TranscriptionProvider):
    @property
    def name(self) -> str:
        return "elevenlabs"

    @property
    def display_name(self) -> str:
        return "ElevenLabs Scribe"

    def is_available(self) -> bool:
        return bool((get_profile_env("ELEVENLABS_API_KEY", "") or "").strip())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": "scribe_v2", "display": "Scribe v2"},
            {"id": "scribe_v1", "display": "Scribe v1"},
        ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "ElevenLabs Scribe",
            "badge": "paid",
            "tag": "Speech recognition with diarization and audio-event tags",
            "env_vars": [
                {
                    "key": "ELEVENLABS_API_KEY",
                    "prompt": "ElevenLabs API key",
                    "url": "https://elevenlabs.io/app/settings/api-keys",
                }
            ],
        }

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        api_key = (get_profile_env("ELEVENLABS_API_KEY", "") or "").strip()
        if not api_key:
            return self._error("ELEVENLABS_API_KEY not set")
        config = _config()
        base_url = str(
            config.get("base_url")
            or get_profile_env("ELEVENLABS_STT_BASE_URL", "")
            or _DEFAULT_BASE_URL
        ).strip().rstrip("/")
        payload = {
            "model_id": str(model or config.get("model_id") or "scribe_v2"),
            "tag_audio_events": (
                "true" if is_truthy_value(config.get("tag_audio_events", False)) else "false"
            ),
            "diarize": "true" if is_truthy_value(config.get("diarize", False)) else "false",
        }
        language_code = str(language or config.get("language_code") or "").strip()
        if language_code:
            payload["language_code"] = language_code
        try:
            import requests

            with open(file_path, "rb") as audio_file:
                response = requests.post(
                    f"{base_url}/speech-to-text",
                    headers={"xi-api-key": api_key},
                    files={"file": (Path(file_path).name, audio_file)},
                    data=payload,
                    timeout=120,
                )
            if response.status_code != 200:
                return self._error(
                    f"ElevenLabs STT API error (HTTP {response.status_code}): "
                    f"{self._error_detail(response)}"
                )
            result = response.json()
            text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
            if not text:
                return self._error("ElevenLabs STT returned an empty transcript")
            return {"success": True, "transcript": text, "provider": self.name}
        except Exception as exc:
            logger.warning("ElevenLabs STT failed: %s", exc, exc_info=True)
            return self._error(f"ElevenLabs STT failed: {exc}")

    def _error(self, message: str) -> Dict[str, Any]:
        return {"success": False, "transcript": "", "error": message, "provider": self.name}

    @staticmethod
    def _error_detail(response: Any) -> str:
        try:
            body = response.json()
            value = body.get("detail") or body.get("error")
            if isinstance(value, dict):
                return str(value.get("message") or value)
            if value:
                return str(value)
        except Exception:
            pass
        return str(getattr(response, "text", "") or "")[:300]


def register(ctx) -> None:
    ctx.register_transcription_provider(ElevenLabsTranscriptionProvider())
