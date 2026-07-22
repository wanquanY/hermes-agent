from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.methods import conversation_render_snapshot as render_snapshot


def test_render_snapshot_reads_state_through_store_components(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(
            "team-session-1",
            source="team_mission",
            conversation_kind="team",
        )
        db.participants.upsert_conversation_participant(
            conversation_session_id="team-session-1",
            participant_id="leader:conversation-1",
            role="leader",
            display_name="Leader",
        )
        db.activities.ensure_mission(
            conversation_id="team-session-1",
            mission_id="mission-1",
            status="running",
        )
        db.runs.append_event(
            "team-session-1",
            {"type": "message.delta", "run_id": "run-1", "payload": {}},
        )
        db.runs.append_event(
            "team-session-1",
            {"type": "message.complete", "run_id": "run-1", "payload": {}},
        )
        monkeypatch.setattr(render_snapshot, "_get_db", lambda: db)

        assert render_snapshot._route_kind_from_session_index(db, "team-session-1") == "team"
        assert [
            participant["participant_id"]
            for participant in render_snapshot._participants_for_session("team-session-1")
        ] == ["leader:conversation-1"]
        assert [
            activity["target_mission_id"]
            for activity in render_snapshot._mission_activities_for_session("team-session-1")
        ] == ["mission-1"]
        assert render_snapshot._run_event_session_last_seq(db, "team-session-1") == 2
        assert render_snapshot._run_event_activity_last_seq(db, "chat:team-session-1") == 2
    finally:
        db.close()


def test_team_activity_messages_are_a_separate_owner_projection(monkeypatch) -> None:
    monkeypatch.setattr(render_snapshot, "_get_db", lambda: object())
    monkeypatch.setattr(
        render_snapshot,
        "get_team_mission_node_runtime_history",
        lambda _db, params: {
            "messages": [
                {
                    "id": 6,
                    "message_id": "message-6",
                    "role": "user",
                    "participant_id": "member:verifier",
                    "text": "Verify the output.",
                    "timestamp": 1699,
                    "metadata": {"run_id": "run-verify"},
                },
                {
                    "id": 7,
                    "message_id": "message-7",
                    "role": "assistant",
                    "participant_id": "member:verify-output",
                    "text": "Verification passed.",
                    "timestamp": 1700,
                    "metadata": {"run_id": "run-verify"},
                }
            ],
            "source": {"session_id": "runtime-node-verify"},
        },
    )

    messages = render_snapshot._team_activity_messages(  # noqa: SLF001
        session_id="team-conversation-1",
        activities=[
            {
                "activity_id": "act-node:mission-1:verify-output",
                "kind": "agent_dispatch",
                "status": "completed",
                "graph_node_id": "verify-output",
                "target_mission_id": "mission-1",
                "owner_participant_id": "member:verifier",
            }
        ],
    )

    assert len(messages) == 2
    assert messages[0]["participant_id"] == "user"
    assert messages[1]["participant_id"] == "member:verifier"
    assert messages[1]["message_id"] == (
        "act-node:mission-1:verify-output:message-7"
    )
    assert messages[1]["source_message_id"] == "message-7"
    assert messages[1]["metadata"] == {
        "run_id": "run-verify",
        "activity_id": "act-node:mission-1:verify-output",
        "activity_kind": "agent_dispatch",
        "node_id": "verify-output",
        "mission_id": "mission-1",
    }


def test_team_activity_watermarks_include_non_terminal_node_journals(monkeypatch) -> None:
    monkeypatch.setattr(render_snapshot, "_get_db", lambda: object())
    monkeypatch.setattr(
        render_snapshot,
        "_run_event_activity_last_seq",
        lambda _db, activity_id: 73 if activity_id.startswith("act-node:") else 0,
    )
    monkeypatch.setattr(render_snapshot, "_run_event_session_last_seq", lambda *_args: 0)

    watermarks = render_snapshot._team_activity_watermarks(  # noqa: SLF001
        session_id="team-conversation-1",
        conversation={},
        mission={},
        team={},
        participants=[],
        mission_activities=[],
        activities=[
            {
                "activity_id": "act-node:mission-1:verify-output",
                "kind": "agent_dispatch",
                "status": "running",
            }
        ],
        is_running=True,
    )

    node = next(
        item for item in watermarks
        if item["activity_id"] == "act-node:mission-1:verify-output"
    )
    assert node["last_seq"] == 73
    assert node["terminal"] is False
    assert node["replay_policy"] == "replay_live"
