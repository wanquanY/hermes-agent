"""TLS verification policy shared by all HTTP provider clients."""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

_CA_ENV_NAMES = (
    "HERMES_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def _coerce_insecure(ssl_verify: Any) -> bool:
    if ssl_verify is False:
        return True
    return isinstance(ssl_verify, str) and ssl_verify.strip().lower() in {
        "false",
        "0",
        "no",
        "off",
    }


def resolve_httpx_verify(
    *,
    ca_bundle: Optional[str] = None,
    ssl_verify: Any = None,
    base_url: str = "",
) -> bool | ssl.SSLContext:
    """Resolve an httpx ``verify`` value with explicit configuration priority."""
    if _coerce_insecure(ssl_verify):
        logger.warning(
            "TLS certificate verification DISABLED for %s; use only on a fully controlled network",
            base_url or "a custom provider endpoint",
        )
        return False

    effective_ca = (ca_bundle or "").strip()
    if not effective_ca:
        effective_ca = next(
            (os.getenv(name, "").strip() for name in _CA_ENV_NAMES if os.getenv(name, "").strip()),
            "",
        )
    if not effective_ca:
        return True
    path = Path(effective_ca).expanduser()
    if path.is_file():
        return ssl.create_default_context(cafile=str(path))
    logger.warning(
        "CA bundle path does not exist: %s; falling back to default certificates",
        effective_ca,
    )
    return True
