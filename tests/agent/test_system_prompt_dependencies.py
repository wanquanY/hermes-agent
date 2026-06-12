import builtins
import sys
from types import SimpleNamespace

import pytest

from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
from agent.system_prompt import build_system_prompt_parts


def test_build_system_prompt_parts_does_not_import_run_agent(monkeypatch):
    monkeypatch.delitem(sys.modules, "run_agent", raising=False)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "run_agent":
            raise AssertionError("system prompt assembly must not import run_agent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=set(),
        _kanban_worker_guidance="",
        _tool_use_enforcement="auto",
        provider="custom",
        model="deepseek-v4-pro",
        platform="tui",
        _memory_store=None,
        _memory_enabled=False,
        _user_profile_enabled=False,
        _memory_manager=None,
        pass_session_id=False,
        session_id="test-session",
    )

    parts = build_system_prompt_parts(agent)

    assert DEFAULT_AGENT_IDENTITY in parts["stable"]
    assert "run_agent" not in sys.modules
