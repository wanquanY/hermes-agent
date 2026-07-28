"""Permission gate + ``@requires_permission`` decorator (spec §J8).

Every gateway method must declare a permission tag. Undeclared methods fail
the startup registry validation — no ambient authority allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar


_PERMISSION_ATTR = "__hermes_gateway_permission__"


F = TypeVar("F", bound=Callable[..., object])


@dataclass(frozen=True)
class Permission:
    """A permission tag; may be a compound (``session.read``, ``run.write``).

    ``read_only=True`` unlocks legacy DB read-only fastpath (spec — old
    ``_READ_ONLY_DB_METHODS`` frozenset is being retired).
    """

    name: str
    read_only: bool = False


def requires_permission(permission: str | Permission, *, read_only: bool = False) -> Callable[[F], F]:
    """Attach a permission tag to a gateway handler.

    Usage:

        @requires_permission("session.read", read_only=True)
        def method_session_get(params, ctx): ...
    """

    if isinstance(permission, Permission):
        tag = permission
    else:
        tag = Permission(name=str(permission), read_only=bool(read_only))

    def _wrap(fn: F) -> F:
        setattr(fn, _PERMISSION_ATTR, tag)
        return fn

    return _wrap


def get_permission(fn: Callable[..., object]) -> Permission | None:
    """Return the declared permission or ``None`` if the fn is undeclared."""
    return getattr(fn, _PERMISSION_ATTR, None)


def is_declared(fn: Callable[..., object]) -> bool:
    return get_permission(fn) is not None
