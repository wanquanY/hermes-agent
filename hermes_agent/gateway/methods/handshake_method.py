"""``system.handshake`` gateway method (spec §11 Phase H).

Push-model handshake (server sends the frame on connect) is the ultimate
target — until the transport layer refactor lands, we expose a pull-model
method so the frontend can request the payload deterministically:

    request:  {"id": "req-1", "method": "system.handshake"}
    response: {"id": "req-1", "result": {
        "type": "handshake",
        "contractVersion": "3.1",
        "capabilities": {...},
        "deprecations": []
    }}
"""

from __future__ import annotations

from typing import Any

from hermes_agent.gateway.auth import requires_permission
from hermes_agent.gateway.handshake import build_handshake_frame
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.gateway.registry import MethodRegistry


HANDSHAKE_METHOD_NAME = "system.handshake"


@requires_permission("system.handshake", read_only=True)
def method_system_handshake(params: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    """Return the current handshake frame — inputs are ignored, output is
    always the canonical capabilities snapshot.
    """
    return build_handshake_frame()


def register(registry: MethodRegistry) -> None:
    """Register ``system.handshake`` on the given registry."""
    registry.register(HANDSHAKE_METHOD_NAME, method_system_handshake)


__all__ = [
    "HANDSHAKE_METHOD_NAME",
    "method_system_handshake",
    "register",
]
