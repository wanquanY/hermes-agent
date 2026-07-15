from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_team_mission.state.memory import team_mission_bindings_events_map


def test_run_service_lists_events_by_run_with_per_run_limit(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        for run_id, text in (
            ("run-a", "a-1"),
            ("run-b", "b-1"),
            ("run-a", "a-2"),
            ("run-b", "b-2"),
        ):
            db.runs.append_event(
                "session-1",
                {
                    "type": "message.delta",
                    "run_id": run_id,
                    "payload": {"text": text},
                },
            )
        db.runs.append_event(
            "session-1",
            {
                "type": "_internal.trace",
                "run_id": "run-a",
                "payload": {"text": "internal"},
            },
        )

        events = db.runs.list_events_by_run_ids(
            ["run-b", "run-a", "run-missing", "run-a"],
            limit_per_run=1,
        )

        assert list(events) == ["run-b", "run-a", "run-missing"]
        assert [event["payload"]["text"] for event in events["run-a"]] == ["a-1"]
        assert [event["payload"]["text"] for event in events["run-b"]] == ["b-1"]
        assert events["run-missing"] == []
        including_internal = db.runs.list_events_by_run_ids(
            ["run-a"],
            include_internal=True,
        )
        assert [event["type"] for event in including_internal["run-a"]] == [
            "message.delta",
            "message.delta",
            "_internal.trace",
        ]
    finally:
        db.close()


def test_team_mission_memory_reads_binding_events_through_run_service(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.runs.append_event(
            "session-1",
            {
                "type": "message.complete",
                "run_id": "run-1",
                "payload": {"text": "done"},
            },
        )

        events = team_mission_bindings_events_map(
            db,
            [{"run_id": "run-1", "session_id": "session-1"}],
        )

        assert [event["type"] for event in events["run-1"]] == ["message.complete"]
        assert events["run-1"][0]["payload"]["text"] == "done"
    finally:
        db.close()
