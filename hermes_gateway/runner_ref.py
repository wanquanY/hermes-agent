"""Process-local reference to the active gateway runner."""

from __future__ import annotations

import weakref
from typing import Any

_gateway_runner_ref: weakref.ReferenceType[Any] | None = None


def set_gateway_runner(runner: Any) -> None:
    """Register the active in-process gateway runner."""

    global _gateway_runner_ref
    _gateway_runner_ref = weakref.ref(runner)


def gateway_runner_ref() -> Any | None:
    """Return the active gateway runner, if this process owns one."""

    if _gateway_runner_ref is None:
        return None
    return _gateway_runner_ref()
