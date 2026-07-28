"""OpenCode provider profiles (Zen + Go).

OpenCode Go multiplexes several model families through one endpoint. Its
profile centralizes each family's request contract so reasoning controls do
not leak between GLM, Kimi, DeepSeek, or unrelated models.
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


def _flat_model_name(model: str | None) -> str:
    return (model or "").strip().rsplit("/", 1)[-1].lower()


def _is_kimi_k2_model(model: str | None) -> bool:
    return _flat_model_name(model).startswith("kimi-k2")


def _is_deepseek_thinking_model(model: str | None) -> bool:
    normalized = _flat_model_name(model)
    if normalized.startswith("deepseek-v") and not normalized.startswith("deepseek-v3"):
        return True
    return normalized == "deepseek-reasoner"


def _is_glm_5_2_model(model: str | None) -> bool:
    normalized = _flat_model_name(model)
    return any(token in normalized for token in ("glm-5.2", "glm-5-2", "glm-5p2"))


class OpenCodeGoProfile(ProviderProfile):
    """OpenCode Go model-specific limits and reasoning controls."""

    _MODEL_MAX_TOKENS: dict[str, int] = {
        "mimo-v2.5-pro": 131072,
    }

    def get_max_tokens(self, model: str | None) -> int | None:
        return self._MODEL_MAX_TOKENS.get(
            _flat_model_name(model), self.default_max_tokens
        )

    def model_capabilities(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_mode: str | None = None,
    ) -> dict[str, Any]:
        if _is_glm_5_2_model(model):
            return {
                "reasoning_enabled": True,
                "reasoning_efforts": ["none", "high", "max"],
                "default_reasoning_effort": "high",
                "reasoning_format": "reasoning_content",
            }
        if _is_kimi_k2_model(model):
            return {
                "reasoning_enabled": True,
                "reasoning_efforts": [
                    "none",
                    "enabled",
                    "low",
                    "medium",
                    "high",
                ],
                "default_reasoning_effort": "enabled",
                "reasoning_format": "reasoning_content",
            }
        if _is_deepseek_thinking_model(model):
            return {
                "reasoning_enabled": True,
                "reasoning_efforts": [
                    "none",
                    "enabled",
                    "low",
                    "medium",
                    "high",
                    "max",
                ],
                "default_reasoning_effort": "enabled",
                "reasoning_format": "reasoning_content",
            }
        if _flat_model_name(model).startswith("minimax-"):
            return {
                "reasoning_enabled": True,
                "reasoning_efforts": [
                    "none",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                ],
                "default_reasoning_effort": "medium",
                "reasoning_format": "thinking_blocks",
            }
        return {}

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if _is_glm_5_2_model(model):
            if not isinstance(reasoning_config, dict):
                return extra_body, top_level
            if reasoning_config.get("enabled") is False:
                return extra_body, top_level
            effort = str(reasoning_config.get("effort") or "").strip().lower()
            if not effort or effort == "none":
                return extra_body, top_level
            top_level["reasoning_effort"] = (
                "max" if effort in {"xhigh", "max", "ultra"} else "high"
            )
            return extra_body, top_level

        if _is_kimi_k2_model(model):
            if not isinstance(reasoning_config, dict):
                return extra_body, top_level
            if reasoning_config.get("enabled") is False:
                extra_body["thinking"] = {"type": "disabled"}
                return extra_body, top_level

            effort = str(reasoning_config.get("effort") or "").strip().lower()
            if effort in {"xhigh", "max", "ultra"}:
                top_level["reasoning_effort"] = "high"
            elif effort in {"low", "medium", "high"}:
                top_level["reasoning_effort"] = effort

            if "reasoning_effort" not in top_level:
                extra_body["thinking"] = {"type": "enabled"}
            return extra_body, top_level

        if not _is_deepseek_thinking_model(model):
            return extra_body, top_level

        enabled = not (
            isinstance(reasoning_config, dict)
            and reasoning_config.get("enabled") is False
        )
        if not enabled:
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        if isinstance(reasoning_config, dict):
            effort = str(reasoning_config.get("effort") or "").strip().lower()
            if effort in {"xhigh", "max", "ultra"}:
                top_level["reasoning_effort"] = "max"
            elif effort in {"low", "medium", "high"}:
                top_level["reasoning_effort"] = effort

        if "reasoning_effort" not in top_level:
            extra_body["thinking"] = {"type": "enabled"}
        return extra_body, top_level


opencode_zen = ProviderProfile(
    name="opencode-zen",
    aliases=("opencode", "opencode_zen", "zen"),
    env_vars=("OPENCODE_ZEN_API_KEY",),
    base_url="https://opencode.ai/zen/v1",
    default_aux_model="gemini-3-flash",
)

opencode_go = OpenCodeGoProfile(
    name="opencode-go",
    aliases=("opencode_go", "go", "opencode-go-sub"),
    env_vars=("OPENCODE_GO_API_KEY",),
    base_url="https://opencode.ai/zen/go/v1",
    default_aux_model="glm-5",
)

register_provider(opencode_zen)
register_provider(opencode_go)
