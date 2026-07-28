"""Domain service for dashboard-managed OpenAI-compatible endpoints.

The dashboard is one editor of ``providers.<id>`` blocks, not their owner.
This service therefore preserves fields it does not understand and keeps the
main-model mirror consistent when endpoints are activated or removed.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Callable

from hermes_cli.config import clear_model_endpoint_credentials, redact_key


class CustomEndpointValidationError(ValueError):
    """A user-provided endpoint definition is incomplete or malformed."""


def custom_endpoint_id(raw: str, fallback: str = "custom") -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw or "").strip())
    return slug.strip("-_").lower() or fallback


def models_from_entry(entry: dict[str, Any]) -> list[str]:
    models: list[str] = []
    raw_models = entry.get("models")
    if isinstance(raw_models, dict):
        models.extend(str(model).strip() for model in raw_models)
    elif isinstance(raw_models, list):
        models.extend(str(model).strip() for model in raw_models)
    default_model = str(entry.get("model") or entry.get("default_model") or "").strip()
    if default_model:
        models.insert(0, default_model)
    return list(dict.fromkeys(model for model in models if model))


def endpoint_response(config: dict[str, Any]) -> dict[str, Any]:
    model_config = config.get("model")
    model_config = model_config if isinstance(model_config, dict) else {}
    current_provider = str(model_config.get("provider") or "")
    current_model = str(
        model_config.get("default", model_config.get("name", "")) or ""
    )
    current_base_url = str(model_config.get("base_url") or "")

    endpoints: list[dict[str, Any]] = []
    providers = config.get("providers")
    if isinstance(providers, dict):
        for provider_id, raw_entry in providers.items():
            if not isinstance(raw_entry, dict):
                continue
            base_url = str(
                raw_entry.get("base_url")
                or raw_entry.get("url")
                or raw_entry.get("api")
                or ""
            ).strip()
            if not base_url:
                continue
            models = models_from_entry(raw_entry)
            endpoint_model = str(
                raw_entry.get("model")
                or raw_entry.get("default_model")
                or (models[0] if models else "")
            )
            api_key = str(raw_entry.get("api_key") or raw_entry.get("api") or "")
            endpoints.append(
                {
                    "id": str(provider_id),
                    "name": str(raw_entry.get("name") or provider_id),
                    "base_url": base_url,
                    "model": endpoint_model,
                    "models": models,
                    "context_length": raw_entry.get("context_length"),
                    "discover_models": bool(raw_entry.get("discover_models", True)),
                    "has_api_key": bool(api_key.strip()),
                    "api_key_preview": redact_key(api_key) if api_key else None,
                    "is_current": str(provider_id).lower() == current_provider.lower(),
                    "source": "providers",
                }
            )

    if (
        current_provider.lower() == "custom"
        and current_base_url
        and not any(endpoint["id"] == "custom" for endpoint in endpoints)
    ):
        api_key = str(model_config.get("api_key") or model_config.get("api") or "")
        endpoints.insert(
            0,
            {
                "id": "custom",
                "name": "Custom",
                "base_url": current_base_url,
                "model": current_model,
                "models": [current_model] if current_model else [],
                "context_length": model_config.get("context_length"),
                "discover_models": True,
                "has_api_key": bool(api_key.strip()),
                "api_key_preview": redact_key(api_key) if api_key else None,
                "is_current": True,
                "source": "direct-config",
            },
        )

    return {
        "endpoints": endpoints,
        "current": {
            "provider": current_provider,
            "model": current_model,
            "base_url": current_base_url,
        },
    }


def detach_main_model(config: dict[str, Any], provider_id: str) -> None:
    """Remove every inline endpoint credential when its provider is deleted."""
    model_config = config.get("model")
    if not isinstance(model_config, dict):
        return
    if str(model_config.get("provider") or "").strip().lower() != provider_id:
        return
    model_config.pop("provider", None)
    clear_model_endpoint_credentials(model_config, clear_base_url=True)
    config["model"] = model_config


def write_endpoint(
    config: dict[str, Any],
    body: Any,
    *,
    apply_main_assignment: Callable[..., dict],
) -> tuple[str, dict[str, Any]]:
    endpoint_id = custom_endpoint_id(body.id or body.name)
    name = str(body.name or "").strip()
    base_url = str(body.base_url or "").strip().rstrip("/")
    model = str(body.model or "").strip()
    if not name:
        raise CustomEndpointValidationError("name required")
    if not base_url:
        raise CustomEndpointValidationError("base_url required")
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise CustomEndpointValidationError("base_url must include scheme and host")
    if not model:
        raise CustomEndpointValidationError("model required")

    providers = config.get("providers")
    providers = providers if isinstance(providers, dict) else {}
    existing = providers.get(endpoint_id)
    existing = existing if isinstance(existing, dict) else {}

    # Preserve hand-written transport/auth/request fields the dashboard does
    # not expose. The same rule applies to the full model catalog.
    entry: dict[str, Any] = dict(existing)
    entry.update(
        {
            "name": name,
            "base_url": base_url,
            "model": model,
            "discover_models": bool(body.discover_models),
        }
    )
    existing_models = entry.get("models")
    models = dict(existing_models) if isinstance(existing_models, dict) else {}
    model_entry = models.get(model)
    models[model] = dict(model_entry) if isinstance(model_entry, dict) else {}
    entry["models"] = models
    if body.context_length and body.context_length > 0:
        entry["context_length"] = int(body.context_length)
        entry["models"][model]["context_length"] = int(body.context_length)
    if body.api_key is not None and str(body.api_key).strip():
        entry["api_key"] = str(body.api_key).strip()

    providers[endpoint_id] = entry
    config["providers"] = providers
    if body.make_default:
        model_config = apply_main_assignment(
            config.get("model", {}), endpoint_id, model, base_url
        )
        if entry.get("api_key"):
            model_config["api_key"] = entry["api_key"]
            model_config.pop("api", None)
        config["model"] = model_config
    return endpoint_id, entry


def activate_endpoint(
    config: dict[str, Any],
    provider_id: str,
    *,
    apply_main_assignment: Callable[..., dict],
) -> tuple[str, str]:
    provider_key = custom_endpoint_id(provider_id)
    providers = config.get("providers")
    entry = providers.get(provider_key) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        raise KeyError("custom endpoint not found")
    models = models_from_entry(entry)
    model = str(entry.get("model") or (models[0] if models else "")).strip()
    base_url = str(entry.get("base_url") or "").strip()
    if not model or not base_url:
        raise CustomEndpointValidationError("custom endpoint is incomplete")
    model_config = apply_main_assignment(
        config.get("model", {}), provider_key, model, base_url
    )
    api_key = entry.get("api_key") or entry.get("api")
    if api_key:
        model_config["api_key"] = api_key
        model_config.pop("api", None)
    config["model"] = model_config
    return provider_key, model


def delete_endpoint(config: dict[str, Any], provider_id: str) -> None:
    provider_key = custom_endpoint_id(provider_id)
    providers = config.get("providers")
    if not isinstance(providers, dict) or provider_key not in providers:
        raise KeyError("custom endpoint not found")
    providers.pop(provider_key)
    config["providers"] = providers
    detach_main_model(config, provider_key)


def parse_model_ids(response: Any) -> list[str]:
    try:
        if not response.is_success:
            return []
        payload = response.json()
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    ids = [
        str(item.get("id") if isinstance(item, dict) else item or "").strip()
        for item in data
    ]
    return [model_id for model_id in ids if model_id]


__all__ = [
    "CustomEndpointValidationError",
    "activate_endpoint",
    "custom_endpoint_id",
    "delete_endpoint",
    "detach_main_model",
    "endpoint_response",
    "models_from_entry",
    "parse_model_ids",
    "write_endpoint",
]
