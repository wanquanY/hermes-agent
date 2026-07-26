from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_runtime_events import KanbanRuntimeEventSink


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_kanban_runtime_event_sink_writes_run_scoped_side_channel_events(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="worker task",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = kb.get_task(conn, task_id).current_run_id

        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

        sink = KanbanRuntimeEventSink.from_env(session_id="sid-worker", runtime_scope_key="scope-worker")
        assert sink is not None
        sink.start(profile="worker", workspace="/tmp/workspace")
        sink.on_message_delta("hello ")
        sink.on_message_delta("world")
        sink.on_tool_generating("read_file", "tool-1")
        sink.on_tool_start("tool-1", "read_file", {"path": "brief.md"})
        sink.on_tool_complete("tool-1", "read_file", {"path": "brief.md"}, "brief")
        sink.complete(text="hello world", status="completed")

        db_runtime_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind.startswith("runtime.")
        ]
        assert db_runtime_events == []

        log_path = kb.runtime_event_log_path("default", task_id)
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        kinds = [event["kind"] for event in events]
        assert "runtime.session" in kinds
        assert "runtime.message_delta" in kinds
        assert "runtime.tool_generating" in kinds
        assert "runtime.tool_start" in kinds
        assert "runtime.tool_complete" in kinds
        assert "runtime.message_complete" in kinds
        assert {event["run_id"] for event in events} == {run_id}

        message_deltas = [event for event in events if event["kind"] == "runtime.message_delta"]
        assert "".join(event["payload"]["text"] for event in message_deltas) == "hello world"
        assert {event["payload"]["session_id"] for event in message_deltas} == {"sid-worker"}
        assert {event["payload"]["runtime_scope_key"] for event in message_deltas} == {"scope-worker"}

        tool_generating = next(
            event for event in events if event["kind"] == "runtime.tool_generating"
        )
        assert tool_generating["payload"]["type"] == "tool.generating"
        assert tool_generating["payload"]["tool_id"] == "tool-1"

        tool_complete = next(event for event in events if event["kind"] == "runtime.tool_complete")
        assert tool_complete["payload"]["type"] == "tool.complete"
        assert tool_complete["payload"]["tool_call_id"] == "tool-1"
        assert tool_complete["payload"]["result_text"] == "brief"
    finally:
        conn.close()
