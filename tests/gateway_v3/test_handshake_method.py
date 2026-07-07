"""Phase H — ``system.handshake`` gateway method."""

from __future__ import annotations

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods.handshake_method import (
    HANDSHAKE_METHOD_NAME,
    method_system_handshake,
    register,
)


def test_handshake_method_name_is_stable():
    assert HANDSHAKE_METHOD_NAME == "system.handshake"


def test_handshake_method_declares_permission():
    """spec §J8 — every method needs @requires_permission (system.handshake read_only)."""
    from hermes_agent.gateway.auth import get_permission

    perm = get_permission(method_system_handshake)
    assert perm is not None
    assert perm.name == "system.handshake"
    assert perm.read_only is True


def test_register_wires_handshake_into_registry():
    reg = MethodRegistry()
    register(reg)
    assert HANDSHAKE_METHOD_NAME in reg


def test_dispatch_handshake_returns_frame_shape():
    reg = MethodRegistry()
    register(reg)
    resp = dispatch(
        reg,
        {"id": "req-1", "method": HANDSHAKE_METHOD_NAME, "params": {}},
        resolver=AllowAllResolver(),
    )
    assert resp["id"] == "req-1"
    result = resp["result"]
    assert result["type"] == "handshake"
    assert result["contractVersion"] == "3.1"
    assert set(result["capabilities"].keys()) == {"cursor", "history", "toolEvents"}
    assert result["deprecations"] == []


def test_dispatch_handshake_ignores_extra_params():
    reg = MethodRegistry()
    register(reg)
    resp = dispatch(
        reg,
        {
            "id": "req-2",
            "method": HANDSHAKE_METHOD_NAME,
            "params": {"noise": "ignored", "storedSessionId": "s1"},
        },
        resolver=AllowAllResolver(),
    )
    assert "result" in resp
    assert resp["result"]["contractVersion"] == "3.1"


def test_dispatch_denied_handshake_returns_4003():
    """Even system.handshake goes through the auth gate — no ambient exemption."""

    class _DenyAll:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return False

    reg = MethodRegistry()
    register(reg)
    resp = dispatch(
        reg,
        {"id": "req-3", "method": HANDSHAKE_METHOD_NAME},
        resolver=_DenyAll(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value
