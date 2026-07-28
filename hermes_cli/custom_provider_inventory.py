"""Saved custom-provider inventory and live catalog discovery.

This module owns the expensive and stateful portion of provider listing so
``model_switch`` remains a routing layer.  It deliberately groups by endpoint,
credential identity, transport, and display prefix: entries for multiple
models of one provider collapse, while distinct providers behind one proxy do
not accidentally share credentials or model catalogs.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any

from hermes_cli.config import normalize_extra_headers
from hermes_cli.providers import custom_provider_slug


def declared_model_ids(value: Any) -> list[str]:
    """Return unique model IDs from every supported config representation."""
    candidates: list[Any]
    if isinstance(value, dict):
        candidates = list(value)
    elif isinstance(value, (list, tuple)):
        candidates = [
            item.get("id") or item.get("name") if isinstance(item, dict) else item
            for item in value
        ]
    else:
        candidates = [value]
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        model_id = candidate.strip()
        if model_id.lower() not in seen:
            seen.add(model_id.lower())
            result.append(model_id)
    return result


def save_discovered_models_to_config(api_url: str, model_ids: list[str]) -> None:
    """Cache a successful legacy custom-provider probe without erasing metadata."""
    if not api_url or not model_ids:
        return
    try:
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        providers = cfg.get("custom_providers") or []
        if not isinstance(providers, list):
            return
        target = api_url.strip().rstrip("/").lower()
        changed = False
        for entry in providers:
            if not isinstance(entry, dict):
                continue
            entry_url = str(
                entry.get("base_url") or entry.get("url") or entry.get("api") or ""
            ).strip().rstrip("/").lower()
            if entry_url != target:
                continue
            existing = entry.get("models")
            if isinstance(existing, dict) or (
                isinstance(existing, list)
                and any(isinstance(item, dict) for item in existing)
            ):
                continue
            if existing == model_ids:
                continue
            entry["models"] = list(model_ids)
            changed = True
        if changed:
            cfg["custom_providers"] = providers
            save_config(cfg)
    except Exception:
        # Inventory must remain readable when a cache write is unavailable.
        pass


def _group_display_name(display_name: str) -> str:
    grouped = display_name
    for separator in ("—", " - "):
        if separator in grouped:
            grouped = grouped.split(separator, 1)[0].strip()
            break
    tokens = grouped.split()
    digit_index = next(
        (
            index
            for index, token in enumerate(tokens)
            if any(char.isdigit() for char in token.strip(".,()"))
        ),
        None,
    )
    if digit_index is not None and digit_index >= 2:
        grouped = " ".join(tokens[:digit_index]).strip()
    return grouped or display_name


def build_keyed_provider_rows(
    user_providers: dict | None,
    *,
    current_provider: str,
    current_base_url: str,
    seen_slugs: set[str],
    curated_models: dict[str, list[str]],
    probe_all: bool,
    probe_current: bool,
    excluded_names: set[str],
) -> tuple[list[dict], set[tuple[str, str]]]:
    """Group v12 ``providers:`` entries into stable picker rows."""
    if not isinstance(user_providers, dict):
        return [], set()
    from hermes_cli.config import is_provider_enabled

    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for raw_slug, entry in user_providers.items():
        slug = str(raw_slug).strip()
        if not slug or not isinstance(entry, dict) or not is_provider_enabled(entry):
            continue
        display_name = str(entry.get("name") or slug).strip()
        if {slug.lower(), display_name.lower()} & excluded_names:
            continue
        if slug.lower() in seen_slugs:
            continue
        api_url = str(
            entry.get("base_url") or entry.get("api") or entry.get("url") or ""
        ).strip().rstrip("/")
        key_env = str(entry.get("key_env") or "").strip()
        inline_key = str(entry.get("api_key") or "").strip()
        api_key = inline_key or (os.environ.get(key_env, "").strip() if key_env else "")
        credential_identity = inline_key or (f"env:{key_env}" if key_env else "")
        api_mode = str(entry.get("api_mode") or entry.get("transport") or "").strip().lower()
        extra_headers = normalize_extra_headers(entry.get("extra_headers"))
        group_key = (
            api_url.lower(),
            credential_identity,
            api_mode,
            tuple(sorted(extra_headers.items())),
        )
        discover = entry.get("discover_models", True)
        if isinstance(discover, str):
            discover = discover.strip().lower() not in {"false", "0", "no", "off"}
        group = groups.setdefault(
            group_key,
            {
                "slug": slug,
                "name": _group_display_name(display_name),
                "api_url": api_url,
                "api_key": api_key,
                "api_mode": api_mode,
                "extra_headers": extra_headers,
                "discover_models": bool(discover),
                "models": [],
                "raw_names": [],
            },
        )
        if api_key and not group["api_key"]:
            group["api_key"] = api_key
        if not discover:
            group["discover_models"] = False
        active_model = str(entry.get("default_model") or entry.get("model") or "").strip()
        entry_models = ([active_model] if active_model else []) + declared_model_ids(
            entry.get("models")
        )
        for model_id in entry_models:
            if model_id and model_id not in group["models"]:
                group["models"].append(model_id)
        group["raw_names"].append(display_name)

    current_provider_norm = str(current_provider or "").strip().lower()
    current_url_norm = str(current_base_url or "").strip().rstrip("/").lower()
    rows: list[dict] = []
    emitted_pairs: set[tuple[str, str]] = set()
    for group in groups.values():
        models = list(group["models"])
        api_url = group["api_url"]
        if not models and "api.openai.com" in api_url.lower():
            models = list(curated_models.get("openai") or [])
        url_norm = api_url.lower()
        custom_slug = custom_provider_slug(group["name"]).lower()
        is_current = (
            group["slug"].lower() == current_provider_norm
            or custom_slug == current_provider_norm
            or (
                current_provider_norm == "custom"
                and bool(current_url_norm)
                and url_norm == current_url_norm
            )
        )
        should_probe = (
            (probe_all or (probe_current and is_current))
            and bool(api_url)
            and group["discover_models"]
            and (bool(group["api_key"]) or not models)
        )
        if should_probe:
            try:
                from hermes_cli.models import fetch_api_models

                probe_kwargs: dict[str, Any] = {}
                if group["api_mode"]:
                    probe_kwargs["api_mode"] = group["api_mode"]
                if group["extra_headers"]:
                    probe_kwargs["headers"] = group["extra_headers"]
                live_models = fetch_api_models(
                    group["api_key"], api_url, **probe_kwargs
                )
                if live_models:
                    models = live_models
            except Exception:
                pass
        rows.append(
            {
                "slug": group["slug"],
                "name": group["name"],
                "is_current": is_current,
                "is_user_defined": True,
                "models": models,
                "total_models": len(models),
                "source": "user-config",
                "api_url": api_url,
            }
        )
        seen_slugs.update((group["slug"].lower(), custom_slug))
        for raw_name in group["raw_names"] or [group["name"]]:
            emitted_pairs.add((raw_name.lower(), url_norm))
            seen_slugs.add(custom_provider_slug(raw_name).lower())
        emitted_pairs.add((group["name"].lower(), url_norm))
    return rows, emitted_pairs


def build_saved_custom_provider_rows(
    custom_providers: list | None,
    *,
    current_provider: str,
    current_model: str,
    current_base_url: str,
    seen_slugs: set[str],
    emitted_provider_pairs: set[tuple[str, str]],
    builtin_endpoints: set[str],
    probe_all: bool,
    probe_current: bool,
    excluded_names: set[str],
) -> list[dict]:
    """Build picker rows for legacy and compatibility-normalized providers."""
    if not isinstance(custom_providers, list):
        return []

    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        raw_name = str(entry.get("name") or "").strip()
        api_url = str(
            entry.get("base_url") or entry.get("url") or entry.get("api") or ""
        ).strip().rstrip("/")
        if not raw_name or not api_url:
            continue

        display_prefix = raw_name
        for separator in ("—", " - "):
            if separator in display_prefix:
                display_prefix = display_prefix.split(separator, 1)[0].strip()
                break
        display_name = display_prefix or raw_name
        slug = custom_provider_slug(display_name)
        if {slug.lower(), raw_name.lower(), display_name.lower()} & excluded_names:
            continue

        inline_key = str(entry.get("api_key") or "").strip()
        key_env = str(entry.get("key_env") or "").strip()
        api_key = inline_key or (os.environ.get(key_env, "").strip() if key_env else "")
        credential_identity = inline_key or (f"env:{key_env}" if key_env else "")
        api_mode = str(entry.get("api_mode") or entry.get("transport") or "").strip().lower()
        extra_headers = normalize_extra_headers(entry.get("extra_headers"))
        discover = entry.get("discover_models", True)
        if isinstance(discover, str):
            discover = discover.strip().lower() not in {"false", "0", "no", "off"}

        group_key = (
            api_url.lower(),
            credential_identity,
            api_mode,
            tuple(sorted(extra_headers.items())),
            display_name.lower(),
        )
        group = groups.setdefault(
            group_key,
            {
                "slug": slug,
                "name": display_name,
                "api_url": api_url,
                "api_key": api_key,
                "api_mode": api_mode,
                "extra_headers": extra_headers,
                "models": [],
                "has_explicit_models": False,
                "discover_models": bool(discover),
            },
        )
        if api_key and not group["api_key"]:
            group["api_key"] = api_key
        if not discover:
            group["discover_models"] = False

        active_model = str(entry.get("model") or entry.get("default_model") or "").strip()
        if active_model and active_model not in group["models"]:
            group["models"].append(active_model)
        declared = declared_model_ids(entry.get("models"))
        group["has_explicit_models"] = group["has_explicit_models"] or bool(declared)
        for model_id in declared:
            if model_id not in group["models"]:
                group["models"].append(model_id)

    current_provider_norm = str(current_provider or "").strip().lower()
    current_url_norm = str(current_base_url or "").strip().rstrip("/").lower()
    matching_current_urls = sum(
        str(group["api_url"]).lower() == current_url_norm
        for group in groups.values()
        if current_url_norm
    )
    rows: list[dict] = []
    if (
        current_provider_norm == "custom"
        and current_url_norm
        and not matching_current_urls
        and "custom" not in excluded_names
    ):
        bare_models = [current_model] if current_model else []
        if probe_all or probe_current:
            try:
                from hermes_cli.models import fetch_api_models

                live_models = fetch_api_models("", current_base_url.rstrip("/"))
                if live_models:
                    bare_models = live_models
            except Exception:
                pass
        rows.append(
            {
                "slug": "custom",
                "name": "Custom endpoint",
                "is_current": True,
                "is_user_defined": True,
                "models": bare_models,
                "total_models": len(bare_models),
                "source": "model-config",
                "api_url": current_base_url.rstrip("/"),
            }
        )
        seen_slugs.add("custom")
    emitted_custom_slugs: set[str] = set()
    for group in groups.values():
        slug = group["slug"]
        if slug.lower() in seen_slugs and slug.lower() not in emitted_custom_slugs:
            continue
        if slug.lower() in emitted_custom_slugs:
            base_slug = slug
            suffix = 2
            while f"{base_slug}-{suffix}".lower() in seen_slugs:
                suffix += 1
            slug = f"{base_slug}-{suffix}"

        url_norm = str(group["api_url"]).strip().rstrip("/").lower()
        pair = (str(group["name"]).strip().lower(), url_norm)
        if pair in emitted_provider_pairs or url_norm in builtin_endpoints:
            continue
        is_current = slug.lower() == current_provider_norm or (
            current_provider_norm == "custom"
            and bool(current_url_norm)
            and url_norm == current_url_norm
            and matching_current_urls == 1
        )
        should_probe = (
            (probe_all or (probe_current and is_current))
            and bool(group["api_url"])
            and (bool(group["api_key"]) or not group["has_explicit_models"])
            and group["discover_models"]
        )
        if should_probe:
            try:
                from hermes_cli.models import fetch_api_models

                probe_kwargs = (
                    {"api_mode": group["api_mode"]} if group["api_mode"] else {}
                )
                if group["extra_headers"]:
                    probe_kwargs["headers"] = group["extra_headers"]
                live_models = fetch_api_models(
                    group["api_key"], group["api_url"], **probe_kwargs
                )
                if live_models:
                    group["models"] = live_models
                    save_discovered_models_to_config(group["api_url"], live_models)
            except Exception:
                pass
        rows.append(
            {
                "slug": slug,
                "name": group["name"],
                "is_current": is_current,
                "is_user_defined": True,
                "models": group["models"],
                "total_models": len(group["models"]),
                "source": "user-config",
                "api_url": group["api_url"],
            }
        )
        seen_slugs.add(slug.lower())
        emitted_custom_slugs.add(slug.lower())
    return rows
