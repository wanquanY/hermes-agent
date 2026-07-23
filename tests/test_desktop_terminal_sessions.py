from __future__ import annotations

from types import SimpleNamespace

import pytest

from tui_gateway import server
from tools import process_registry as process_registry_module


class FakePty:
    def __init__(self) -> None:
        self.resize_calls: list[tuple[int, int]] = []

    def setwinsize(self, rows: int, cols: int) -> None:
        self.resize_calls.append((rows, cols))


class FakeProcessRegistry:
    def __init__(self) -> None:
        self.processes: dict[str, SimpleNamespace] = {}
        self.spawn_calls: list[dict] = []
        self.write_calls: list[tuple[str, str]] = []
        self.kill_calls: list[tuple[str, str]] = []

    def spawn_local(self, command: str, **kwargs):
        self.spawn_calls.append({"command": command, **kwargs})
        process = SimpleNamespace(
            id="proc_terminal",
            command=command,
            task_id=kwargs["task_id"],
            session_key=kwargs["session_key"],
            cwd=kwargs["cwd"],
            pid=42,
            started_at=100.0,
            exited=False,
            exit_code=None,
            output_buffer="ready\r\n",
            _pty=FakePty(),
        )
        self.processes[process.id] = process
        return process

    def get(self, process_id: str):
        return self.processes.get(process_id)

    def list_sessions(self, task_id: str | None = None):
        return [
            {"session_id": process.id}
            for process in self.processes.values()
            if task_id is None or process.task_id == task_id
        ]

    def write_stdin(self, process_id: str, data: str):
        self.write_calls.append((process_id, data))
        return {"status": "ok", "bytes_written": len(data)}

    def kill_process(self, process_id: str, *, source: str = "process.kill"):
        self.kill_calls.append((process_id, source))
        process = self.processes[process_id]
        process.exited = True
        process.termination_source = source
        return {"status": "killed", "session_id": process_id}


@pytest.fixture()
def terminal_runtime(monkeypatch, tmp_path):
    registry = FakeProcessRegistry()
    monkeypatch.setattr(process_registry_module, "process_registry", registry)
    with server._sessions_lock:
        server._sessions["runtime-session"] = {
            "session_key": "conversation-session",
            "cwd": str(tmp_path),
        }
        server._sessions["other-runtime"] = {
            "session_key": "other-conversation",
            "cwd": str(tmp_path),
        }
    try:
        yield registry, tmp_path
    finally:
        with server._sessions_lock:
            server._sessions.pop("runtime-session", None)
            server._sessions.pop("other-runtime", None)


def result(response: dict) -> dict:
    assert "error" not in response
    return response["result"]


def test_desktop_terminal_methods_are_registered():
    assert {
        "terminal.session.open",
        "terminal.session.list",
        "terminal.session.write",
        "terminal.session.resize",
        "terminal.session.close",
    } <= set(server._methods)


def test_open_creates_and_then_reuses_session_scoped_pty(terminal_runtime):
    registry, tmp_path = terminal_runtime
    open_terminal = server._methods["terminal.session.open"]

    created = result(open_terminal("open-1", {"session_id": "runtime-session"}))
    restored = result(open_terminal("open-2", {"session_id": "runtime-session"}))

    assert created["reused"] is False
    assert restored["reused"] is True
    assert created["terminal"]["process_id"] == "proc_terminal"
    assert len(registry.spawn_calls) == 1
    assert registry.spawn_calls[0]["cwd"] == str(tmp_path)
    assert registry.spawn_calls[0]["session_key"] == "conversation-session"
    assert registry.spawn_calls[0]["task_id"] == "dovie.desktop.terminal:conversation-session"
    assert registry.spawn_calls[0]["use_pty"] is True


def test_open_uses_durable_session_and_explicit_workspace_when_agent_is_idle(terminal_runtime):
    registry, tmp_path = terminal_runtime

    created = result(
        server._methods["terminal.session.open"](
            "open-idle",
            {
                "session_id": "persisted-conversation",
                "stored_session_id": "persisted-conversation",
                "cwd": str(tmp_path),
                "runtime_scope_key": "profile:agent-1",
            },
        )
    )

    assert created["terminal"]["process_id"] == "proc_terminal"
    assert registry.spawn_calls[0]["cwd"] == str(tmp_path)
    assert registry.spawn_calls[0]["session_key"] == "persisted-conversation"
    assert registry.spawn_calls[0]["task_id"] == "dovie.desktop.terminal:persisted-conversation"


def test_write_resize_and_close_require_terminal_ownership(terminal_runtime):
    registry, _ = terminal_runtime
    open_terminal = server._methods["terminal.session.open"]
    result(open_terminal("open", {"session_id": "runtime-session"}))

    write = result(
        server._methods["terminal.session.write"](
            "write",
            {"session_id": "runtime-session", "process_id": "proc_terminal", "data": "pwd\r"},
        )
    )
    resized = result(
        server._methods["terminal.session.resize"](
            "resize",
            {"session_id": "runtime-session", "process_id": "proc_terminal", "cols": 140, "rows": 36},
        )
    )
    closed = result(
        server._methods["terminal.session.close"](
            "close",
            {"session_id": "runtime-session", "process_id": "proc_terminal"},
        )
    )

    assert write == {"status": "ok", "bytes_written": 4}
    assert resized == {"process_id": "proc_terminal", "cols": 140, "rows": 36}
    assert registry.processes["proc_terminal"]._pty.resize_calls == [(36, 140)]
    assert closed["status"] == "killed"
    assert registry.write_calls == [("proc_terminal", "pwd\r")]
    assert registry.kill_calls == [("proc_terminal", "terminal.session.close")]
    listed = result(
        server._methods["terminal.session.list"](
            "list-after-close",
            {"session_id": "runtime-session"},
        )
    )
    assert listed["terminals"] == []

    denied = server._methods["terminal.session.write"](
        "write-other",
        {"session_id": "other-runtime", "process_id": "proc_terminal", "data": "whoami\r"},
    )
    assert denied["error"]["code"] == 4044
