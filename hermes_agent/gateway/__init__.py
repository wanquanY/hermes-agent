"""L4 Gateway layer (spec §12 Phase G) — single registry + normalization + auth.

Gateway dispatch owns a single registry, request normalization, and auth.
Legacy session-id aliases are folded at the pipeline boundary.
"""

from __future__ import annotations

from hermes_agent.gateway.auth import Permission, get_permission, is_declared, requires_permission
from hermes_agent.gateway.error_codes import ErrorCode, GatewayError, MethodError, err
from hermes_agent.gateway.handshake import HANDSHAKE_FRAME_TYPE, build_handshake_frame
from hermes_agent.gateway.pipeline import (
    AllowAllResolver,
    DispatchContext,
    PermissionResolver,
    dispatch,
    fold_response_aliases,
    normalize_params,
    to_snake_case,
)
from hermes_agent.gateway.registry import MethodRegistration, MethodRegistry, RegistryError

__all__ = [
    "AllowAllResolver",
    "DispatchContext",
    "ErrorCode",
    "GatewayError",
    "HANDSHAKE_FRAME_TYPE",
    "MethodError",
    "MethodRegistration",
    "MethodRegistry",
    "Permission",
    "PermissionResolver",
    "RegistryError",
    "build_handshake_frame",
    "dispatch",
    "err",
    "fold_response_aliases",
    "get_permission",
    "is_declared",
    "normalize_params",
    "requires_permission",
    "to_snake_case",
]
