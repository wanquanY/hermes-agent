"""Custom / Ollama (local) provider profile.

Covers any endpoint registered as provider="custom", including local
Ollama instances and OpenAI-compatible reasoning endpoints (GLM-5.2 on
Volcengine ARK, vLLM, llama.cpp). Key quirks:
  - ollama_num_ctx → extra_body.options.num_ctx (local context window)
  - reasoning_config disabled → extra_body.think = False
  - reasoning_config enabled + effort → top-level reasoning_effort
    (the native OpenAI-compatible format; unset omits it so the endpoint's
    server default applies)
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class CustomProfile(ProviderProfile):
    """Custom/Ollama local provider — think=false and num_ctx support."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        ollama_num_ctx: int | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Ollama context window
        if ollama_num_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_num_ctx
            extra_body["options"] = options

        # Dovie Cloud is a named custom endpoint at runtime, but its wire is a
        # stable product contract rather than a vendor API.  Always send the
        # selected product value as top-level `reasoning_effort`; the Dovie
        # model registry validates it and translates it for the resolved
        # upstream provider.
        requested_provider = str(ctx.get("requested_provider") or "").strip().lower()
        if requested_provider == "dovie-cloud":
            if isinstance(reasoning_config, dict):
                effort = str(reasoning_config.get("effort") or "").strip().lower()
                if reasoning_config.get("enabled") is False:
                    top_level["reasoning_effort"] = "none"
                elif effort:
                    top_level["reasoning_effort"] = effort
                elif reasoning_config.get("enabled") is True:
                    top_level["reasoning_effort"] = "enabled"
            return extra_body, top_level

        # Custom endpoints do not share one enable flag: Ollama understands
        # ``think=false``, while GLM/vLLM-style OpenAI-compatible APIs accept
        # a top-level ``reasoning_effort``. Never force ``think=true`` because
        # non-Ollama endpoints reject it; an unset effort deliberately leaves
        # the server default untouched.
        if reasoning_config and isinstance(reasoning_config, dict):
            _effort = (reasoning_config.get("effort") or "").strip().lower()
            _enabled = reasoning_config.get("enabled", True)
            if _effort == "none" or _enabled is False:
                extra_body["think"] = False
            elif _effort:
                top_level["reasoning_effort"] = _effort

        return extra_body, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Custom/Ollama: base_url is user-configured; fetch if set."""
        effective_base_url = str(base_url or self.base_url or "").strip()
        if not effective_base_url:
            return None
        return super().fetch_models(
            api_key=api_key,
            base_url=effective_base_url,
            timeout=timeout,
        )


custom = CustomProfile(
    name="custom",
    aliases=(
        "ollama",
        "local",
        "vllm",
        "llamacpp",
        "llama.cpp",
        "llama-cpp",
    ),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # Without an explicit max_tokens Ollama falls back to a tiny internal
    # num_predict default. This remains user-overridable per model.
    default_max_tokens=65536,
)

register_provider(custom)
