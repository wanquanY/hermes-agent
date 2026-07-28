"""Dovie extension entrypoint for Hermes hooks."""

from __future__ import annotations

from typing import Any

from .gateway_methods import dovie_gateway_method_overrides, register_gateway_methods
from .manifest import gateway_capabilities


class DovieHermesExtension:
    """Registers Dovie-owned Gateway ABI surface with Hermes Core."""

    id = "dovie"
    version = "2026-07-28"

    def register_gateway_methods(self, registry: dict[str, Any] | None = None) -> None:
        register_gateway_methods(registry)

    def register_capabilities(self, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        capabilities = gateway_capabilities()
        if manifest is not None:
            manifest.update(capabilities)
        return capabilities

    def gateway_method_overrides(self) -> frozenset[str]:
        return dovie_gateway_method_overrides()

    def register_tools(self) -> None:
        """Import Dovie-owned tool modules so they self-register with Hermes."""
        from . import document_parse_tool  # noqa: F401


def load_extension() -> DovieHermesExtension:
    return DovieHermesExtension()
