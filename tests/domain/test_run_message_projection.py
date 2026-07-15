from __future__ import annotations

from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_conversation_message_identity import AssistantMessageIdentity
from hermes_conversation_message_identity import (
    assistant_conversation_message_id_for,
)


def _message_complete_event(session_id: str) -> dict:
    return {
        "type": "message.complete",
        "session_id": "runtime-member-1",
        "conversation_session_id": session_id,
        "run_id": "member-run-1",
        "turn_id": "member-turn-1",
        "message_seq_in_run": 1,
        "runtime_scope_key": "member-chat:conversation-1:member-1",
        "participant_id": "member:member-1",
        "payload": {
            "status": "complete",
            "text": "member response",
            "activity_id": "act-member_chat:conversation-1:member-1",
        },
    }


def test_message_complete_projects_canonical_team_transcript_in_same_write(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        session_id = "conversation-1"
        store.sessions.create(session_id, source="team_mission")
        expected_message_id = assistant_conversation_message_id_for(
            AssistantMessageIdentity(
                session_id=session_id,
                run_id="member-run-1",
                message_seq_in_run="1",
            )
        )

        saved = store.runs.append_event(session_id, _message_complete_event(session_id))
        messages = store.messages.all_as_conversation(
            session_id,
            include_storage_metadata=True,
        )

        assert saved["_projected_message_id"] == expected_message_id
        assert len(messages) == 1
        assert messages[0]["conversation_message_id"] == expected_message_id
        assert messages[0]["participant_id"] == "member:member-1"
        assert messages[0]["content"] == "member response"
        assert messages[0]["metadata"]["transcript_activity_kind"] == "member_direct_chat"
        event_row = store._conn.execute(  # noqa: SLF001
            "SELECT projected_message_id, projection_state FROM run_events "
            "WHERE session_id = ? AND seq = ?",
            (session_id, saved["seq"]),
        ).fetchone()
        assert tuple(event_row) == (expected_message_id, "projected")
    finally:
        store.close()


def test_late_worker_flush_merges_into_projected_message(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        session_id = "conversation-1"
        store.sessions.create(session_id, source="team_mission")
        store.runs.append_event(session_id, _message_complete_event(session_id))

        store.messages.append(
            session_id,
            "assistant",
            "member response",
            participant_id="member:member-1",
            reasoning="native reasoning",
            metadata={"run_id": "member-run-1", "turn_id": "member-turn-1"},
        )
        messages = store.messages.all_as_conversation(
            session_id,
            include_storage_metadata=True,
        )

        assert len(messages) == 1
        assert messages[0]["content"] == "member response"
        assert messages[0]["reasoning"] == "native reasoning"
    finally:
        store.close()
