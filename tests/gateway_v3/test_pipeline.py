"""Phase G — dispatch pipeline (snake_case + auth + error envelope)."""

from __future__ import annotations

import pytest

from hermes_agent.gateway import (
    AllowAllResolver,
    DispatchContext,
    ErrorCode,
    MethodRegistry,
    PermissionResolver,
    dispatch,
    normalize_params,
    requires_permission,
    to_snake_case,
)


def test_to_snake_case_basic():
    assert to_snake_case("sessionId") == "session_id"
    assert to_snake_case("conversationSessionId") == "conversation_session_id"
    assert to_snake_case("runId") == "run_id"


def test_to_snake_case_idempotent():
    assert to_snake_case("session_id") == "session_id"


def test_to_snake_case_pascalcase():
    assert to_snake_case("PascalCase") == "pascal_case"


def test_normalize_params_recursive():
    got = normalize_params({
        "sessionId": "s1",
        "runInfo": {
            "runId": "r1",
            "turnId": "t1",
        },
        "history": [
            {"messageId": "m1"},
            {"messageId": "m2"},
        ],
    })
    assert got == {
        "session_id": "s1",
        "run_info": {"run_id": "r1", "turn_id": "t1"},
        "history": [{"message_id": "m1"}, {"message_id": "m2"}],
    }


def test_normalize_params_handles_none():
    assert normalize_params(None) == {}


@requires_permission("echo.read", read_only=True)
def _echo_handler(params, ctx):
    return {"echoed": params, "method": ctx.method}


def test_dispatch_normalizes_and_calls_handler():
    reg = MethodRegistry()
    reg.register("echo", _echo_handler)

    frame = {
        "id": "req-1",
        "method": "echo",
        "params": {"sessionId": "s1", "runId": "r1"},
    }
    resp = dispatch(reg, frame, resolver=AllowAllResolver())

    assert resp["id"] == "req-1"
    assert "result" in resp
    assert resp["result"]["echoed"] == {"session_id": "s1", "run_id": "r1"}
    assert resp["result"]["method"] == "echo"


def test_dispatch_unknown_method_returns_4001():
    reg = MethodRegistry()
    resp = dispatch(
        reg,
        {"id": "req-2", "method": "nope"},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.UNKNOWN_METHOD.value


def test_dispatch_missing_method_name():
    reg = MethodRegistry()
    resp = dispatch(reg, {"id": "req-3"}, resolver=AllowAllResolver())
    assert resp["error"]["code"] == ErrorCode.MALFORMED_FRAME.value


class _DenyAll(PermissionResolver):
    def is_allowed(self, ctx, permission_name, *, read_only):
        return False


def test_dispatch_denied_permission_returns_4003():
    reg = MethodRegistry()
    reg.register("echo", _echo_handler)
    resp = dispatch(
        reg,
        {"id": "req-4", "method": "echo", "params": {}},
        resolver=_DenyAll(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


@requires_permission("boom.write")
def _boom_handler(params, ctx):
    raise RuntimeError("kaboom")


def test_dispatch_handler_exception_returns_5007():
    reg = MethodRegistry()
    reg.register("boom", _boom_handler)
    resp = dispatch(
        reg,
        {"id": "req-5", "method": "boom", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.UPSTREAM_FAILURE.value
    assert "kaboom" in resp["error"]["message"]
