from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store


ACTIVITY_ID = "act-node:mission-1:node-a"


def _append_node_message(
    store,
    *,
    text: str,
    node_id: str,
    transcript_activity_kind: str = "mission_node",
) -> int:
    return store.messages.append(
        "node-session",
        role="assistant",
        content=text,
        metadata={
            "activity_id": ACTIVITY_ID,
            "node_id": node_id,
            "transcript_activity_kind": transcript_activity_kind,
        },
    )


def test_node_history_read_model_applies_domain_visibility_and_node_filter(
    tmp_path: Path,
) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        expected_id = _append_node_message(
            store,
            text="node-a",
            node_id="node-a",
        )
        _append_node_message(store, text="node-b", node_id="node-b")
        _append_node_message(
            store,
            text="main transcript",
            node_id="node-a",
            transcript_activity_kind="leader_chat",
        )

        messages, total = store.team_mission_node_history.list_messages(
            "node-session",
            activity_id=ACTIVITY_ID,
            node_id="node-a",
        )

        assert [message["id"] for message in messages] == [expected_id]
        assert [message["text"] for message in messages] == ["node-a"]
        assert total == 1
    finally:
        store.close()


def test_run_event_owner_supports_history_event_filters(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.sessions.create("node-session", source="team_mission")
        store.runs.upsert(
            run_id="run-1",
            session_id="node-session",
            status="running",
        )
        for seq, event_type in enumerate(
            ("mission.started", "custom.alpha", "custom.beta", "custom.gamma"),
            start=1,
        ):
            store.runs.append_event(
                "node-session",
                {
                    "type": event_type,
                    "conversation_session_id": "node-session",
                    "run_id": "run-1",
                    "seq": seq,
                    "payload": {"index": seq},
                },
            )

        events = store.runs.list_events(
            "node-session",
            event_types=("custom.alpha", "custom.beta", "custom.gamma"),
            exclude_event_types=("custom.beta",),
            exclude_event_type_prefixes=("mission.",),
            include_internal=True,
        )

        assert [event["type"] for event in events] == [
            "custom.alpha",
            "custom.gamma",
        ]
    finally:
        store.close()


def test_runtime_history_has_no_storage_internals() -> None:
    source = Path("hermes_team_mission/runtime/history.py").read_text()

    assert "._conn" not in source
    assert "._lock" not in source
    assert "decode_run_event_row" not in source
    assert "decode_message_content" not in source
    assert "hasattr(db," not in source
