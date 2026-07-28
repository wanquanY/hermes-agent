"""Prevent invalid CA-bundle configuration from failing deep in provider code."""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path

from agent.errors import SSLConfigurationError


logger = logging.getLogger(__name__)

_CA_BUNDLE_ENV_VARS = (
    "HERMES_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def _repair_hint() -> str:
    return (
        "Repair: python -m pip install --force-reinstall certifi openai httpx\n"
        "If you configured a corporate CA bundle, fix or unset its environment variable."
    )


def _configuration_error(message: str) -> SSLConfigurationError:
    return SSLConfigurationError(f"{message}\n{_repair_hint()}")


def _validate_bundle_path(
    label: str,
    value: str,
    *,
    require_substantial: bool = False,
) -> None:
    path = Path(value).expanduser()
    if not path.exists():
        raise _configuration_error(f"{label} points to a missing CA bundle: {value}")
    if not path.is_file():
        raise _configuration_error(f"{label} does not point to a CA bundle file: {value}")
    if require_substantial and path.stat().st_size < 1024:
        raise _configuration_error(f"{label} at {value} appears corrupted (too small)")
    try:
        context = ssl.create_default_context(cafile=str(path))
    except Exception as exc:
        raise _configuration_error(
            f"{label} CA bundle at {value} cannot be loaded: {exc}"
        ) from exc
    if not context.get_ca_certs():
        raise _configuration_error(
            f"{label} CA bundle at {value} did not load any certificates"
        )


def verify_ca_bundle() -> None:
    """Validate explicit CA configuration and the bundled certifi trust store."""
    if os.getenv("HERMES_SKIP_SSL_GUARD", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        logger.debug("SSL CA-bundle guard explicitly disabled")
        return
    for name in _CA_BUNDLE_ENV_VARS:
        if value := os.getenv(name):
            _validate_bundle_path(name, value)
    try:
        import certifi
    except Exception as exc:
        raise _configuration_error(f"certifi is not importable: {exc}") from exc
    _validate_bundle_path("certifi", str(certifi.where()), require_substantial=True)


def verify_ca_bundle_with_fallback() -> None:
    """Compatibility name retained while enforcing the same fail-fast check."""
    verify_ca_bundle()
