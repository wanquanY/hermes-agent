from __future__ import annotations

from typing import Any


def normalize_model_descriptor(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    descriptor_id = str(raw.get("id") or "").strip()
    if not descriptor_id:
        return {}

    descriptor: dict[str, Any] = {"id": descriptor_id}
    for key in (
        "name",
        "provider",
        "api_provider",
        "api_format",
        "reasoning_format",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            descriptor[key] = value.strip()

    context_window = raw.get("context_window")
    if isinstance(context_window, (int, float)) and context_window > 0:
        descriptor["context_window"] = int(context_window)

    for key in (
        "vision_enabled",
        "reasoning_enabled",
        "reasoning_extra_body_enabled",
        "responses_api_required_for_reasoning_with_tools",
    ):
        value = raw.get(key)
        if isinstance(value, bool):
            descriptor[key] = value

    request_params = raw.get("request_params")
    if isinstance(request_params, dict):
        descriptor["request_params"] = dict(request_params)

    return descriptor


def set_session_model_descriptor(
    session: dict[str, Any],
    descriptor: dict[str, Any],
    *,
    clear_if_empty: bool = False,
) -> None:
    normalized = normalize_model_descriptor(descriptor)
    agent = session.get("agent") if session else None
    if normalized:
        session["model_descriptor"] = normalized
        if agent is not None:
            setattr(agent, "model_descriptor", normalized)
        return
    if clear_if_empty:
        session.pop("model_descriptor", None)
        if agent is not None and hasattr(agent, "model_descriptor"):
            setattr(agent, "model_descriptor", {})
