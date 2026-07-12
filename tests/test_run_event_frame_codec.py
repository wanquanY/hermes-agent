from __future__ import annotations

import json
import time

from hermes_conversation_message_identity import AssistantMessageIdentity
from hermes_conversation_message_identity import assistant_conversation_message_id_for
from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_agent.domain.run_event_codec import decode_run_event_row


def _message_delta(seq: int, text: str = "hello") -> dict:
    return {
        "type": "message.delta",
        "session_id": "runtime-1",
        "conversation_session_id": "session-1",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "seq": seq,
        "timestamp": 1000.0 + seq,
        "payload": {"delta": text, "offset": 0},
    }


def test_runtime_source_seq_uses_explicit_column_after_json_cache_is_cleared(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        frame = _message_delta(1, "source")
        frame["payload"]["runtime_source_seq"] = 77
        db.runs.append_event("session-1", frame)
        row = db._conn.execute("SELECT * FROM run_events WHERE session_id = ?", ("session-1",)).fetchone()

        assert row["runtime_source_seq"] == 77

        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE run_events SET event_json = '{}', payload_json = '' WHERE id = ?",
                (row["id"],),
            )
        )

        assert db.runs.has_event_source(
            "session-1",
            run_id="run-1",
            runtime_source_seq=77,
        )
    finally:
        db.close()


def test_payload_contains_uses_run_event_search_index_after_json_cache_is_cleared(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        frame = {
            **_message_delta(1, "subagent output"),
            "type": "subagent.output_delta",
            "payload": {"subagent_id": "sa-index", "text": "subagent output"},
        }
        db.runs.append_event("session-1", frame)
        row = db._conn.execute("SELECT * FROM run_events WHERE session_id = ?", ("session-1",)).fetchone()
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE run_events SET event_json = '{}', payload_json = '' WHERE id = ?",
                (row["id"],),
            )
        )

        events = db.runs.list_filtered_events(
            "session-1",
            event_type_prefix="subagent.",
            payload_contains="sa-index",
        )
    finally:
        db.close()

    assert [event["seq"] for event in events] == [1]
    assert events[0]["payload"]["subagent_id"] == "sa-index"


def test_reference_run_event_payloads_slim_message_complete_and_rehydrates_from_messages(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    large_text = "final answer " * 300
    try:
        db.sessions.create("session-1", "dovie")
        now = time.time()
        event = {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "session-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "message_seq_in_run": 1,
            "participant_id": "agent:default",
            "seq": 1,
            "timestamp": now,
            "payload": {"text": large_text, "status": "complete"},
        }
        db.runs.append_event("session-1", event)
        conversation_message_id = assistant_conversation_message_id_for(
            AssistantMessageIdentity("session-1", "run-1", "1")
        )
        db.messages.upsert_team_message(
            session_id="session-1",
            conversation_message_id=conversation_message_id,
            role="assistant",
            content=large_text,
            participant_id="agent:default",
            metadata={
                "run_id": "run-1",
                "turn_id": "turn-1",
                "message_seq_in_run": "1",
                "client_message_id": "turn-1:assistant-segment:1",
            },
            status="completed",
        )

        result = db.run_event_maintenance.reference_payloads(session_id="session-1")
        row = db._conn.execute("SELECT * FROM run_events WHERE session_id = ?", ("session-1",)).fetchone()
        decoded = decode_run_event_row(row)
        events = db.runs.list_events("session-1")
    finally:
        db.close()

    assert result["referenced_events"] == 1
    assert row["projection_state"] == "referenced"
    assert row["projected_message_id"] == conversation_message_id
    assert large_text not in row["event_json"]
    assert decoded["payload"]["summary"] == large_text[:500]
    assert "text" not in decoded["payload"]
    assert events[0]["payload"]["text"] == large_text
    assert events[0]["payload"]["message_id"] == conversation_message_id
    assert events[0]["payload"]["client_message_id"] == "turn-1:assistant-segment:1"


def test_reference_run_event_payloads_slim_tool_complete_and_rehydrates_from_tool_events(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    large_result = "tool output " * 300
    try:
        db.sessions.create("session-1", "dovie")
        now = time.time()
        db.runs.append_event(
            "session-1",
            {
                "type": "tool.complete",
                "session_id": "runtime-1",
                "conversation_session_id": "session-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "participant_id": "agent:default",
                "seq": 1,
                "timestamp": now,
                "payload": {
                    "tool_call_id": "tool-1",
                    "tool_name": "terminal",
                    "result": {"text": large_result},
                    "result_text": large_result,
                    "status": "completed",
                },
            },
        )

        result = db.run_event_maintenance.reference_payloads(session_id="session-1")
        row = db._conn.execute("SELECT * FROM run_events WHERE session_id = ?", ("session-1",)).fetchone()
        decoded = decode_run_event_row(row)
        events = db.runs.list_events("session-1")
    finally:
        db.close()

    assert result["referenced_events"] == 1
    assert row["projection_state"] == "referenced"
    assert row["projected_tool_event_id"]
    assert large_result not in row["event_json"]
    assert decoded["payload"]["summary"] == large_result[:500]
    assert "result" not in decoded["payload"]
    assert events[0]["payload"]["result"] == {"text": large_result}
    assert events[0]["payload"]["result_text"] == large_result


def test_append_run_event_double_writes_compressed_frame(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        db.runs.append_event("session-1", _message_delta(1, "stream text"))
        row = db._conn.execute("SELECT * FROM run_events WHERE session_id = ?", ("session-1",)).fetchone()

        assert row["frame_blob"]
        assert row["frame_format"] == "zlib+json:v1"
        assert row["retention_class"] == "stream"
        decoded = decode_run_event_row(row)
        assert decoded["payload"]["delta"] == "stream text"

        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE run_events SET event_json = '{}', payload_json = '' WHERE id = ?",
                (row["id"],),
            )
        )
        events = db.runs.list_events("session-1")
    finally:
        db.close()

    assert events[0]["payload"]["delta"] == "stream text"
    assert events[0]["type"] == "message.delta"


def test_backfill_run_event_frame_blobs_migrates_legacy_rows(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "dovie")
        frame = _message_delta(1, "legacy text")
        payload = frame["payload"]
        db._execute_write(
            lambda conn: conn.execute(
                """
                INSERT INTO run_events (
                    session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                    event_type, seq, timestamp, payload_json, event_json, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "session-1",
                    "run-1",
                    "turn-1",
                    "runtime-1",
                    "session-1",
                    "message.delta",
                    1,
                    1001.0,
                    json.dumps(payload),
                    json.dumps(frame),
                    "",
                ),
            )
        )

        result = db.run_event_maintenance.backfill_frames()
        row = db._conn.execute("SELECT * FROM run_events WHERE session_id = ?", ("session-1",)).fetchone()
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE run_events SET event_json = '{}', payload_json = '' WHERE id = ?",
                (row["id"],),
            )
        )
        events = db.runs.list_events("session-1")
    finally:
        db.close()

    assert result["updated_events"] == 1
    assert result["remaining_events"] == 0
    assert row["frame_blob"]
    assert row["retention_class"] == "stream"
    assert events[0]["payload"]["delta"] == "legacy text"
