"""Run-scoped inference route policy for auxiliary LLM closure."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Mapping

_ROUTE_RESOLUTION: ContextVar[dict[str, Any] | None] = ContextVar(
    "hermes_route_resolution",
    default=None,
)
_MAIN_RUNTIME: ContextVar[dict[str, Any] | None] = ContextVar(
    "hermes_route_main_runtime",
    default=None,
)


def push_inference_route(
    route_resolution: Mapping[str, Any] | None,
    main_runtime: Mapping[str, Any] | None,
) -> tuple[Token, Token]:
    route = dict(route_resolution) if isinstance(route_resolution, Mapping) else None
    runtime = dict(main_runtime) if isinstance(main_runtime, Mapping) else None
    return _ROUTE_RESOLUTION.set(route), _MAIN_RUNTIME.set(runtime)


def reset_inference_route(tokens: tuple[Token, Token]) -> None:
    route_token, runtime_token = tokens
    _MAIN_RUNTIME.reset(runtime_token)
    _ROUTE_RESOLUTION.reset(route_token)


def current_route_resolution() -> dict[str, Any] | None:
    value = _ROUTE_RESOLUTION.get()
    return dict(value) if isinstance(value, dict) else None


def current_route_main_runtime() -> dict[str, Any] | None:
    value = _MAIN_RUNTIME.get()
    return dict(value) if isinstance(value, dict) else None


def direct_only_route_active() -> bool:
    resolution = _ROUTE_RESOLUTION.get()
    if not isinstance(resolution, dict):
        return False
    route = resolution.get("route")
    return (
        isinstance(route, dict)
        and str(route.get("cloud_usage_policy") or "") == "direct_only"
    )


__all__ = [
    "current_route_main_runtime",
    "current_route_resolution",
    "direct_only_route_active",
    "push_inference_route",
    "reset_inference_route",
]
