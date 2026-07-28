from __future__ import annotations

import json
from types import SimpleNamespace

import tools.subagent as subagent_tool
from agent.subagent_invoke import SubagentResult
from tools.registry import registry


def test_invoke_subagent_tool_registered_in_registry():
    entry = registry.get_entry("invoke_subagent")

    assert entry is not None
    assert entry.toolset == "subagent"
    schema = registry.get_schema("invoke_subagent")
    assert schema["parameters"]["required"] == ["target_profile_id", "prompt"]


def test_invoke_subagent_tool_handler_serializes_result_to_dict(monkeypatch):
    calls = {}

    def fake_invoke(caller_agent, target_profile_id, prompt, *, files=None, tools_subset=None):
        calls["caller_agent"] = caller_agent
        calls["target_profile_id"] = target_profile_id
        calls["prompt"] = prompt
        calls["files"] = files
        calls["tools_subset"] = tools_subset
        return SubagentResult(
            output="ok",
            tool_calls=[{"name": "read_file"}],
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            elapsed_s=0.25,
        )

    monkeypatch.setattr(subagent_tool, "invoke_subagent", fake_invoke)
    parent = SimpleNamespace(name="parent")

    payload = json.loads(
        registry.dispatch(
            "invoke_subagent",
            {
                "target_profile_id": "worker",
                "prompt": "do it",
                "files": ["a.py"],
                "tools_subset": ["read_file"],
            },
            parent_agent=parent,
        )
    )

    assert calls == {
        "caller_agent": parent,
        "target_profile_id": "worker",
        "prompt": "do it",
        "files": ["a.py"],
        "tools_subset": ["read_file"],
    }
    assert payload == {
        "output": "ok",
        "tool_calls": [{"name": "read_file"}],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        "error": None,
        "elapsed_s": 0.25,
    }
