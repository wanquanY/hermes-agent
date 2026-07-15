from __future__ import annotations

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_reasoning_stream_fragments_coalesce_by_source(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        for seq, source, text in (
            (1, "provider_reasoning", "先分析"),
            (2, "provider_reasoning", "再验证"),
            (3, "other", "独立片段"),
        ):
            store.runs.append_event(
                "conversation-1",
                {
                    "type": "reasoning.delta",
                    "session_id": "runtime-1",
                    "run_id": "run-1",
                    "turn_id": "turn-1",
                    "seq": seq,
                    "payload": {"source": source, "text": text},
                },
            )

        events = store.runs.list_filtered_events(
            "conversation-1",
            event_types=["reasoning.delta"],
        )

        assert [event["seq"] for event in events] == [2, 3]
        assert [event["payload"]["text"] for event in events] == [
            "先分析再验证",
            "独立片段",
        ]
    finally:
        store.close()


def test_subagent_tool_event_starts_a_new_stream_segment(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        for event in (
            {
                "type": "subagent.output_delta",
                "seq": 1,
                "payload": {"subagent_id": "worker-1", "text": "第一"},
            },
            {
                "type": "subagent.output_delta",
                "seq": 2,
                "payload": {"subagent_id": "worker-1", "text": "段"},
            },
            {
                "type": "subagent.tool",
                "seq": 3,
                "payload": {"subagent_id": "worker-1", "tool_name": "terminal"},
            },
            {
                "type": "subagent.output_delta",
                "seq": 4,
                "payload": {"subagent_id": "worker-1", "text": "第二段"},
            },
        ):
            store.runs.append_event(
                "conversation-1",
                {
                    **event,
                    "session_id": "runtime-1",
                    "run_id": "run-1",
                },
            )

        events = store.runs.list_filtered_events(
            "conversation-1",
            event_type_prefix="subagent.",
        )

        assert [event["type"] for event in events] == [
            "subagent.output_delta",
            "subagent.tool",
            "subagent.output_delta",
        ]
        assert [event["seq"] for event in events] == [2, 3, 4]
        assert events[0]["payload"]["text"] == "第一段"
    finally:
        store.close()


def test_session_info_is_idempotent_and_preserves_execution_identity(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        event = {
            "type": "session.info",
            "session_id": "runtime-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:agent-default",
            "seq": 7,
            "payload": {
                "status": "starting",
                "model": "test-model",
                "provider": "test-provider",
            },
        }

        first = store.runs.append_event("conversation-1", event)
        duplicate = store.runs.append_event("conversation-1", event)
        state = store.runs.runtime_state("conversation-1")
        events = store.runs.list_events("conversation-1")

        assert first["session_id"] == "conversation-1"
        assert first["execution_session_id"] == "runtime-1"
        assert duplicate["_persistence_disposition"] == "duplicate_session_info"
        assert duplicate["seq"] == first["seq"]
        assert state["execution_session_id"] == "runtime-1"
        assert [saved["type"] for saved in events] == ["session.info"]
    finally:
        store.close()
