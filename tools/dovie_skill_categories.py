"""Dovie skill category catalog helpers.

Dovie owns the product taxonomy for skill categories. Hermes still owns local
skill files, so Dovie runtimes expose the enabled category catalog through a
runtime-token protected endpoint. This module keeps that integration in one
place for skill creation and profile design tools.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any


_CACHE_TTL_SECONDS = 60
_cache_expires_at = 0.0
_cache: list[dict[str, Any]] | None = None


def dovie_runtime_mode_enabled() -> bool:
    return bool(
        str(os.getenv("DOVIE_HERMES_RUNTIME_MODE") or "").strip()
        or str(os.getenv("DOVIE_SKILL_CATEGORIES_URL") or "").strip()
        or str(os.getenv("DOVIE_API_ORIGIN") or "").strip()
    )


def _runtime_token() -> str:
    return str(os.getenv("DOVIE_LLM_RUNTIME_TOKEN") or "").strip()


def _categories_url() -> str:
    explicit = str(os.getenv("DOVIE_SKILL_CATEGORIES_URL") or "").strip()
    if explicit:
        return explicit
    origin = str(os.getenv("DOVIE_API_ORIGIN") or "").strip().rstrip("/")
    return f"{origin}/api/v1/llm-proxy/v1/skill-market/categories" if origin else ""


def _normalize_category(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("slug") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not slug:
        return None
    return {
        "id": str(raw.get("id") or "").strip(),
        "slug": slug,
        "name": name or slug,
        "description": str(raw.get("description") or "").strip(),
        "sort_order": int(raw.get("sort_order") or raw.get("sortOrder") or 0),
        "enabled": bool(raw.get("enabled", True)),
    }


def _extract_categories(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    categories = []
    for item in data:
        normalized = _normalize_category(item)
        if normalized and normalized["enabled"]:
            categories.append(normalized)
    return sorted(categories, key=lambda item: (item["sort_order"], item["name"], item["slug"]))


def _fetch_categories() -> list[dict[str, Any]]:
    url = _categories_url()
    token = _runtime_token()
    if not url:
        raise RuntimeError("DOVIE_SKILL_CATEGORIES_URL is not configured.")
    if not token:
        raise RuntimeError("DOVIE_LLM_RUNTIME_TOKEN is not configured.")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    def _send() -> tuple[bytes, int]:
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.read(), getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            return exc.read(), exc.code

    try:
        from tools.interrupt import run_blocking_interruptibly

        raw, status = run_blocking_interruptibly(
            _send,
            interrupted_message="Dovie skill category request interrupted.",
        )
    except TimeoutError as exc:
        raise RuntimeError("Dovie skill category request timed out.") from exc
    except socket.timeout as exc:
        raise RuntimeError("Dovie skill category request timed out.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dovie skill category request failed: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:
        raise RuntimeError(f"Dovie skill category response is not JSON: HTTP {status}") from exc
    if status < 200 or status >= 300:
        message = payload.get("message") if isinstance(payload, dict) else ""
        detail = payload.get("detail") if isinstance(payload, dict) else ""
        raise RuntimeError(str(message or detail or f"HTTP {status}"))
    return _extract_categories(payload)


def list_dovie_skill_categories(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    global _cache, _cache_expires_at
    now = time.time()
    if not force_refresh and _cache is not None and now < _cache_expires_at:
        return list(_cache)
    categories = _fetch_categories()
    _cache = categories
    _cache_expires_at = now + _CACHE_TTL_SECONDS
    return list(categories)


def validate_dovie_skill_category(category: str | None) -> str | None:
    if not dovie_runtime_mode_enabled():
        return None
    normalized = str(category or "").strip()
    try:
        categories = list_dovie_skill_categories()
    except Exception as exc:
        return f"Dovie skill categories are unavailable: {exc}"

    allowed_slugs = [item["slug"] for item in categories]
    if not allowed_slugs:
        return "Dovie skill categories are empty. Create an enabled skill category in Admin first."
    if not normalized:
        return "category is required in Dovie runtimes. Choose one of: " + ", ".join(allowed_slugs)
    if normalized not in set(allowed_slugs):
        return f"Invalid Dovie skill category '{normalized}'. Choose one of: " + ", ".join(allowed_slugs)
    return None
