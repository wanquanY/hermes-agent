"""Google Gemini provider profiles.

gemini:            Google AI Studio (API key) — uses GeminiNativeClient

Reports api_mode="chat_completions" but uses a custom native client
that bypasses the standard OpenAI transport. The profile captures auth
and endpoint metadata for auth.py / runtime_provider.py migration, and
carries the thinking_config translation hook so the transport's profile
path produces the same extra_body shape the legacy flag path did.
"""

import json
import logging
import urllib.parse
import urllib.request
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile
from hermes_cli.urllib_security import open_credentialed_url

logger = logging.getLogger(__name__)


class GeminiProfile(ProviderProfile):
    """Gemini — translate reasoning_config to thinking_config in extra_body."""

    def model_capabilities(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_mode: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(model or "").strip().lower().removeprefix("google/")
        if not normalized.startswith("gemini"):
            return {"reasoning_enabled": False}
        if normalized.startswith("gemini-2.5-"):
            efforts = ["none", "enabled"]
        elif normalized.startswith("gemini-3.5-") and "flash" in normalized:
            efforts = ["none", "minimal", "low", "medium", "high"]
        elif normalized.startswith("gemini-3"):
            efforts = ["none", "low", "medium", "high"]
        else:
            efforts = ["none", "enabled"]
        return {
            "reasoning_enabled": True,
            "reasoning_efforts": efforts,
            "default_reasoning_effort": (
                "medium" if "medium" in efforts else "enabled"
            ),
            "reasoning_format": "thinking_parts",
        }

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Validate an AI Studio key through Gemini's native model catalog."""
        if not api_key:
            return None
        effective_base = (base_url or self.base_url or "").rstrip("/")
        if not effective_base:
            return None
        url = f"{effective_base}/models?{urllib.parse.urlencode({'key': api_key})}"
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json"},
            )
            with open_credentialed_url(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return []
            return [
                str(item.get("name") or "").removeprefix("models/")
                for item in models
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
        except Exception as exc:
            logger.debug("fetch_models(gemini): %s", exc)
            return None

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        """Emit extra_body.thinking_config (native) or extra_body.extra_body.google.thinking_config
        (OpenAI-compat /openai subpath), mirroring the legacy path's behavior.
        """
        from agent.transports.chat_completions import (
            _build_gemini_thinking_config,
            _is_gemini_openai_compat_base_url,
            _snake_case_gemini_thinking_config,
        )

        model = context.get("model") or ""
        reasoning_config = context.get("reasoning_config")
        base_url = context.get("base_url") or self.base_url

        raw_thinking_config = _build_gemini_thinking_config(model, reasoning_config)
        if not raw_thinking_config:
            return {}

        body: dict[str, Any] = {}
        if self.name == "gemini" and _is_gemini_openai_compat_base_url(base_url):
            thinking_config = _snake_case_gemini_thinking_config(raw_thinking_config)
            if thinking_config:
                body["extra_body"] = {"google": {"thinking_config": thinking_config}}
        else:
            body["thinking_config"] = raw_thinking_config
        return body


gemini = GeminiProfile(
    name="gemini",
    aliases=("google", "google-gemini", "google-ai-studio"),
    api_mode="chat_completions",
    env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta",
    auth_type="api_key",
    default_aux_model="gemini-3.5-flash",
)

register_provider(gemini)
