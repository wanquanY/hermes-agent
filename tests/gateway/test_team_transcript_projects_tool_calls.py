"""Team leader ``message.complete`` projection MUST write
``messages.tool_calls`` reconstructed from the ``tool_events`` read
model.

The team-mission ``message.complete`` payload only carries
``text_length`` / ``text_sha256`` / ``reasoning_*`` metadata — never
``tool_calls``. Without this reconstruction the projected assistant row
has a NULL ``tool_calls`` column, and the frontend's rehydration path
(which reads that column to render tool cards on reload / conversation
switch) silently drops the assistant's tool cards. Most visibly, the
``team_mission_start_task`` card disappears the moment streaming state
is discarded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_conversation_message_identity import (
    AssistantMessageIdentity,
    assistant_conversation_message_id_for,
)
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


def _seed_tool_event(
    db: SessionDB,
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    participant_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    seq_start: int,
    seq_last: int,
) -> None:
    def _insert(conn):
        conn.execute(
            """
            INSERT INTO tool_events (
                session_id, run_id, turn_id, tool_call_id, tool_name,
                status, started_at, updated_at, completed_at,
                seq_start, seq_last, arguments_json, result_text,
                participant_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                turn_id,
                tool_call_id,
                tool_name,
                "completed",
                float(seq_start),
                float(seq_last),
                float(seq_last),
                seq_start,
                seq_last,
                json.dumps(arguments, ensure_ascii=False),
                "{}",
                participant_id,
            ),
        )

    db._execute_write(_insert)  # noqa: SLF001 - test seeds read model directly.


def _message_complete_frame(
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    participant_id: str,
    activity_id: str,
    seq: int,
    text: str,
    message_seq_in_run: int = 1,
) -> dict[str, Any]:
    client_message_id = f"{turn_id}:assistant-segment:0"
    payload = {
        "activity_id": activity_id,
        "activityId": activity_id,
        "participant_id": participant_id,
        "participantId": participant_id,
        "activity_kind": "leader_chat",
        "activityKind": "leader_chat",
        "transcript_activity_kind": "leader_chat",
        "transcriptActivityKind": "leader_chat",
        "client_message_id": client_message_id,
        "clientMessageId": client_message_id,
        "message_seq_in_run": message_seq_in_run,
        "messageSeqInRun": message_seq_in_run,
        "text": text,
        "status": "complete",
    }
    return {
        "type": "message.complete",
        "session_id": f"runtime-{run_id}",
        "stored_session_id": session_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": "team:conv-1:leader-conversation",
        "activity_id": activity_id,
        "activityId": activity_id,
        "participant_id": participant_id,
        "participantId": participant_id,
        "seq": seq,
        "timestamp": float(seq),
        "payload": payload,
        "client_message_id": client_message_id,
        "clientMessageId": client_message_id,
        "message_seq_in_run": message_seq_in_run,
        "messageSeqInRun": message_seq_in_run,
    }


def _stored_row(db: SessionDB, session_id: str) -> dict[str, Any]:
    with db._lock:  # noqa: SLF001 - test verifies persisted row shape.
        row = db._conn.execute(  # noqa: SLF001
            """
            SELECT content, tool_calls, participant_id, metadata_json
            FROM messages
            WHERE session_id = ?
              AND role = 'assistant'
              AND active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else {}


def test_project_message_complete_writes_tool_calls_from_tool_events(tmp_path: Path) -> None:
    db, session_id, participant_id = _create_team_session(tmp_path, "conv-tool-calls")
    run_id = "team-leader-run-tool-calls"
    turn_id = "team-leader-turn-tool-calls"
    activity_id = f"chat:{session_id}"

    arguments = {
        "objective": "在 workspace 创建测试文件",
        "title": "测试团队任务 - 创建文件",
    }
    _seed_tool_event(
        db,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        tool_call_id="call_00_team_mission_start",
        tool_name="team_mission_start_task",
        arguments=arguments,
        seq_start=609,
        seq_last=610,
    )

    frame = _message_complete_frame(
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        activity_id=activity_id,
        seq=666,
        text="好的，重新发起「测试团队任务 - 创建文件」。",
    )

    def _project(conn):
        return RuntimeTranscriptWriter.project_message_complete_event_locked(
            db, conn, session_id=session_id, event=frame,
        )

    saved = db._execute_write(_project)  # noqa: SLF001
    assert saved, "projection must produce a row for a leader message.complete"

    row = _stored_row(db, session_id)
    assert row, "assistant row must be persisted to messages"
    assert row["content"], "text content must survive projection"

    tool_calls_raw = row["tool_calls"]
    assert tool_calls_raw, (
        "R2 invariant: messages.tool_calls must NOT be NULL when the "
        "run has associated tool_events — the frontend rehydrates tool "
        "cards from this column"
    )
    tool_calls = json.loads(tool_calls_raw)
    assert isinstance(tool_calls, list) and len(tool_calls) == 1
    call = tool_calls[0]
    assert call["id"] == "call_00_team_mission_start"
    assert call["type"] == "function"
    assert call["function"]["name"] == "team_mission_start_task"
    persisted_args = json.loads(call["function"]["arguments"])
    assert persisted_args == arguments, (
        "arguments must round-trip through tool_events → messages.tool_calls"
    )


def test_backfill_heals_historical_null_tool_calls_when_tool_events_exist(
    tmp_path: Path,
) -> None:
    """Historical assistant rows written before the projection fix have
    NULL tool_calls even when tool_events records exist for their run.
    ``backfill_projected_message_tool_calls_locked`` must repair them."""
    from hermes_team_mission.runtime.team_transcript_writer import (
        backfill_projected_message_tool_calls_locked,
    )

    db, session_id, participant_id = _create_team_session(tmp_path, "conv-backfill")
    run_id = "team-leader-run-backfill-historical"
    turn_id = "team-leader-turn-backfill-historical"

    # Simulate the old (broken) projection: assistant row inserted with
    # NO tool_calls column value, but metadata_json carries run_id/turn_id
    # so backfill can locate it.
    conversation_message_id = _conversation_message_id(session_id, run_id, "1")

    def _seed_broken_assistant(conn):
        conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, participant_id, timestamp,
                conversation_message_id, metadata_json, reasoning
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                "assistant",
                "已重新启动，任务正在异步执行中。",
                participant_id,
                1.0,
                conversation_message_id,
                json.dumps(
                    {"run_id": run_id, "turn_id": turn_id, "session_id": session_id},
                    ensure_ascii=False,
                ),
                "",
            ),
        )

    db._execute_write(_seed_broken_assistant)  # noqa: SLF001

    _seed_tool_event(
        db,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        tool_call_id="call_backfill_target",
        tool_name="team_mission_start_task",
        arguments={"title": "历史任务"},
        seq_start=100,
        seq_last=101,
    )

    def _do_backfill(conn):
        return backfill_projected_message_tool_calls_locked(
            db, conn, session_ids=[session_id],
        )

    repaired = db._execute_write(_do_backfill)  # noqa: SLF001
    assert repaired == 1

    row = _stored_row(db, session_id)
    tool_calls = json.loads(row["tool_calls"])
    assert tool_calls[0]["id"] == "call_backfill_target"


def _conversation_message_id(session_id: str, run_id: str, message_seq_in_run: str) -> str:
    return assistant_conversation_message_id_for(
        AssistantMessageIdentity(
            session_id=session_id,
            run_id=run_id,
            message_seq_in_run=message_seq_in_run,
        )
    )


def test_project_message_complete_leaves_tool_calls_null_when_no_tool_events(
    tmp_path: Path,
) -> None:
    """Plain text turn (no tool call): tool_calls column must be NULL,
    not the empty string / empty list — the frontend distinguishes."""
    db, session_id, participant_id = _create_team_session(tmp_path, "conv-text-only")
    run_id = "team-leader-run-text-only"
    turn_id = "team-leader-turn-text-only"
    activity_id = f"chat:{session_id}"

    frame = _message_complete_frame(
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        activity_id=activity_id,
        seq=42,
        text="小马，好的，我已经收到。",
    )

    def _project(conn):
        return RuntimeTranscriptWriter.project_message_complete_event_locked(
            db, conn, session_id=session_id, event=frame,
        )

    db._execute_write(_project)  # noqa: SLF001
    row = _stored_row(db, session_id)
    assert row["content"], "text-only turn still projects"
    assert row["tool_calls"] in (None, ""), (
        "no tool_events for this run → tool_calls must stay NULL/empty"
    )
