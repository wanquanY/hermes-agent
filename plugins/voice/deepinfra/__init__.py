"""DeepInfra voice backend registered through Hermes' typed plugin APIs."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.secret_scope import get_profile_env
from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"


def _provider_config(kind: str) -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        root = load_config().get(kind)
        section = root.get("deepinfra") if isinstance(root, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _base_url(config: Dict[str, Any]) -> str:
    value = (
        config.get("base_url")
        or get_profile_env("DEEPINFRA_BASE_URL", "")
        or _DEFAULT_BASE_URL
    )
    return str(value).strip().rstrip("/")


def _catalog_models(tag: str) -> List[Dict[str, Any]]:
    try:
        from hermes_cli.models import _fetch_deepinfra_models_by_tag

        return _fetch_deepinfra_models_by_tag(tag) or []
    except Exception:
        logger.debug("DeepInfra %s catalog lookup failed", tag, exc_info=True)
        return []


def _model_rows(tag: str) -> List[Dict[str, Any]]:
    rows = []
    for item in _catalog_models(tag):
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        metadata = item.get("metadata") if isinstance(item, dict) else None
        metadata = metadata if isinstance(metadata, dict) else {}
        rows.append(
            {
                "id": model_id,
                "display": model_id.split("/", 1)[-1],
                "strengths": str(metadata.get("description") or ""),
            }
        )
    return rows


def _api_key() -> str:
    return (get_profile_env("DEEPINFRA_API_KEY", "") or "").strip()


def _setup_schema(capability: str) -> Dict[str, Any]:
    return {
        "name": f"DeepInfra {capability}",
        "badge": "paid",
        "tag": "OpenAI-compatible audio API with live model discovery",
        "env_vars": [
            {
                "key": "DEEPINFRA_API_KEY",
                "prompt": "DeepInfra API key",
                "url": "https://deepinfra.com/dash/api_keys",
            }
        ],
    }


class DeepInfraTTSProvider(TTSProvider):
    @property
    def name(self) -> str:
        return "deepinfra"

    @property
    def display_name(self) -> str:
        return "DeepInfra"

    @property
    def voice_compatible(self) -> bool:
        return True

    def is_available(self) -> bool:
        return bool(_api_key()) and importlib.util.find_spec("openai") is not None

    def list_models(self) -> List[Dict[str, Any]]:
        return _model_rows("tts")

    def get_setup_schema(self) -> Dict[str, Any]:
        return _setup_schema("TTS")

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        format: str = "mp3",
        **extra: Any,
    ) -> str:
        api_key = _api_key()
        if not api_key:
            raise ValueError("DEEPINFRA_API_KEY not set")
        config = _provider_config("tts")
        model_id = model or config.get("model") or self.default_model()
        if not model_id:
            raise ValueError(
                "No DeepInfra TTS model available; set tts.deepinfra.model "
                "or restore access to the live catalog"
            )

        try:
            import openai
        except ImportError as exc:
            raise RuntimeError("DeepInfra TTS requires the openai package") from exc

        response_format = str(format or "mp3").lower()
        if response_format == "ogg":
            response_format = "opus"
        client = openai.OpenAI(
            api_key=api_key,
            base_url=_base_url(config),
            timeout=120,
            max_retries=2,
        )
        try:
            response = client.audio.speech.create(
                model=str(model_id),
                voice=str(voice or config.get("voice") or "default"),
                input=text,
                response_format=response_format,
                speed=float(speed if speed is not None else config.get("speed", 1.0)),
            )
            response.stream_to_file(output_path)
            return output_path
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()


class DeepInfraTranscriptionProvider(TranscriptionProvider):
    @property
    def name(self) -> str:
        return "deepinfra"

    @property
    def display_name(self) -> str:
        return "DeepInfra"

    def is_available(self) -> bool:
        return bool(_api_key()) and importlib.util.find_spec("openai") is not None

    def list_models(self) -> List[Dict[str, Any]]:
        return _model_rows("stt")

    def get_setup_schema(self) -> Dict[str, Any]:
        return _setup_schema("STT")

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        api_key = _api_key()
        if not api_key:
            return self._error("DEEPINFRA_API_KEY not set")
        config = _provider_config("stt")
        model_id = model or config.get("model") or self.default_model()
        if not model_id:
            return self._error(
                "No DeepInfra STT model available; set stt.deepinfra.model "
                "or restore access to the live catalog"
            )
        try:
            import openai

            client = openai.OpenAI(
                api_key=api_key,
                base_url=_base_url(config),
                timeout=120,
                max_retries=2,
            )
            try:
                with open(file_path, "rb") as audio_file:
                    response = client.audio.transcriptions.create(
                        model=str(model_id),
                        file=audio_file,
                        response_format="json",
                        **({"language": language} if language else {}),
                    )
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            text = self._extract_text(response)
            if not text:
                return self._error("DeepInfra STT returned an empty transcript")
            return {"success": True, "transcript": text, "provider": self.name}
        except Exception as exc:
            logger.warning("DeepInfra STT failed: %s", exc, exc_info=True)
            return self._error(f"DeepInfra STT failed: {exc}")

    def _error(self, message: str) -> Dict[str, Any]:
        return {"success": False, "transcript": "", "error": message, "provider": self.name}

    @staticmethod
    def _extract_text(response: Any) -> str:
        if isinstance(response, str):
            return response.strip()
        if isinstance(response, dict):
            return str(response.get("text") or "").strip()
        return str(getattr(response, "text", "") or "").strip()


def register(ctx) -> None:
    ctx.register_tts_provider(DeepInfraTTSProvider())
    ctx.register_transcription_provider(DeepInfraTranscriptionProvider())
