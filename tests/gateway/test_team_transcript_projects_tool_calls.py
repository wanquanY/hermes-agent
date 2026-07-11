"""Team leader ``message.complete`` projection MUST NOT set
``messages.tool_calls`` alone — the team-transcript path has no matching
``role='tool'`` response projection, so a populated
``assistant.tool_calls`` produces a dangling turn that LLM providers
reject with HTTP 400 ("assistant message with tool_calls must be
followed by tool messages responding to each tool_call_id").

A prior version of the projection rebuilt ``tool_calls`` from the
``tool_events`` read model to fix UI rehydration; those tests are gone.
Until a proper tool-response projection or a frontend switch to reading
``tool_events`` directly lands, ``messages.tool_calls`` stays NULL for
the team transcript path, and the backfill helper is a NO-OP that also
clears any dangling ``tool_calls`` a prior heal left behind.
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
from hermes_agent.repositories.conversation_participant_repo import leader_participant_id
from hermes_team_mission.runtime.team_transcript_writer import RuntimeTranscriptWriter


def _create_team_session(tmp_path: Path, conversation_id: str) -> tuple[SessionDB, str, str]:
    db = SessionDB(tmp_path / "state.db")
    session_id = f"team-session-{conversation_id}"
    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=session_id,
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

    db._execute_write(_insert)  # noqa: SLF001


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
        "conversation_session_id": session_id,
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
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            """
            SELECT id, content, tool_calls, participant_id, metadata_json, timestamp
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


def test_project_message_complete_leaves_tool_calls_null_even_with_tool_events(
    tmp_path: Path,
) -> None:
    """LLM protocol invariant: the team projection MUST NOT populate
    ``tool_calls`` on its own, because the team-transcript path has no
    matching ``role='tool'`` response projection — a populated
    assistant.tool_calls with no follow-up is a dangling turn."""
    db, session_id, participant_id = _create_team_session(tmp_path, "conv-invariant")
    run_id = "team-leader-run-invariant"
    turn_id = "team-leader-turn-invariant"
    activity_id = f"chat:{session_id}"

    # Seed tool_events as if the leader had called team_mission_start_task.
    _seed_tool_event(
        db,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        tool_call_id="call_dangling_check",
        tool_name="team_mission_start_task",
        arguments={"title": "invariant test"},
        seq_start=1,
        seq_last=2,
    )

    frame = _message_complete_frame(
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        participant_id=participant_id,
        activity_id=activity_id,
        seq=10,
        text="已启动。",
    )

    def _project(conn):
        return RuntimeTranscriptWriter.project_message_complete_event_locked(
            db, conn, session_id=session_id, event=frame,
        )

    db._execute_write(_project)  # noqa: SLF001
    row = _stored_row(db, session_id)
    assert row["content"], "text still projects"
    assert row["tool_calls"] in (None, ""), (
        "LLM protocol invariant: team assistant row must NOT carry "
        "tool_calls without a matching role='tool' response projection"
    )


def test_project_message_complete_leaves_tool_calls_null_when_no_tool_events(
    tmp_path: Path,
) -> None:
    """Plain text turn (no tool call): tool_calls column must stay NULL."""
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
    assert row["tool_calls"] in (None, ""), "no tool_events, no tool_calls"


def test_backfill_clears_dangling_tool_calls_left_by_prior_heal(tmp_path: Path) -> None:
    """A prior version of ``backfill_projected_message_tool_calls_locked``
    populated ``messages.tool_calls`` for historical NULL rows. That
    produced dangling assistant turns and 400s. The current no-op
    backfill also defensively clears any tool_calls value that has no
    matching role='tool' follow-up row for the same session."""
    from hermes_team_mission.runtime.team_transcript_writer import (
        backfill_projected_message_tool_calls_locked,
    )

    db, session_id, participant_id = _create_team_session(tmp_path, "conv-dangling")
    run_id = "team-leader-run-dangling"
    turn_id = "team-leader-turn-dangling"
    conversation_message_id = assistant_conversation_message_id_for(
        AssistantMessageIdentity(
            session_id=session_id,
            run_id=run_id,
            message_seq_in_run="1",
        )
    )

    dangling_json = json.dumps(
        [{
            "id": "call_prior_heal",
            "type": "function",
            "function": {"name": "team_mission_start_task", "arguments": "{}"},
        }],
        ensure_ascii=False,
    )

    def _seed_dangling(conn):
        conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, participant_id, timestamp,
                conversation_message_id, metadata_json, reasoning, tool_calls
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                "assistant",
                "已启动。",
                participant_id,
                1.0,
                conversation_message_id,
                json.dumps({"run_id": run_id, "turn_id": turn_id, "session_id": session_id}),
                "",
                dangling_json,
            ),
        )

    db._execute_write(_seed_dangling)  # noqa: SLF001

    row_before = _stored_row(db, session_id)
    assert row_before["tool_calls"] == dangling_json, "seed must place dangling tool_calls"

    def _do_backfill(conn):
        return backfill_projected_message_tool_calls_locked(
            db, conn, session_ids=[session_id],
        )

    db._execute_write(_do_backfill)  # noqa: SLF001

    row_after = _stored_row(db, session_id)
    assert row_after["tool_calls"] in (None, ""), (
        "defensive clear: dangling tool_calls (no matching role='tool' "
        "follow-up) must be cleared so LLM requests stop 400ing"
    )


def test_backfill_preserves_tool_calls_when_role_tool_response_exists(tmp_path: Path) -> None:
    """If a matching role='tool' response row exists for the same
    session at or after the assistant timestamp, the tool_calls value
    is a valid pair and must be preserved."""
    from hermes_team_mission.runtime.team_transcript_writer import (
        backfill_projected_message_tool_calls_locked,
    )

    db, session_id, participant_id = _create_team_session(tmp_path, "conv-valid-pair")
    run_id = "team-leader-run-valid-pair"
    conversation_message_id = assistant_conversation_message_id_for(
        AssistantMessageIdentity(
            session_id=session_id,
            run_id=run_id,
            message_seq_in_run="1",
        )
    )
    valid_tool_calls = json.dumps(
        [{
            "id": "call_valid_pair",
            "type": "function",
            "function": {"name": "search_files", "arguments": "{}"},
        }],
        ensure_ascii=False,
    )

    def _seed_pair(conn):
        conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, participant_id, timestamp,
                conversation_message_id, metadata_json, reasoning, tool_calls
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, "assistant", "调用工具", participant_id, 1.0,
             conversation_message_id, "{}", "", valid_tool_calls),
        )
        conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, participant_id, timestamp,
                tool_call_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, "tool", "{\"result\": \"ok\"}", "", 2.0, "call_valid_pair"),
        )

    db._execute_write(_seed_pair)  # noqa: SLF001

    def _do_backfill(conn):
        return backfill_projected_message_tool_calls_locked(
            db, conn, session_ids=[session_id],
        )

    db._execute_write(_do_backfill)  # noqa: SLF001

    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            "SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant'",
            (session_id,),
        ).fetchone()
    assert row["tool_calls"] == valid_tool_calls, "paired tool_calls must survive"
