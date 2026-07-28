"""Project the configured tool surface onto active model capabilities.

Toolsets express what a session is authorized to use. Model capability
projection is a separate, request-time concern: a tool may be authorized but
redundant for the active model. Keeping the configured surface immutable lets
model switches and provider fallbacks recompute the correct request surface
without corrupting session authorization state.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


VISION_ANALYSIS_TOOL = "vision_analyze"


def _tool_name(tool: Dict[str, Any]) -> str:
    function = tool.get("function")
    if not isinstance(function, dict):
        return ""
    return str(function.get("name") or "").strip()


def project_tools_for_model(
    tools: Optional[List[Dict[str, Any]]],
    *,
    supports_native_vision: bool,
    image_input_mode: str = "auto",
) -> Optional[List[Dict[str, Any]]]:
    """Return the model-visible tools without mutating session toolsets.

    ``vision_analyze`` is a fallback for models that cannot consume images.
    Advertising it to a native-vision model encourages an unnecessary
    auxiliary round-trip and makes the UI show a misleading image-analysis
    tool call. The only exception is the explicit ``text`` input mode, where
    the user deliberately requested the auxiliary pipeline.
    """
    if not tools or not supports_native_vision or image_input_mode == "text":
        return tools

    return [tool for tool in tools if _tool_name(tool) != VISION_ANALYSIS_TOOL]


__all__ = ["VISION_ANALYSIS_TOOL", "project_tools_for_model"]
