"""Dispatch pre-hook — snake_case normalization + auth (spec §J7, §J8).

Handlers see snake_case params only. The legacy dual-write of ``sessionId`` /
``session_id`` in ``server.py:122-155`` is retired; ``normalize_params()``
is the single conversion point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from hermes_agent.gateway.error_codes import ErrorCode, MethodError, err
from hermes_agent.gateway.registry import MethodRegistry


_CAMEL_TO_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake_case(name: str) -> str:
    """Convert ``camelCase`` / ``PascalCase`` to ``snake_case``.

    Idempotent — feeding a snake_case string returns it unchanged.
    """
    if not name:
        return name
    return _CAMEL_TO_SNAKE_RE.sub("_", name).lower()


"""Legacy session-id alias vocabulary (spec §5.1).

Every one of these becomes ``session_id`` at the dispatch boundary. Handlers
never see the alias; ``SessionRepo`` never sees the alias. Frontend code is
allowed to keep using the alias in transit — dispatch strips it here.
"""
_SESSION_ID_ALIASES = (
    "conversation_session_id",
    "stored_session_id",
    "stable_session_id",
    "runtime_session_id",
)


def _fold_session_id_aliases(normalized: dict[str, Any]) -> None:
    """In-place collapse of any legacy alias to ``session_id`` (spec §5.1).

    Precedence: an explicit ``session_id`` in the request wins over any
    alias (the caller was already up-to-date). Otherwise the first non-empty
    alias in the vocabulary order is promoted. All aliases are removed from
    the dict on the way out so downstream handlers cannot inadvertently key
    off the legacy name.
    """
    existing = str(normalized.get("session_id") or "").strip()
    if not existing:
        for alias in _SESSION_ID_ALIASES:
            raw = normalized.get(alias)
            if raw is None:
                continue
            candidate = str(raw).strip()
            if candidate:
                normalized["session_id"] = candidate
                break
    for alias in _SESSION_ID_ALIASES:
        normalized.pop(alias, None)


def fold_response_aliases(result: Any) -> Any:
    """Recursively strip legacy ``session_id`` aliases from a handler's
    return value so the wire response only carries the canonical name
    (spec §5.1 — dual retirement channel symmetric to ``normalize_params``).

    Rules (mirror of ``_fold_session_id_aliases`` on the request side):
    * If ``session_id`` is already present, drop every alias.
    * Otherwise, if any alias carries a non-empty value, promote the first
      hit (in vocabulary order) to ``session_id`` and drop all aliases.
    * Nested dicts and lists are traversed.
    * Non-dict / non-list values pass through untouched.
    """
    if isinstance(result, dict):
        _fold_session_id_aliases(result)
        for key, value in list(result.items()):
            result[key] = fold_response_aliases(value)
        return result
    if isinstance(result, list):
        return [fold_response_aliases(item) for item in result]
    if isinstance(result, tuple):
        return tuple(fold_response_aliases(item) for item in result)
    return result


def normalize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Recursively snake_case-normalize dict keys (values untouched).

    Lists / nested dicts are traversed. Non-dict values pass through. After
    snake_case-normalization, legacy session_id aliases (spec §5.1) are
    folded into the canonical ``session_id`` at every dict level so handlers
    always see the single identifier.
    """
    if not isinstance(params, dict):
        return {} if params is None else params
    normalized: dict[str, Any] = {}
    for key, value in params.items():
        snake_key = to_snake_case(str(key))
        if isinstance(value, dict):
            normalized[snake_key] = normalize_params(value)
        elif isinstance(value, list):
            normalized[snake_key] = [
                normalize_params(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            normalized[snake_key] = value
    _fold_session_id_aliases(normalized)
    return normalized


@dataclass(frozen=True)
class DispatchContext:
    """Request context passed to the handler after auth."""

    request_id: str
    method: str
    caller_scope: str = ""


class PermissionResolver:
    """Callback plugin — resolves whether the caller may invoke the method."""

    def is_allowed(
        self,
        ctx: DispatchContext,
        permission_name: str,
        *,
        read_only: bool,
    ) -> bool:
        raise NotImplementedError


class AllowAllResolver(PermissionResolver):
    """Development / test resolver — grants everything. Do NOT use in prod."""

    def is_allowed(self, ctx, permission_name, *, read_only):  # noqa: D401
        return True


def dispatch(
    registry: MethodRegistry,
    frame: dict[str, Any],
    *,
    resolver: PermissionResolver,
) -> dict[str, Any]:
    """Run the pre-hook chain and invoke the handler.

    Order (spec §12 Phase G):
        1. Normalize params (snake_case)
        2. Look up method
        3. Auth check
        4. Handler call
        5. Error envelope on exception

    Handler contract: ``handler(params: dict, ctx: DispatchContext) -> dict``
    """
    request_id = str(frame.get("id") or "")
    method_name = str(frame.get("method") or "")
    raw_params = frame.get("params") or {}
    params = normalize_params(raw_params if isinstance(raw_params, dict) else {})

    if not method_name:
        return err(request_id, ErrorCode.MALFORMED_FRAME, "method is required")

    entry = registry.get(method_name)
    if entry is None:
        return err(request_id, ErrorCode.UNKNOWN_METHOD, f"unknown method {method_name!r}")

    ctx = DispatchContext(
        request_id=request_id,
        method=entry.name,
        caller_scope=str(frame.get("caller_scope") or ""),
    )

    if not resolver.is_allowed(
        ctx,
        entry.permission.name,
        read_only=entry.permission.read_only,
    ):
        return err(
            request_id,
            ErrorCode.PERMISSION_DENIED,
            f"caller lacks permission {entry.permission.name!r}",
            method=entry.name,
        )

    try:
        result = entry.handler(params, ctx)
    except MethodError as exc:
        # Handler classified the failure explicitly (spec §J9 ErrorCode).
        return err(
            request_id,
            exc.code,
            exc.message,
            method=entry.name,
            **(exc.details or {}),
        )
    except TypeError as exc:
        return err(request_id, ErrorCode.INVALID_PARAMS, str(exc), method=entry.name)
    except Exception as exc:  # spec §J11 — bubble up, but log via caller
        return err(
            request_id,
            ErrorCode.UPSTREAM_FAILURE,
            f"{type(exc).__name__}: {exc}",
            method=entry.name,
        )

    # Symmetric alias fold on the response side: whatever the legacy
    # handler happens to emit, the wire result exposes ``session_id`` only.
    return {"id": request_id, "result": fold_response_aliases(result)}


_CALLABLE_HANDLER: Callable[..., Any]  # for future type hints
