from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store


def _event(
    event_type: str,
    *,
    session_id: str,
    run_id: str,
    payload: dict,
) -> dict:
    return {
        "type": event_type,
        "conversation_session_id": session_id,
        "session_id": session_id,
        "execution_session_id": "exec-1",
        "runtime_scope_key": f"scope:{session_id}",
        "run_id": run_id,
        "turn_id": "turn-1",
        "participant_id": "participant-agent",
        "payload": payload,
    }


@pytest.mark.parametrize(
    "session_id",
    ["conversation-ordinary", "team:mission:lifecycle:events"],
)
def test_terminal_run_closes_open_tools_before_terminal_event(
    tmp_path: Path,
    session_id: str,
) -> None:
    path = tmp_path / f"{session_id.replace(':', '-')}.db"
    db = open_cli_session_store(path)
    try:
        db.runs.append_event(
            session_id,
            _event(
                "tool.generating",
                session_id=session_id,
                run_id="run-1",
                payload={"tool_id": "call-1", "name": "write_file"},
            ),
        )

        terminal = db.runs.append_event(
            session_id,
            _event(
                "message.complete",
                session_id=session_id,
                run_id="run-1",
                payload={"status": "failed", "message": "provider stream failed"},
            ),
        )

        assert [
            item["type"] for item in terminal["_lifecycle_prelude_events"]
        ] == ["tool.complete"]
        events = db.runs.list_events(session_id)
        assert [item["type"] for item in events] == [
            "tool.complete",
            "message.complete",
        ]
        assert events[0]["seq"] < events[1]["seq"]
        tools = db.tool_event_projection.list(session_id, run_id="run-1")
        assert len(tools) == 1
        assert tools[0]["status"] == "failed"
        assert terminal["_lifecycle_prelude_events"][0]["payload"][
            "error_code"
        ] == "parent_run_terminal"
    finally:
        db.close()

    reloaded = open_cli_session_store(path)
    try:
        assert [
            item["type"] for item in reloaded.runs.list_events(session_id)
        ] == ["tool.complete", "message.complete"]
        assert reloaded.tool_event_projection.list(
            session_id,
            run_id="run-1",
        )[0]["status"] == "failed"
    finally:
        reloaded.close()


def test_migration_repairs_historical_terminal_run_tool(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "historical.db")
    try:
        db.runs.append_event(
            "conversation-historical",
            _event(
                "tool.generating",
                session_id="conversation-historical",
                run_id="run-historical",
                payload={"tool_id": "call-old", "name": "write_file"},
            ),
        )
        db._conn.execute(
            """
            UPDATE runs
               SET status = 'failed', completed_at = updated_at
             WHERE run_id = 'run-historical'
            """
        )

        migration = importlib.import_module(
            "hermes_agent.composition.migrations.0060_close_terminal_run_tools"
        )
        migration.apply(db._conn.cursor())

        tools = db.tool_event_projection.list(
            "conversation-historical",
            run_id="run-historical",
        )
        assert tools[0]["status"] == "failed"
        events = db.runs.list_events("conversation-historical")
        assert [item["type"] for item in events] == [
            "tool.generating",
            "tool.complete",
        ]
        assert events[-1]["payload"]["lifecycle_repair"] is True
    finally:
        db.close()
