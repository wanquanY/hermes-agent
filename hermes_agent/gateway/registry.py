"""Method registry (spec §J6) — single source of truth for gateway methods.

Replaces the legacy dual mechanism (``tui_gateway.core.method_registration
::METHOD_MODULES`` + ``dovie_extension.gateway_methods
::DOVIE_GATEWAY_METHOD_OVERRIDES``). Registration is the ONLY way to expose a
handler; runtime "hot override" paths are banned.

Startup validation (``validate()``) fails fast if any registered handler is
missing a ``@requires_permission`` tag — spec §J8.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from hermes_agent.gateway.auth import Permission, get_permission


@dataclass(frozen=True)
class MethodRegistration:
    name: str
    handler: Callable[..., Any]
    permission: Permission


class RegistryError(RuntimeError):
    """Registration or validation failure."""


class MethodRegistry:
    """Single-registry method table.

    ``register(name, fn)`` fails if ``name`` is already claimed or if ``fn``
    is not permission-tagged. ``validate()`` at process start-up sweeps every
    handler and fails on any drift.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, MethodRegistration] = {}

    def register(self, name: str, handler: Callable[..., Any]) -> MethodRegistration:
        normalized = _normalize_method_name(name)
        if not normalized:
            raise RegistryError("method name is required")
        if normalized in self._by_name:
            existing = self._by_name[normalized]
            raise RegistryError(
                f"method {normalized!r} already registered by "
                f"{existing.handler.__qualname__}"
            )
        perm = get_permission(handler)
        if perm is None:
            raise RegistryError(
                f"method {normalized!r} handler {handler.__qualname__} is "
                "missing @requires_permission — spec §J8 forbids ambient authority"
            )
        entry = MethodRegistration(name=normalized, handler=handler, permission=perm)
        self._by_name[normalized] = entry
        return entry

    def get(self, name: str) -> MethodRegistration | None:
        return self._by_name.get(_normalize_method_name(name))

    def names(self) -> list[str]:
        return sorted(self._by_name.keys())

    def entries(self) -> Iterable[MethodRegistration]:
        return list(self._by_name.values())

    def validate(self) -> None:
        """spec §J8 — startup sweep. Fails on any undeclared handler."""
        for entry in self._by_name.values():
            if get_permission(entry.handler) is None:
                raise RegistryError(
                    f"method {entry.name!r} lost its permission tag between "
                    "registration and startup validation"
                )

    def __contains__(self, name: str) -> bool:
        return _normalize_method_name(name) in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


def _normalize_method_name(name: str) -> str:
    return str(name or "").strip()
