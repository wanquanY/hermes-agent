from __future__ import annotations

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store


def _tool_event(
    event_type: str,
    *,
    seq: int,
    tool_id: str = "tool-1",
    name: str = "terminal",
    run_id: str = "run-1",
    payload: dict | None = None,
) -> dict:
    body = {
        "type": event_type,
        "session_id": "runtime-1",
        "conversation_session_id": "session-1",
        "run_id": run_id,
        "turn_id": "turn-1",
        "participant_id": "agent:default",
        "seq": seq,
        "timestamp": 1000.0 + seq,
        "payload": {
            "name": name,
        },
    }
    if tool_id:
        body["payload"]["tool_id"] = tool_id
    body["payload"].update(payload or {})
    return body


def test_append_run_event_projects_tool_events_index(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.start",
                seq=1,
                payload={"arguments": {"command": "pwd"}, "context": "$ pwd"},
            ),
        )
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.progress",
                seq=2,
                payload={"preview": "running pwd"},
            ),
        )
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=3,
                payload={
                    "result": {"exit_code": 0},
                    "result_text": "/tmp/project",
                    "summary": "pwd completed",
                    "duration_s": 1.25,
                },
            ),
        )

        tool_events = db.tool_event_projection.list("session-1")
        run_events = db.runs.list_events("session-1")
    finally:
        db.close()

    assert len(tool_events) == 1
    tool = tool_events[0]
    assert tool["tool_call_id"] == "tool-1"
    assert tool["name"] == "terminal"
    assert tool["status"] == "completed"
    assert tool["phase"] == "complete"
    assert tool["arguments"] == {"command": "pwd"}
    assert tool["progress"]["preview"] == "running pwd"
    assert tool["result"] == {"exit_code": 0}
    assert tool["result_text"] == "/tmp/project"
    assert tool["summary"] == "pwd completed"
    assert tool["seq_start"] == 1
    assert tool["seq_last"] == 3
    assert [event["type"] for event in run_events] == [
        "tool.start",
        "tool.progress",
        "tool.complete",
    ]


def test_list_tool_events_tail_returns_latest_events_in_chronological_order(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=1,
                tool_id="tool-old",
                name="read_file",
                payload={"result_text": "old"},
            ),
        )
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=2,
                tool_id="tool-new",
                name="search_files",
                payload={"result_text": "new"},
            ),
        )

        tool_events = db.tool_event_projection.list("session-1", direction="tail", limit=1)
    finally:
        db.close()

    assert [tool["tool_call_id"] for tool in tool_events] == ["tool-new"]
    assert tool_events[0]["seq_start"] == 2


def test_tool_progress_without_id_attaches_to_latest_open_tool(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.start",
                seq=1,
                tool_id="tool-terminal-1",
                payload={"arguments": {"command": "ls"}},
            ),
        )
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.progress",
                seq=2,
                tool_id="",
                payload={"preview": "listing files"},
            ),
        )

        tool_events = db.tool_event_projection.list("session-1")
    finally:
        db.close()

    assert len(tool_events) == 1
    assert tool_events[0]["tool_call_id"] == "tool-terminal-1"
    assert tool_events[0]["progress"]["preview"] == "listing files"
    assert tool_events[0]["seq_last"] == 2


def test_tool_start_after_complete_does_not_regress_terminal_status(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=1,
                payload={"result_text": "done"},
            ),
        )
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.start",
                seq=2,
                payload={"arguments": {"command": "pwd"}},
            ),
        )

        tool_events = db.tool_event_projection.list("session-1")
    finally:
        db.close()

    assert len(tool_events) == 1
    assert tool_events[0]["status"] == "completed"
    assert tool_events[0]["phase"] == "complete"
    assert tool_events[0]["seq_last"] == 2


def test_tool_events_backfill_from_existing_run_events(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.start",
                seq=1,
                payload={"arguments": {"path": "README.md"}},
                name="read_file",
            ),
        )
        db.runs.append_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=2,
                payload={"result_text": "contents"},
                name="read_file",
            ),
        )

        db._execute_write(lambda conn: conn.execute("DELETE FROM tool_events"))
        assert db.tool_event_projection.list("session-1") == []
        db.run_event_maintenance.rebuild_tool_event_projection()
        tool_events = db.tool_event_projection.list("session-1")
    finally:
        db.close()

    assert len(tool_events) == 1
    assert tool_events[0]["tool_call_id"] == "tool-1"
    assert tool_events[0]["name"] == "read_file"
    assert tool_events[0]["status"] == "completed"
    assert tool_events[0]["result_text"] == "contents"
