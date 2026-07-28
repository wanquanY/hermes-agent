"""Regression tests for task/session cwd propagation in terminal_tool."""

import json

import tools.terminal_tool as terminal_tool
import tools.file_tools as file_tools
from tools.terminal_cwd_registry import TerminalCwdRegistry


def _minimal_terminal_config(cwd="/default"):
    return {
        "env_type": "local",
        "cwd": cwd,
        "timeout": 60,
    }


def test_foreground_command_uses_registered_task_cwd_for_existing_environment(monkeypatch):
    """ACP can update task cwd after the local env exists; foreground must honor it."""
    calls = []

    class FakeEnv:
        env = {}

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "ok", "returncode": 0}

    task_id = "acp-session-1"
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {task_id: {"cwd": "/workspace/acp"}})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _minimal_terminal_config())
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    result = json.loads(terminal_tool.terminal_tool(command="pwd", task_id=task_id))

    assert result["exit_code"] == 0
    assert calls == [("pwd", {"timeout": 60, "cwd": "/workspace/acp"})]


def test_explicit_workdir_still_wins_over_registered_task_cwd(monkeypatch):
    calls = []

    class FakeEnv:
        env = {}

        def execute(self, command, **kwargs):
            calls.append(kwargs)
            return {"output": "ok", "returncode": 0}

    task_id = "acp-session-1"
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {task_id: {"cwd": "/workspace/acp"}})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _minimal_terminal_config())
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    result = json.loads(
        terminal_tool.terminal_tool(
            command="pwd",
            task_id=task_id,
            workdir="/explicit/workdir",
        )
    )

    assert result["exit_code"] == 0
    assert calls == [{"timeout": 60, "cwd": "/explicit/workdir"}]


def test_terminal_cwd_registry_keeps_sessions_independent():
    registry = TerminalCwdRegistry()
    registry.record("default", "session-a", "/workspace/a/deep")
    registry.record("default", "session-b", "/workspace/b/deep")

    assert registry.resolve(
        environment_key="default",
        session_key="session-a",
        default_cwd="/workspace/a",
    ) == "/workspace/a/deep"
    assert registry.resolve(
        environment_key="default",
        session_key="session-b",
        default_cwd="/workspace/b",
    ) == "/workspace/b/deep"


def test_stale_shared_environment_cwd_is_not_another_sessions_default(monkeypatch):
    calls = []

    class FakeEnv:
        env = {}
        cwd = "/workspace/session-a/deep"

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            self.cwd = kwargs["cwd"]
            return {"output": "ok", "returncode": 0}

    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: _minimal_terminal_config(cwd="/workspace/session-b"),
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    result = json.loads(terminal_tool.terminal_tool(command="pwd", task_id="session-b"))

    assert result["exit_code"] == 0
    assert calls == [("pwd", {"timeout": 60, "cwd": "/workspace/session-b"})]


def test_foreground_command_records_session_local_cd(monkeypatch):
    class FakeEnv:
        env = {}
        cwd = "/workspace/root"

        def execute(self, command, **kwargs):
            self.cwd = "/workspace/root/subdir"
            return {"output": "ok", "returncode": 0}

    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: _minimal_terminal_config(cwd="/workspace/root"),
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    assert json.loads(
        terminal_tool.terminal_tool(command="cd subdir", task_id="session-local")
    )["exit_code"] == 0

    calls = []
    terminal_tool._active_environments["default"].execute = lambda command, **kwargs: (
        calls.append(kwargs) or {"output": "ok", "returncode": 0}
    )
    terminal_tool.terminal_tool(command="pwd", task_id="session-local")
    assert calls == [{"timeout": 60, "cwd": "/workspace/root/subdir"}]


def test_file_tools_do_not_inherit_another_gateway_sessions_live_cwd(monkeypatch):
    stale_env = type("StaleEnv", (), {"cwd": "/workspace/session-a/deep"})()
    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": stale_env})
    monkeypatch.setattr(
        "tools.approval.get_current_session_key",
        lambda default="": "session-b",
    )
    monkeypatch.setattr(
        "tools.terminal_cwd_registry.terminal_cwd_registry.get",
        lambda _environment, _session: None,
    )

    assert file_tools._get_live_tracking_cwd("shared-task") is None
