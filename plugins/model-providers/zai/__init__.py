"""ZAI / GLM provider profile.

GLM-4.5-and-later models expose a binary ``thinking`` control. GLM-5.2
additionally exposes top-level ``reasoning_effort`` with the enabled levels
``high`` and ``max``. The profile owns both translations so transport code
does not need model/provider special cases.
"""

from __future__ import annotations

import re
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

_GLM_VERSION_RE = re.compile(r"^glm-(\d+)(?:\.(\d+))?")


def _bare_model_name(model: str | None) -> str:
    return (model or "").strip().lower().rsplit("/", 1)[-1]


def _model_supports_thinking(model: str | None) -> bool:
    """Return whether the canonical GLM family supports thinking control."""
    match = _GLM_VERSION_RE.match(_bare_model_name(model))
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (major, minor) >= (4, 5)


def _is_glm_5_2(model: str | None) -> bool:
    """Detect GLM-5.2 across canonical and relay-specific spellings."""
    normalized = (model or "").strip().lower()
    return any(token in normalized for token in ("glm-5.2", "glm-5-2", "glm-5p2"))


def _glm_5_2_reasoning_effort(reasoning_config: dict | None) -> str | None:
    """Map Hermes' effort scale onto GLM-5.2's native high/max scale."""
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None

    effort = str(reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None
    if effort in {"xhigh", "max", "ultra"}:
        return "max"
    return "high"


class ZaiProfile(ProviderProfile):
    """Z.AI / GLM reasoning wire contract."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not _model_supports_thinking(model) and not _is_glm_5_2(model):
            return extra_body, top_level

        if isinstance(reasoning_config, dict):
            enabled = reasoning_config.get("enabled") is not False
            extra_body["thinking"] = {
                "type": "enabled" if enabled else "disabled"
            }

        if _is_glm_5_2(model):
            effort = _glm_5_2_reasoning_effort(reasoning_config)
            if effort is not None:
                top_level["reasoning_effort"] = effort

        return extra_body, top_level


zai = ZaiProfile(
    name="zai",
    aliases=("glm", "z-ai", "z.ai", "zhipu"),
    env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
    display_name="Z.AI (GLM)",
    description="Z.AI / GLM — Zhipu AI models",
    signup_url="https://z.ai/",
    fallback_models=(
        "glm-5.2",
        "glm-5",
        "glm-4-9b",
    ),
    base_url="https://api.z.ai/api/paas/v4",
    default_aux_model="glm-4.5-flash",
)

register_provider(zai)
