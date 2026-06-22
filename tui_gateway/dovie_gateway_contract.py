"""Compatibility shim for the Dovie extension capability manifest."""

from __future__ import annotations

from dovie_extension.manifest import (  # noqa: F401
    CONTRACT_VERSION,
    EXTENSION_VERSION,
    REQUIRED_EVENTS,
    REQUIRED_METHODS,
    REQUIRED_RUNTIME_FEATURES,
    REQUIRED_STATE_FEATURES,
    gateway_capabilities,
)
