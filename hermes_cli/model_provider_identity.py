"""Stable identities for platform-owned model providers."""

from __future__ import annotations

from typing import Any

DOVIE_CLOUD_CONNECTION_ID = "cloud:dovie"
DOVIE_CLOUD_PROVIDER_IDS = frozenset(
    {"dovie-cloud", "dovie_cloud", "dovie"}
)


def _identity(value: Any) -> str:
    return str(value or "").strip().lower()


def is_dovie_cloud_provider(provider_id: Any) -> bool:
    """Return whether a provider identity belongs to Dovie Cloud."""

    return _identity(provider_id) in DOVIE_CLOUD_PROVIDER_IDS


def is_dovie_cloud_connection(
    connection_id: Any,
    provider_id: Any = None,
) -> bool:
    """Return whether a connection identity is platform-owned Dovie Cloud."""

    return (
        _identity(connection_id) == DOVIE_CLOUD_CONNECTION_ID
        or is_dovie_cloud_provider(provider_id)
    )


__all__ = [
    "DOVIE_CLOUD_CONNECTION_ID",
    "DOVIE_CLOUD_PROVIDER_IDS",
    "is_dovie_cloud_connection",
    "is_dovie_cloud_provider",
]
