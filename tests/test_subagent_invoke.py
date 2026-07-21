from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.subagent_invoke import invoke_subagent
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from tools.registry import registry


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        return response


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def _agent(client: FakeClient, valid_tools: set[str] | None = None):
    return SimpleNamespace(
        client=client,
        model="test-model",
        provider="test",
        api_mode="chat_completions",
        request_overrides={},
        valid_tool_names=valid_tools or set(),
    )


def _response(content="", tool_calls=None, usage=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage
        or SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
    )


def _tool_call(name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _make_profile(root: Path, name: str, soul: str = "Target soul") -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (home / "SOUL.md").write_text(soul, encoding="utf-8")
    return home


def test_invoke_subagent_happy_path_returns_output_and_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_profile(tmp_path, "worker", "Worker soul")
    client = FakeClient([_response("done")])

    result = invoke_subagent(_agent(client), "worker", "do work")

    assert result.output == "done"
    assert result.error is None
    assert result.tool_calls == []
    assert result.usage["input_tokens"] == 3
    assert result.usage["output_tokens"] == 5
    assert result.usage["total_tokens"] == 8
    system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert system_prompt.startswith("Worker soul\n\n# Skills")


def test_invoke_subagent_target_profile_missing_raises_value_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="target profile missing"):
        invoke_subagent(_agent(FakeClient([])), "missing", "prompt")


def test_invoke_subagent_timeout_sets_error_field(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_profile(tmp_path, "slow")

    def slow_response():
        time.sleep(0.02)
        return _response("late")

    result = invoke_subagent(
        _agent(FakeClient([slow_response])),
        "slow",
        "prompt",
        timeout_s=0.001,
    )

    assert result.output == ""
    assert result.error == "timeout_after_0.001s"


def test_invoke_subagent_llm_error_captured_in_result_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_profile(tmp_path, "worker")

    result = invoke_subagent(
        _agent(FakeClient([RuntimeError("boom")])),
        "worker",
        "prompt",
    )

    assert result.output == ""
    assert result.error == "RuntimeError: boom"


def test_invoke_subagent_tools_subset_restricts_callable_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_profile(tmp_path, "worker")
    called = {"count": 0}

    def denied_handler(args, **kwargs):
        called["count"] += 1
        return json.dumps({"ok": True})

    registry.register(
        name="subagent_test_denied_tool",
        toolset="subagent-test",
        schema={
            "name": "subagent_test_denied_tool",
            "description": "Denied test tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=denied_handler,
    )
    try:
        client = FakeClient(
            [
                _response(
                    tool_calls=[
                        _tool_call("subagent_test_denied_tool", {}, "denied_call")
                    ]
                ),
                _response("finished after denial"),
            ]
        )
        result = invoke_subagent(
            _agent(client, {"subagent_test_denied_tool"}),
            "worker",
            "prompt",
            tools_subset=[],
        )
    finally:
        registry.deregister("subagent_test_denied_tool")

    assert called["count"] == 0
    assert result.output == "finished after denial"
    assert result.tool_calls[0]["name"] == "subagent_test_denied_tool"
    assert result.tool_calls[0]["error"] == "tool_not_available"
    assert client.chat.completions.calls[0].get("tools") is None


def test_nested_subagent_context_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile_a = _make_profile(tmp_path, "a", "Soul A")
    profile_b = _make_profile(tmp_path, "b", "Soul B")
    observed = {}

    client = FakeClient(
        [
            _response(tool_calls=[_tool_call("subagent_test_nested", {}, "nested")]),
            _response("inner done"),
            _response("outer done"),
        ]
    )
    caller = _agent(client, {"subagent_test_nested"})

    def nested_handler(args, **kwargs):
        observed["before"] = get_hermes_home()
        inner = invoke_subagent(caller, "b", "inner", tools_subset=[])
        observed["inner_output"] = inner.output
        observed["after"] = get_hermes_home()
        return json.dumps({"inner": inner.output})

    registry.register(
        name="subagent_test_nested",
        toolset="subagent-test",
        schema={
            "name": "subagent_test_nested",
            "description": "Nested test tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=nested_handler,
    )
    try:
        result = invoke_subagent(caller, "a", "outer")
    finally:
        registry.deregister("subagent_test_nested")

    assert result.output == "outer done"
    assert observed["inner_output"] == "inner done"
    assert observed["before"] == profile_a
    assert observed["after"] == profile_a
    outer_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert outer_prompt.startswith("Soul A\n\n# Skills")
    inner_prompt = client.chat.completions.calls[1]["messages"][0]["content"]
    assert inner_prompt.startswith("Soul B\n\n# Skills")
    assert get_hermes_home() == Path(str(tmp_path))
    assert profile_b.is_dir()


def test_invoke_subagent_does_not_pollute_caller_active_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    caller_home = _make_profile(tmp_path, "caller", "Caller soul")
    _make_profile(tmp_path, "worker", "Worker soul")
    token = set_hermes_home_override(caller_home)
    try:
        result = invoke_subagent(
            _agent(FakeClient([_response("done")])),
            "worker",
            "prompt",
        )
        assert result.output == "done"
        assert get_hermes_home() == caller_home
    finally:
        reset_hermes_home_override(token)
