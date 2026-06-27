from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hermes_state import SessionDB
from tui_gateway.run_worker import DBRpcRequestFrame
from tui_gateway.services.worker_db_proxy import (
    WorkerDBProxy,
    WorkerDBProxyDisconnectedError,
    WorkerDBProxyMethodError,
    WorkerDBProxyRemoteError,
    WorkerDBProxyTimeoutError,
    serialize_db_value,
)
from tui_gateway.services.worker_supervisor import WorkerSupervisor


class _Writer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def write_json(self, obj: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(obj)

    def pop(self) -> dict[str, Any]:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with self.lock:
                if self.requests:
                    return self.requests.pop(0)
            time.sleep(0.001)
        raise AssertionError("timed out waiting for proxy request")


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = None
        self.returncode = None
        self.pid = 0


class _FakeDB:
    def __init__(self) -> None:
        self.messages = [{"role": "user", "content": "hello"}]
        self.appended: list[tuple[str, str, str | None]] = []
        self.entered: list[str] = []
        self.active = 0
        self.max_active = 0

    def get_messages_as_conversation(self, session_id: str):
        return list(self.messages)

    def get_conversation_message_read_model(self, session_id: str):
        return [{"role": "assistant", "content": f"canonical:{session_id}"}]

    def get_session_index(self, session_id: str):
        return {"session_id": session_id, "conversation_kind": "team"}

    def append_message(self, session_id: str, role: str, content: str | None = None, **kwargs):
        self.appended.append((session_id, role, content))
        self.messages.append({"role": role, "content": content})
        return len(self.messages)

    def explode(self):
        raise RuntimeError("boom")

    def slow_append_message(self, session_id: str, role: str, content: str | None = None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.append(content or "")
        time.sleep(0.01)
        self.active -= 1
        return len(self.entered)


def _proxy_roundtrip(proxy: WorkerDBProxy, writer: _Writer, result=None, error=None):
    request = writer.pop()
    reply = {"jsonrpc": "2.0", "id": request["id"]}
    if error is not None:
        reply["error"] = error
    else:
        reply["result"] = result
    proxy.handle_reply(reply)
    return request


def test_get_messages_proxy_returns_correct_data() -> None:
    writer = _Writer()
    proxy = WorkerDBProxy(writer, timeout_s=1)
    result_box: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result_box.update(
            result=proxy.get_messages_as_conversation("session-1")
        )
    )
    thread.start()
    request = _proxy_roundtrip(
        proxy,
        writer,
        result=[{"role": "user", "content": "hello"}],
    )
    thread.join(timeout=1)
    assert result_box["result"] == [{"role": "user", "content": "hello"}]
    assert request["method"] == "db.get_messages_as_conversation"
    assert request["params"] == [["session-1"], {}]


@pytest.mark.asyncio
async def test_append_message_proxy_writes_through_main(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    monkeypatch.setattr(
        "tui_gateway.server._db_for_stable_session",
        lambda _stable: db,
        raising=False,
    )
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )
    reply = await supervisor._execute_db_rpc(
        DBRpcRequestFrame(
            id="1",
            method="db.append_message",
            params=[["session-1", "user", "hi"], {}],
            db_scope={"stable_session_id": "session-1"},
        )
    )
    assert reply.error is None
    assert reply.result == 2
    assert db.appended == [("session-1", "user", "hi")]


@pytest.mark.asyncio
async def test_projector_read_model_methods_are_exposed_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB()
    monkeypatch.setattr(
        "tui_gateway.server._db_for_stable_session",
        lambda _stable: db,
        raising=False,
    )
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )

    read_model_reply = await supervisor._execute_db_rpc(
        DBRpcRequestFrame(
            id="1",
            method="db.get_conversation_message_read_model",
            params=[["team-session-1"], {}],
            db_scope={"stable_session_id": "team-session-1"},
        )
    )
    session_index_reply = await supervisor._execute_db_rpc(
        DBRpcRequestFrame(
            id="2",
            method="db.get_session_index",
            params=[["team-session-1"], {}],
            db_scope={"stable_session_id": "team-session-1"},
        )
    )

    assert read_model_reply.error is None
    assert read_model_reply.result == [{"role": "assistant", "content": "canonical:team-session-1"}]
    assert session_index_reply.error is None
    assert session_index_reply.result["conversation_kind"] == "team"


@pytest.mark.asyncio
async def test_create_activity_proxy_writes_through_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(
        "tui_gateway.server._db_for_stable_session",
        lambda _stable: db,
        raising=False,
    )
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )
    reply = await supervisor._execute_db_rpc(
        DBRpcRequestFrame(
            id="1",
            method="db.create_activity",
            params=[
                [],
                {
                    "activity_id": "act-1",
                    "conversation_id": "conv-1",
                    "kind": "agent_dispatch",
                    "prompt_summary": "Review the report",
                },
            ],
            db_scope={"stable_session_id": "conv-1"},
        )
    )

    assert reply.error is None
    assert reply.result["activity_id"] == "act-1"
    assert reply.result["conversation_id"] == "conv-1"
    assert reply.result["status"] == "pending"
    assert db.get_activity("act-1")["prompt_summary"] == "Review the report"


def test_proxy_handles_sqlite_row_conversion(tmp_path: Path) -> None:
    path = tmp_path / "row.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sample (id INTEGER, payload BLOB)")
    conn.execute("INSERT INTO sample VALUES (?, ?)", (1, b"abc"))
    row = conn.execute("SELECT * FROM sample").fetchone()
    writer = _Writer()
    proxy = WorkerDBProxy(writer, timeout_s=1)
    result_box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: result_box.update(result=proxy.get_session("s1")))
    thread.start()
    _proxy_roundtrip(proxy, writer, result=serialize_db_value(row))
    thread.join(timeout=1)
    assert result_box["result"] == {"id": 1, "payload": b"abc"}


def test_proxy_propagates_exceptions() -> None:
    writer = _Writer()
    proxy = WorkerDBProxy(writer, timeout_s=1)
    error_box: dict[str, BaseException] = {}

    def call() -> None:
        try:
            proxy.get_session("missing")
        except BaseException as exc:
            error_box["error"] = exc

    thread = threading.Thread(target=call)
    thread.start()
    _proxy_roundtrip(
        proxy,
        writer,
        error={"code": -32000, "type": "RuntimeError", "message": "boom"},
    )
    thread.join(timeout=1)
    assert isinstance(error_box["error"], WorkerDBProxyRemoteError)
    assert "boom" in str(error_box["error"])


@pytest.mark.asyncio
async def test_proxy_method_not_in_whitelist_rejected() -> None:
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )
    reply = await supervisor._execute_db_rpc(
        DBRpcRequestFrame(id="1", method="db.drop_everything", params=[[], {}])
    )
    assert reply.result is None
    assert reply.error["type"] == "WorkerDBProxyMethodError"
    writer = _Writer()
    proxy = WorkerDBProxy(writer, timeout_s=1)
    error_box: dict[str, BaseException] = {}

    def call() -> None:
        try:
            proxy.drop_everything()
        except BaseException as exc:
            error_box["error"] = exc

    thread = threading.Thread(target=call)
    thread.start()
    _proxy_roundtrip(proxy, writer, error=reply.error)
    thread.join(timeout=1)
    assert isinstance(error_box["error"], WorkerDBProxyMethodError)


@pytest.mark.asyncio
async def test_concurrent_worker_calls_serialize(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    monkeypatch.setattr("tui_gateway.services.worker_supervisor.DB_RPC_ALLOWED_METHODS", {"slow_append_message"})
    monkeypatch.setattr(
        "tui_gateway.server._db_for_stable_session",
        lambda _stable: db,
        raising=False,
    )
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )
    replies = await asyncio.gather(
        *[
            supervisor._execute_db_rpc(
                DBRpcRequestFrame(
                    id=str(index),
                    method="db.slow_append_message",
                    params=[["session-1", "user", str(index)], {}],
                    db_scope={"stable_session_id": "session-1"},
                )
            )
            for index in range(5)
        ]
    )
    assert [reply.result for reply in replies] == [1, 2, 3, 4, 5]
    assert db.max_active == 1


def test_proxy_timeout_handled() -> None:
    writer = _Writer()
    proxy = WorkerDBProxy(writer, timeout_s=0.02)
    with pytest.raises(WorkerDBProxyTimeoutError):
        proxy.get_session("session-1")


def test_proxy_handles_main_process_disconnect() -> None:
    writer = _Writer()
    proxy = WorkerDBProxy(writer, timeout_s=1)
    error_box: dict[str, BaseException] = {}

    def call() -> None:
        try:
            proxy.get_session("session-1")
        except BaseException as exc:
            error_box["error"] = exc

    thread = threading.Thread(target=call)
    thread.start()
    writer.pop()
    proxy.close()
    thread.join(timeout=1)
    assert isinstance(error_box["error"], WorkerDBProxyDisconnectedError)


@pytest.mark.asyncio
async def _noop(*_args, **_kwargs) -> None:
    return None


def test_benchmark_proxy_append_message_100_calls(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("bench", source="test")
    started = time.perf_counter()
    for index in range(100):
        db.append_message("bench", "user", f"msg {index}")
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2
