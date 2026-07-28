"""Phase G — MethodRegistry contract (spec §J6, §J8)."""

from __future__ import annotations

import pytest

from hermes_agent.gateway import (
    ErrorCode,
    MethodRegistry,
    RegistryError,
    requires_permission,
)


@requires_permission("session.read", read_only=True)
def _handler_session_read(params, ctx):
    return {"ok": True, "params": params, "method": ctx.method}


@requires_permission("session.write")
def _handler_session_write(params, ctx):
    return {"ok": True}


def _untagged_handler(params, ctx):
    return None


def test_register_success():
    reg = MethodRegistry()
    entry = reg.register("session.read", _handler_session_read)
    assert entry.name == "session.read"
    assert entry.permission.name == "session.read"
    assert entry.permission.read_only is True


def test_register_rejects_duplicate():
    reg = MethodRegistry()
    reg.register("session.write", _handler_session_write)
    with pytest.raises(RegistryError, match="already registered"):
        reg.register("session.write", _handler_session_write)


def test_register_rejects_untagged_handler():
    """spec §J8 — every handler must carry @requires_permission."""
    reg = MethodRegistry()
    with pytest.raises(RegistryError, match="missing @requires_permission"):
        reg.register("session.orphan", _untagged_handler)


def test_registry_names_sorted():
    reg = MethodRegistry()
    reg.register("session.write", _handler_session_write)
    reg.register("session.read", _handler_session_read)
    assert reg.names() == ["session.read", "session.write"]


def test_validate_passes_when_all_tagged():
    reg = MethodRegistry()
    reg.register("session.read", _handler_session_read)
    reg.register("session.write", _handler_session_write)
    reg.validate()  # no raise


def test_contains_and_len():
    reg = MethodRegistry()
    reg.register("session.read", _handler_session_read)
    assert "session.read" in reg
    assert "session.missing" not in reg
    assert len(reg) == 1
