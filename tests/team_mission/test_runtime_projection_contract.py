from __future__ import annotations

from hermes_agent.storage.cli_session_store import open_cli_session_store
from tui_gateway.services.team_mission_activity_events import (
    transport_event_for_subscription,
)


def _bound_planning_run(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-projection",
        conversation_id="conversation-projection",
        title="Projection contract",
        mode="supervised_mission",
        leader_session_id="team-session-projection",
    )
    db.upsert_team_mission_node(
        mission_id="mission-projection",
        node_id="team-mission:mission-projection:root",
        kind="root",
        title="Plan",
        status="running",
    )
    db.runs.upsert(
        run_id="run-planner",
        session_id="runtime-planner",
        runtime_scope_key="team:mission-projection:leader",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-projection",
        node_id="team-mission:mission-projection:root",
        run_id="run-planner",
        session_id="runtime-planner",
        runtime_scope_key="team:mission-projection:leader",
        role="leader",
    )
    return db


def _runtime_events(db, source_event_type: str) -> list[dict]:
    return [
        event
        for event in db.list_team_mission_run_events("mission-projection")
        if event.get("type") == "team_mission.runtime.event"
        and event.get("payload", {}).get("source_event_type") == source_event_type
    ]


def _activity_ledger_events(db, source_event_type: str) -> list[dict]:
    return [
        event
        for event in db.runs.list_events("team:mission:mission-projection:events")
        if event.get("type") == "team_mission.runtime.event"
        and event.get("payload", {}).get("source_event_type") == source_event_type
    ]


def test_unsequenced_planning_events_keep_every_canonical_node_and_edge(tmp_path) -> None:
    db = _bound_planning_run(tmp_path)

    for node_id in ("worker-create", "verifier-check", "synthesis-summary"):
        db.append_team_mission_run_event(
            mission_id="mission-projection",
            run_id="run-planner",
            event={
                "type": "mission.node.created",
                "payload": {
                    "node": {
                        "node_id": node_id,
                        "kind": "worker",
                        "title": node_id,
                        "status": "ready",
                    },
                },
            },
        )
    for edge_id, source, target in (
        ("edge-create-check", "worker-create", "verifier-check"),
        ("edge-check-summary", "verifier-check", "synthesis-summary"),
    ):
        db.append_team_mission_run_event(
            mission_id="mission-projection",
            run_id="run-planner",
            event={
                "type": "mission.edge.created",
                "payload": {
                    "edge": {
                        "edge_id": edge_id,
                        "from_node_id": source,
                        "to_node_id": target,
                        "kind": "depends_on",
                    },
                },
            },
        )

    node_events = _runtime_events(db, "mission.node.created")
    edge_events = _runtime_events(db, "mission.edge.created")

    assert [event["payload"]["source_payload"]["node"]["node_id"] for event in node_events] == [
        "worker-create",
        "verifier-check",
        "synthesis-summary",
    ]
    assert [event["payload"]["source_payload"]["edge"]["edge_id"] for event in edge_events] == [
        "edge-create-check",
        "edge-check-summary",
    ]
    source_seqs = [event["source_seq"] for event in [*node_events, *edge_events]]
    assert all(seq > 0 for seq in source_seqs)
    assert len(source_seqs) == len(set(source_seqs))
    assert len(_activity_ledger_events(db, "mission.node.created")) == 3
    assert len(_activity_ledger_events(db, "mission.edge.created")) == 2


def test_reasoning_and_thinking_text_survive_projection_and_compact_transport(tmp_path) -> None:
    db = _bound_planning_run(tmp_path)

    for event_type, text, offset in (
        ("reasoning.delta", "分析任务结构", 0),
        ("thinking.delta", "检查依赖关系", 6),
    ):
        db.append_team_mission_run_event(
            mission_id="mission-projection",
            run_id="run-planner",
            event={
                "type": event_type,
                "turn_id": "turn-planner",
                "payload": {
                    "mode": "append",
                    "text": text,
                    "delta": text,
                    "offset": offset,
                },
            },
        )

    for event_type, expected_text, expected_channel in (
        ("reasoning.delta", "分析任务结构", "reasoning"),
        ("thinking.delta", "检查依赖关系", "thinking"),
    ):
        events = _runtime_events(db, event_type)
        assert len(events) == 1
        event = events[0]
        text_stream = event["payload"]["text_stream"]
        assert text_stream["delta"] == expected_text
        assert text_stream["text"] == expected_text
        assert text_stream["event"] == "delta"
        assert text_stream["mode"] == "append"
        assert text_stream["channel"] == expected_channel
        assert text_stream["stream_id"].endswith(f":{expected_channel}")

        transported = transport_event_for_subscription(
            _activity_ledger_events(db, event_type)[0],
            "mission:mission-projection",
        )
        assert transported["payload"]["text_stream"]["delta"] == expected_text
        assert transported["payload"]["text"] == expected_text
        assert transported["payload"]["delta"] == expected_text
        assert "source_event" not in transported["payload"]
        assert "source_payload" not in transported["payload"]
