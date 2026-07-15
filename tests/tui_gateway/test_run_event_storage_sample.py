from __future__ import annotations

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services.run_event_storage_sample import (
    collect_run_event_storage_sample,
    format_run_event_storage_sample,
)


def test_collect_run_event_storage_sample_groups_payload_bytes_by_event_type(tmp_path):
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    try:
        db.runs.append_event(
            "stored-1",
            {
                "type": "message.delta",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "seq": 1,
                "payload": {"delta": "A"},
            },
        )
        db.runs.append_event(
            "stored-1",
            {
                "type": "message.delta",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "seq": 2,
                "payload": {"delta": "longer text"},
            },
        )
        db.runs.append_event(
            "stored-1",
            {
                "type": "tool.complete",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "seq": 3,
                "payload": {"tool_name": "terminal", "result": "ok"},
            },
        )
    finally:
        db.close()

    sample = collect_run_event_storage_sample(tmp_path / "state.db")
    by_type = {row["event_type"]: row for row in sample["by_event_type"]}

    assert sample["error"] == ""
    assert sample["total_rows"] == 3
    assert by_type["message.delta"]["rows"] == 2
    assert by_type["message.delta"]["payload_bytes"] > 0
    assert by_type["message.delta"]["p50_payload_bytes"] > 0
    assert by_type["message.delta"]["p95_payload_bytes"] >= by_type["message.delta"]["p50_payload_bytes"]
    assert by_type["message.delta"]["max_payload_bytes"] >= by_type["message.delta"]["p95_payload_bytes"]
    assert by_type["tool.complete"]["rows"] == 1

    rendered = format_run_event_storage_sample(sample, top=10)
    assert "message.delta" in rendered
    assert "tool.complete" in rendered
