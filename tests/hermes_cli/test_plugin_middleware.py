"""Behavioral contracts for plugin request and execution middleware."""
from __future__ import annotations

from types import SimpleNamespace

from agent.tool_middleware_runtime import invoke_agent_tool
from hermes_cli.middleware import (
    apply_llm_request_middleware,
    apply_tool_request_middleware,
    run_llm_execution_middleware,
    run_tool_execution_middleware,
)


class _Manager:
    def __init__(self, middleware):
        self._middleware = middleware

    def has_middleware(self, kind):
        return bool(self._middleware.get(kind))

    def invoke_middleware(self, kind, **kwargs):
        return [callback(**kwargs) for callback in self._middleware.get(kind, [])]


def test_request_middleware_rewrites_in_registration_order(monkeypatch):
    def first(**kwargs):
        return {"args": {**kwargs["args"], "first": True}, "source": "first"}

    def second(**kwargs):
        assert kwargs["args"]["first"] is True
        return {"args": {**kwargs["args"], "second": True}, "source": "second"}

    manager = _Manager({"tool_request": [first, second]})
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)

    original = {"path": "README.md"}
    result = apply_tool_request_middleware("read_file", original)

    assert result.original_payload == original
    assert result.payload == {"path": "README.md", "first": True, "second": True}
    assert result.trace == [{"source": "first"}, {"source": "second"}]


def test_execution_middleware_wraps_terminal_once(monkeypatch):
    events = []

    def outer(**kwargs):
        events.append("outer-before")
        result = kwargs["next_call"]({**kwargs["args"], "outer": True})
        events.append("outer-after")
        return result

    def inner(**kwargs):
        events.append("inner-before")
        result = kwargs["next_call"]({**kwargs["args"], "inner": True})
        events.append("inner-after")
        return result

    manager = _Manager({"tool_execution": [outer, inner]})
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)

    calls = []
    result = run_tool_execution_middleware(
        "terminal",
        {"command": "pwd"},
        lambda args: calls.append(args) or args,
    )

    assert result == {"command": "pwd", "outer": True, "inner": True}
    assert calls == [result]
    assert events == ["outer-before", "inner-before", "inner-after", "outer-after"]


def test_agent_owned_tool_uses_all_middleware_layers(monkeypatch):
    observed = []

    def request(**kwargs):
        observed.append(("request", kwargs["args"]))
        return {"args": {**kwargs["args"], "request": True}, "source": "request"}

    def execution(**kwargs):
        observed.append(("execution", kwargs["args"]))
        return kwargs["next_call"]({**kwargs["args"], "execution": True})

    manager = _Manager(
        {"tool_request": [request], "tool_execution": [execution]}
    )
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    monkeypatch.setattr("hermes_cli.plugins.resolve_pre_tool_block", lambda *a, **k: None)

    agent = SimpleNamespace(
        session_id="session-1",
        _current_turn_id="turn-1",
        _current_api_request_id="api-1",
        _context_engine_tool_names=set(),
        _memory_manager=None,
        valid_tool_names={"demo"},
        enabled_toolsets=None,
        disabled_toolsets=None,
    )

    def dispatch(name, args, task_id, **kwargs):
        observed.append(("dispatch", args))
        return "ok"

    monkeypatch.setattr("run_agent.handle_function_call", dispatch)

    result = invoke_agent_tool(agent, "demo", {"value": 1}, "task-1")

    assert result == "ok"
    assert observed == [
        ("request", {"value": 1}),
        ("execution", {"value": 1, "request": True}),
        ("dispatch", {"value": 1, "request": True, "execution": True}),
    ]


def test_llm_request_and_execution_share_one_composable_contract(monkeypatch):
    def request(**kwargs):
        return {
            "request": {**kwargs["request"], "request_layer": True},
            "source": "request",
        }

    def execution(**kwargs):
        return kwargs["next_call"](
            {**kwargs["request"], "execution_layer": True}
        )

    manager = _Manager(
        {"llm_request": [request], "llm_execution": [execution]}
    )
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)

    request_result = apply_llm_request_middleware({"model": "test"})
    sent = []
    response = run_llm_execution_middleware(
        request_result.payload,
        lambda payload: sent.append(payload) or "response",
        original_request=request_result.original_payload,
    )

    assert response == "response"
    assert sent == [
        {"model": "test", "request_layer": True, "execution_layer": True}
    ]
