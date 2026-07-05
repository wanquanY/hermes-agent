from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_conversation_message_identity import AssistantMessageIdentity
from hermes_conversation_message_identity import assistant_conversation_message_id_for
from hermes_state import SessionDB
from hermes_state_participants import leader_participant_id
from hermes_team_mission.runtime.team_transcript_writer import RuntimeTranscriptWriter


def _create_team_session(tmp_path: Path, conversation_id: str) -> tuple[SessionDB, str, str]:
    db = SessionDB(tmp_path / "state.db")
    session_id = f"team-session-{conversation_id}"
    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=session_id,
        team_id="team-1",
        title="Team conversation",
    )
    return db, session_id, leader_participant_id(conversation_id)


def _frame(
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    participant_id: str,
    activity_id: str,
    event_type: str,
    seq: int,
    text: str = "",
    client_message_id: str = "",
    message_seq_in_run: Any = None,
    offset: Any = None,
    timestamp: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "activity_id": activity_id,
        "activityId": activity_id,
        "participant_id": participant_id,
        "participantId": participant_id,
        "activity_kind": "leader_chat",
        "activityKind": "leader_chat",
        "transcript_activity_kind": "leader_chat",
        "transcriptActivityKind": "leader_chat",
    }
    if client_message_id:
        payload["client_message_id"] = client_message_id
        payload["clientMessageId"] = client_message_id
    if message_seq_in_run is not None:
        payload["message_seq_in_run"] = message_seq_in_run
        payload["messageSeqInRun"] = message_seq_in_run

    frame = {
        "type": event_type,
        "session_id": f"runtime-{run_id}",
        "stored_session_id": session_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": "profile:leader",
        "activity_id": activity_id,
        "activityId": activity_id,
        "participant_id": participant_id,
        "participantId": participant_id,
        "activity_kind": "leader_chat",
        "activityKind": "leader_chat",
        "transcript_activity_kind": "leader_chat",
        "transcriptActivityKind": "leader_chat",
        "seq": seq,
        "timestamp": float(seq) if timestamp is None else timestamp,
        "payload": payload,
    }
    if client_message_id:
        frame["client_message_id"] = client_message_id
        frame["clientMessageId"] = client_message_id
    if message_seq_in_run is not None:
        frame["message_seq_in_run"] = message_seq_in_run
        frame["messageSeqInRun"] = message_seq_in_run

    if event_type == "message.delta":
        payload.update({"mode": "append", "delta": text, "text": text})
        if offset is not None:
            payload["offset"] = offset
    elif event_type == "message.complete":
        payload.update({"status": "complete", "text": text})
    elif event_type.startswith("tool."):
        payload.update({"tool_call_id": f"tool-{seq}", "tool_name": "search_files"})
    return frame


def _insert_raw_run_events(db: SessionDB, session_id: str, events: list[dict[str, Any]]) -> None:
    def insert(conn):
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            conn.execute(
                """
                INSERT INTO run_events (
                    session_id, run_id, turn_id, runtime_session_id, runtime_scope_key,
                    activity_id, event_type, seq, timestamp, payload_json, event_json,
                    status, participant_id, projection_state, runtime_source_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    event["run_id"],
                    event["turn_id"],
                    event["session_id"],
                    event["runtime_scope_key"],
                    event["activity_id"],
                    event["type"],
                    event["seq"],
                    event["timestamp"],
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(event, ensure_ascii=False),
                    "completed" if event["type"] == "message.complete" else "",
                    event["participant_id"],
                    "raw",
                    event["seq"],
                ),
            )

    db._execute_write(insert)  # noqa: SLF001 - test seeds legacy raw rows for backfill.


def _conversation_message_id(session_id: str, run_id: str, message_seq_in_run: str) -> str:
    return assistant_conversation_message_id_for(
        AssistantMessageIdentity(
            session_id=session_id,
            run_id=run_id,
            message_seq_in_run=message_seq_in_run,
        )
    )


def _stored_messages_by_timestamp(db: SessionDB, session_id: str) -> list[dict[str, Any]]:
    with db._lock:  # noqa: SLF001 - test verifies storage ordering contract.
        rows = db._conn.execute(  # noqa: SLF001 - test verifies storage ordering contract.
            """
            SELECT content, timestamp, conversation_message_id, metadata_json
            FROM messages
            WHERE session_id = ?
              AND active = 1
            ORDER BY timestamp ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
    messages: list[dict[str, Any]] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        messages.append({
            "content": row["content"],
            "timestamp": row["timestamp"],
            "conversation_message_id": row["conversation_message_id"],
            "metadata": metadata,
        })
    return messages


def test_team_transcript_projects_pre_tool_assistant_segment_from_raw_stream(tmp_path: Path) -> None:
    db, session_id, participant_id = _create_team_session(tmp_path, "raw-segments-live")
    run_id = "team-leader-run-raw-segments-live"
    turn_id = "turn-raw-segments-live"
    activity_id = f"chat:{session_id}"
    first_client_id = f"{turn_id}:assistant-segment:0"
    final_client_id = f"{turn_id}:assistant-segment:2"

    for event in [
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.start",
            seq=1,
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.delta",
            seq=2,
            text="Opening ",
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.delta",
            seq=3,
            text="before tools.",
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="tool.start",
            seq=4,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="tool.complete",
            seq=5,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.complete",
            seq=6,
            text="Final answer.",
            client_message_id=final_client_id,
            message_seq_in_run=3,
        ),
    ]:
        db.append_run_event(session_id, event)

    messages = db.get_conversation_message_read_model(session_id, include_storage_metadata=True)

    assert [message["role"] for message in messages] == ["assistant", "assistant"]
    assert [message["content"] for message in messages] == ["Opening before tools.", "Final answer."]
    assert messages[0]["conversation_message_id"] == _conversation_message_id(session_id, run_id, "1")
    assert messages[1]["conversation_message_id"] == _conversation_message_id(session_id, run_id, "3")
    assert messages[0]["metadata"]["reconstructed_from_raw"] is True
    assert messages[0]["metadata"]["raw_segment_identity"]["assistant_segment_index"] == "0"
    assert "reconstructed_from_raw" not in messages[1]["metadata"]


def test_team_transcript_raw_segment_reconstruction_deduplicates_double_written_offsets(
    tmp_path: Path,
) -> None:
    db, session_id, participant_id = _create_team_session(tmp_path, "raw-segments-double-write")
    run_id = "team-leader-run-raw-segments-double-write"
    turn_id = "turn-raw-segments-double-write"
    activity_id = f"chat:{session_id}"
    first_client_id = f"{turn_id}:assistant-segment:0"
    final_client_id = f"{turn_id}:assistant-segment:2"

    for event in [
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.start",
            seq=1,
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.start",
            seq=2,
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.delta",
            seq=3,
            text="好的",
            offset=0,
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.delta",
            seq=4,
            text="好的",
            offset=0,
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.delta",
            seq=5,
            text="，我来发起一个简单的任务。",
            offset=2,
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.delta",
            seq=6,
            text="，我来发起一个简单的任务。",
            offset=2,
            client_message_id=first_client_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="tool.start",
            seq=7,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="tool.start",
            seq=8,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="tool.complete",
            seq=9,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="tool.complete",
            seq=10,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.complete",
            seq=11,
            text="最终回复。",
            client_message_id=final_client_id,
            message_seq_in_run=3,
        ),
    ]:
        db.append_run_event(session_id, event)

    messages = db.get_conversation_message_read_model(session_id, include_storage_metadata=True)

    assert [message["content"] for message in messages] == [
        "好的，我来发起一个简单的任务。",
        "最终回复。",
    ]
    assert messages[0]["content"].count("好的") == 1
    assert messages[0]["metadata"]["reconstructed_from_raw"] is True


def test_team_transcript_raw_segment_backfill_orders_reconstruction_before_existing_final_by_timestamp(
    tmp_path: Path,
) -> None:
    db, session_id, participant_id = _create_team_session(tmp_path, "raw-segments-backfill-order")
    run_id = "team-leader-run-raw-segments-backfill-order"
    turn_id = "turn-raw-segments-backfill-order"
    activity_id = f"chat:{session_id}"
    first_client_id = f"{turn_id}:assistant-segment:0"
    final_client_id = f"{turn_id}:assistant-segment:2"
    start_event = _frame(
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        activity_id=activity_id,
        event_type="message.start",
        seq=1,
        timestamp=10.0,
        client_message_id=first_client_id,
        message_seq_in_run=1,
    )
    complete_event = _frame(
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        activity_id=activity_id,
        event_type="message.complete",
        seq=4,
        timestamp=90.0,
        text="Existing final.",
        client_message_id=final_client_id,
        message_seq_in_run=3,
    )
    final_conversation_message_id = _conversation_message_id(session_id, run_id, "3")

    def insert_existing_final(conn):
        return db._upsert_team_message_by_id_locked(  # noqa: SLF001 - simulates legacy projected final row.
            conn,
            session_id=session_id,
            conversation_message_id=final_conversation_message_id,
            role="assistant",
            content="Existing final.",
            participant_id=participant_id,
            metadata={
                "source": "team_mission.runtime_event",
                "message_kind": "assistant_reply",
                "run_id": run_id,
                "turn_id": turn_id,
                "activity_kind": "leader_chat",
                "transcript_activity_kind": "leader_chat",
                "team_mission": {
                    "activity_id": activity_id,
                    "activity_kind": "leader_chat",
                },
            },
            status="completed",
            timestamp=complete_event["timestamp"],
        )

    db._execute_write(insert_existing_final)  # noqa: SLF001 - seeds ordering regression state.
    _insert_raw_run_events(
        db,
        session_id,
        [
            start_event,
            _frame(
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                participant_id=participant_id,
                activity_id=activity_id,
                event_type="message.delta",
                seq=2,
                timestamp=20.0,
                text="Early raw segment.",
                offset=0,
                client_message_id=first_client_id,
                message_seq_in_run=1,
            ),
            _frame(
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                participant_id=participant_id,
                activity_id=activity_id,
                event_type="tool.start",
                seq=3,
                timestamp=30.0,
            ),
            complete_event,
        ],
    )

    db.get_conversation_message_read_model(session_id, include_storage_metadata=True)
    stored_messages = _stored_messages_by_timestamp(db, session_id)

    assert [message["content"] for message in stored_messages] == [
        "Early raw segment.",
        "Existing final.",
    ]
    assert stored_messages[0]["timestamp"] == start_event["timestamp"]
    assert stored_messages[0]["timestamp"] < stored_messages[1]["timestamp"]
    assert stored_messages[0]["metadata"]["reconstructed_from_raw"] is True


def test_team_transcript_raw_segment_reconstruction_is_idempotent_for_reproject_and_backfill(
    tmp_path: Path,
) -> None:
    db, session_id, participant_id = _create_team_session(tmp_path, "raw-segments-backfill")
    run_id = "team-leader-run-raw-segments-backfill"
    turn_id = "turn-raw-segments-backfill"
    activity_id = f"chat:{session_id}"
    first_client_id = f"{turn_id}:assistant-segment:0"
    final_client_id = f"{turn_id}:assistant-segment:2"
    complete_event = _frame(
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        activity_id=activity_id,
        event_type="message.complete",
        seq=5,
        text="Backfilled final.",
        client_message_id=final_client_id,
        message_seq_in_run=3,
    )
    _insert_raw_run_events(
        db,
        session_id,
        [
            _frame(
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                participant_id=participant_id,
                activity_id=activity_id,
                event_type="message.start",
                seq=1,
                client_message_id=first_client_id,
                message_seq_in_run=1,
            ),
            _frame(
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                participant_id=participant_id,
                activity_id=activity_id,
                event_type="message.delta",
                seq=2,
                text="Backfilled opening.",
                client_message_id=first_client_id,
                message_seq_in_run=1,
            ),
            _frame(
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                participant_id=participant_id,
                activity_id=activity_id,
                event_type="tool.start",
                seq=3,
            ),
            _frame(
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                participant_id=participant_id,
                activity_id=activity_id,
                event_type="tool.complete",
                seq=4,
            ),
            complete_event,
        ],
    )

    messages = db.get_conversation_message_read_model(session_id, include_storage_metadata=True)
    row_ids = [message["message_id"] for message in messages]

    assert [message["content"] for message in messages] == ["Backfilled opening.", "Backfilled final."]

    def reproject(conn):
        return RuntimeTranscriptWriter.project_message_complete_event_locked(
            db,
            conn,
            session_id=session_id,
            event=complete_event,
        )

    db._execute_write(reproject)  # noqa: SLF001 - verifies duplicate projection idempotency.
    messages_after_reproject = db.get_conversation_message_read_model(
        session_id,
        include_storage_metadata=True,
    )
    assert [message["message_id"] for message in messages_after_reproject] == row_ids

    def reset_complete_projection(conn):
        conn.execute(
            """
            UPDATE run_events
               SET projected_message_id = '',
                   projection_state = 'raw'
             WHERE session_id = ?
               AND run_id = ?
               AND event_type = 'message.complete'
            """,
            (session_id, run_id),
        )

    db._execute_write(reset_complete_projection)  # noqa: SLF001 - forces a backfill rerun.
    messages_after_backfill = db.get_conversation_message_read_model(
        session_id,
        include_storage_metadata=True,
    )

    assert [message["message_id"] for message in messages_after_backfill] == row_ids
    assert [message["content"] for message in messages_after_backfill] == [
        "Backfilled opening.",
        "Backfilled final.",
    ]


def test_team_transcript_single_segment_turn_stays_unchanged_without_reconstruction_marker(
    tmp_path: Path,
) -> None:
    db, session_id, participant_id = _create_team_session(tmp_path, "single-segment")
    run_id = "team-leader-run-single-segment"
    turn_id = "turn-single-segment"
    activity_id = f"chat:{session_id}"
    client_message_id = f"{turn_id}:assistant-segment:0"

    for event in [
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.start",
            seq=1,
            client_message_id=client_message_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.delta",
            seq=2,
            text="Only segment.",
            client_message_id=client_message_id,
            message_seq_in_run=1,
        ),
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.complete",
            seq=3,
            text="Only segment.",
            client_message_id=client_message_id,
            message_seq_in_run=1,
        ),
    ]:
        db.append_run_event(session_id, event)

    messages = db.get_conversation_message_read_model(session_id, include_storage_metadata=True)

    assert len(messages) == 1
    assert messages[0]["content"] == "Only segment."
    assert messages[0]["conversation_message_id"] == _conversation_message_id(session_id, run_id, "1")
    assert "reconstructed_from_raw" not in messages[0]["metadata"]


def test_team_transcript_message_seq_zero_uses_explicit_zero_identity(tmp_path: Path) -> None:
    db, session_id, participant_id = _create_team_session(tmp_path, "message-seq-zero")
    run_id = "team-leader-run-message-seq-zero"
    turn_id = "turn-message-seq-zero"
    activity_id = f"chat:{session_id}"
    client_message_id = f"{turn_id}:assistant-segment:0"

    event = db.append_run_event(
        session_id,
        _frame(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            participant_id=participant_id,
            activity_id=activity_id,
            event_type="message.complete",
            seq=1,
            text="Zero seq final.",
            client_message_id=client_message_id,
            message_seq_in_run=0,
        ),
    )

    expected = _conversation_message_id(session_id, run_id, "0")
    old_fallback = _conversation_message_id(session_id, run_id, client_message_id)
    messages = db.get_conversation_message_read_model(session_id, include_storage_metadata=True)

    assert event["_projected_message_id"] == expected
    assert expected != old_fallback
    assert messages[0]["conversation_message_id"] == expected
    assert messages[0]["content"] == "Zero seq final."
