from __future__ import annotations

from tui_gateway.services.team_mission_activity_events import (
    transport_event_for_subscription,
)


def test_transport_preserves_tool_result_fields_without_source_payload_blob() -> None:
    projected = transport_event_for_subscription(
        {
            "type": "team_mission.runtime.event",
            "seq": 12,
            "payload": {
                "kind": "node.tool.completed",
                "source_event_type": "tool.complete",
                "source_event": {
                    "type": "tool.complete",
                    "run_id": "run-worker",
                    "payload": {
                        "tool_call_id": "call-write-file",
                        "name": "write_file",
                        "status": "completed",
                        "arguments": {"path": "/tmp/result.md"},
                        "summary": "wrote result file",
                        "result": {"bytes": 42},
                        "result_text": "wrote /tmp/result.md",
                        "duration_s": 0.25,
                    },
                },
                "subject": {
                    "type": "node",
                    "id": "node-worker",
                    "mission_id": "mission-1",
                    "runtime_conversation_session_id": "team:mission-1:node:node-worker",
                },
            },
        },
        "mission:mission-1",
    )

    payload = projected["payload"]

    assert payload["protocol"] == "team_mission.activity.transport.v1"
    assert payload["source_event_type"] == "tool.complete"
    assert payload["tool_call_id"] == "call-write-file"
    assert payload["name"] == "write_file"
    assert payload["status"] == "completed"
    assert payload["arguments"] == {"path": "/tmp/result.md"}
    assert payload["summary"] == "wrote result file"
    assert payload["result"] == {"bytes": 42}
    assert payload["result_text"] == "wrote /tmp/result.md"
    assert payload["duration_s"] == 0.25
    assert "source_event" not in payload
    assert "source_payload" not in payload


def test_transport_preserves_clarify_request_fields_without_source_payload_blob() -> None:
    projected = transport_event_for_subscription(
        {
            "type": "team_mission.runtime.event",
            "seq": 13,
            "payload": {
                "kind": "runtime.trace",
                "source_event_type": "clarify.request",
                "source_event": {
                    "type": "clarify.request",
                    "session_id": "runtime-worker",
                    "conversation_session_id": "team:mission-1:node:node-worker",
                    "runtime_scope_key": "profile:member-1",
                    "run_id": "run-worker",
                    "turn_id": "turn-worker",
                    "payload": {
                        "request_id": "clarify-1",
                        "question": "请选择首个版本范围",
                        "choices": ["A", "B"],
                    },
                },
                "subject": {
                    "type": "node",
                    "id": "node-worker",
                    "mission_id": "mission-1",
                    "runtime_conversation_session_id": "team:mission-1:node:node-worker",
                    "execution_session_id": "runtime-worker",
                    "runtime_scope_key": "profile:member-1",
                    "run_id": "run-worker",
                    "turn_id": "turn-worker",
                },
            },
        },
        "mission:mission-1",
    )

    payload = projected["payload"]

    assert payload["protocol"] == "team_mission.activity.transport.v1"
    assert payload["source_event_type"] == "clarify.request"
    assert payload["request_id"] == "clarify-1"
    assert payload["question"] == "请选择首个版本范围"
    assert payload["choices"] == ["A", "B"]
    assert "source_event" not in payload
    assert "source_payload" not in payload


def test_transport_preserves_node_speaker_identity_from_nested_source_event() -> None:
    projected = transport_event_for_subscription(
        {
            "type": "team_mission.runtime.event",
            "seq": 14,
            "payload": {
                "kind": "node.completed",
                "source_event_type": "message.complete",
                "source_event": {
                    "type": "message.complete",
                    "participant_id": "member:verifier",
                    "run_id": "run-verifier",
                    "payload": {
                        "text": "Verification passed.",
                        "run_context": {
                            "participant_id": "member:verifier",
                        },
                    },
                },
                "subject": {
                    "type": "node",
                    "id": "verify-output",
                    "mission_id": "mission-1",
                    "conversation_session_id": "team-conversation-1",
                },
            },
        },
        "act-node:mission-1:verify-output",
    )

    assert projected["participant_id"] == "member:verifier"
    assert projected["payload"]["participant_id"] == "member:verifier"
    assert projected["conversation_session_id"] == "team-conversation-1"
    assert projected["session_id"] == "team-conversation-1"
    assert projected["payload"]["conversation_session_id"] == "team-conversation-1"
    assert (
        projected["payload"]["subject"]["conversation_session_id"]
        == "team-conversation-1"
    )
