"""xAI (Grok) provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


class XAIProfile(ProviderProfile):
    """xAI Responses route with model-gated reasoning effort."""

    def model_capabilities(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_mode: str | None = None,
    ) -> dict[str, object]:
        from agent.model_metadata import grok_supports_reasoning_effort

        if not grok_supports_reasoning_effort(model):
            return {}
        return {
            "reasoning_enabled": True,
            "reasoning_efforts": ["none", "low", "medium", "high"],
            "default_reasoning_effort": "medium",
            "reasoning_format": "reasoning_items",
        }


xai = XAIProfile(
    name="xai",
    aliases=("grok", "x-ai", "x.ai"),
    api_mode="codex_responses",
    env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1",
    auth_type="api_key",
)

register_provider(xai)
