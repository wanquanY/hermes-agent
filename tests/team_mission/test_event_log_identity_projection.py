from __future__ import annotations

from hermes_team_mission.state.event_log import projection_event


def _project(source_event: dict, **identity: str) -> dict:
    return projection_event(
        source_event,
        {
            "mission_id": "mission-1",
            "conversation_id": "conversation-1",
            "conversation_session_id": "stored-session-1",
            **identity,
        },
        source_seq=1,
        mission_seq=2,
    )


def test_projection_uses_durable_conversation_identity_for_session_fields() -> None:
    projected = _project(
        {
            "type": "message.delta",
            "session_id": "runtime-session-1",
            "execution_session_id": "runtime-session-1",
            "payload": {"delta": "hello"},
        }
    )

    assert projected["session_id"] == "stored-session-1"
    assert projected["conversation_session_id"] == "stored-session-1"
    assert projected["payload"]["session_id"] == "stored-session-1"
    assert projected["payload"]["conversation_session_id"] == "stored-session-1"
    assert projected["execution_session_id"] == "runtime-session-1"
    assert projected["payload"]["execution_session_id"] == "runtime-session-1"


def test_projection_carries_client_message_identity_in_text_stream_contract() -> None:
    projected = _project(
        {
            "type": "message.delta",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "payload": {
                "delta": "hello",
                "mode": "append",
                "offset": 0,
                "client_message_id": "turn-1:assistant-segment:0",
            },
        }
    )

    assert projected["payload"]["text_stream"]["client_message_id"] == (
        "turn-1:assistant-segment:0"
    )


def test_projection_does_not_infer_execution_identity_from_session_id() -> None:
    projected = _project(
        {
            "type": "message.complete",
            "session_id": "stored-session-1",
            "payload": {"text": "done"},
        }
    )

    assert projected["session_id"] == "stored-session-1"
    assert "execution_session_id" not in projected
    assert "execution_session_id" not in projected["payload"]
    assert "execution_session_id" not in projected["payload"]["subject"]


def test_projection_does_not_promote_session_key_to_runtime_conversation_identity() -> None:
    projected = _project(
        {
            "type": "message.delta",
            "payload": {
                "delta": "hello",
                "session_key": "legacy-runtime-session",
            },
        }
    )

    subject = projected["payload"]["subject"]
    assert subject["conversation_session_id"] == "stored-session-1"
    assert "runtime_conversation_session_id" not in subject
    assert "source_session_id" not in subject
    assert "conversation_conversation_session_id" not in subject
