"""Capability projection shared by managed Desktop model inventories."""

from __future__ import annotations

from typing import Any


def _models_dev_capabilities(
    provider_id: str,
    model_id: str,
) -> dict[str, Any]:
    """Read cached public metadata without making inventory a network owner."""

    try:
        from agent.models_dev import get_model_capabilities

        capabilities = get_model_capabilities(provider_id, model_id)
    except Exception:
        capabilities = None
    if capabilities is None:
        return {}
    return {
        "context_window": capabilities.context_window,
        "vision_enabled": capabilities.supports_vision,
        "reasoning_enabled": capabilities.supports_reasoning,
    }


def _upstream_capabilities(model_id: str) -> dict[str, Any]:
    """Resolve relay model IDs such as ``anthropic/claude-*``.

    Nous, OpenRouter, Vercel AI Gateway and other relays preserve the upstream
    provider in the model ID. Some relays have their own models.dev catalog,
    while others do not. Falling back to the upstream catalog keeps capability
    facts model-specific instead of treating every model behind a relay alike.
    """

    upstream_provider, separator, upstream_model = model_id.partition("/")
    if not separator or not upstream_provider.strip() or not upstream_model.strip():
        return {}
    return {
        **_models_dev_capabilities(upstream_provider, upstream_model),
        **_provider_capabilities(
            upstream_provider,
            upstream_model,
            base_url="",
            api_mode="",
        ),
    }


def _provider_capabilities(
    provider_id: str,
    model_id: str,
    *,
    base_url: str,
    api_mode: str,
) -> dict[str, Any]:
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(provider_id)
        if profile is None:
            return {}
        declared = profile.model_capabilities(
            model_id,
            base_url=base_url or None,
            api_mode=api_mode or None,
        )
        return dict(declared) if isinstance(declared, dict) else {}
    except Exception:
        return {}


def model_capability_metadata(
    provider_id: str,
    model_id: str,
    *,
    base_url: str = "",
    api_mode: str = "",
) -> dict[str, Any]:
    """Return Hermes-owned model capability metadata.

    Cached models.dev facts establish broad capabilities. Provider profiles
    then override them with the exact controls their request adapters support.
    Manual model metadata is merged by the caller last and therefore remains
    authoritative for arbitrary Custom Endpoint models.
    """

    catalog_capabilities = _models_dev_capabilities(provider_id, model_id)
    if not catalog_capabilities:
        catalog_capabilities = _upstream_capabilities(model_id)
    metadata = {
        **catalog_capabilities,
        **_provider_capabilities(
            provider_id,
            model_id,
            base_url=base_url,
            api_mode=api_mode,
        ),
    }
    if metadata.get("reasoning_enabled") is not True:
        metadata.pop("reasoning_efforts", None)
        metadata.pop("default_reasoning_effort", None)
        metadata.pop("reasoning_format", None)
    return metadata


__all__ = ["model_capability_metadata"]
