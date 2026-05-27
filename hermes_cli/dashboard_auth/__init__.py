"""Minimal dashboard auth provider registry.

This selective sync keeps the dashboard auth gate decision and fail-closed
behavior without importing the full upstream OAuth route stack. Providers can
register here so `web_server.start_server()` knows whether a gated public bind
has a real authentication backend available.
"""

from __future__ import annotations

from threading import RLock
from typing import Any, List

_providers: list[Any] = []
_lock = RLock()


def register_provider(provider: Any) -> None:
    """Register a dashboard auth provider object."""
    with _lock:
        name = getattr(provider, "name", None)
        if name is not None:
            for idx, existing in enumerate(_providers):
                if getattr(existing, "name", None) == name:
                    _providers[idx] = provider
                    return
        _providers.append(provider)


def list_providers() -> List[Any]:
    """Return registered dashboard auth providers."""
    with _lock:
        return list(_providers)


def clear_providers() -> None:
    """Clear provider registry; intended for tests."""
    with _lock:
        _providers.clear()
