from __future__ import annotations

from pathlib import Path

from hermes_state import SessionDB
from hermes_team_mission.domain.identities import canonical_node_id


def _db_with_bound_node(
    tmp_path: Path,
    *,
    mission_id: str = "mission-A",
    node_id: str = "node-X",
    run_id: str = "run-1",
    session_id: str = "session-1",
    participant_id: str = "member:alice",
) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id=mission_id,
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Exercise canonical node identity",
        mode="supervised_mission",
        leader_session_id=session_id,
    )
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind="worker",
        title="Worker",
        objective="Do work",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=session_id,
        execution_session_id="runtime-1",
        runtime_scope_key="member-chat:conversation-1:alice",
        role="worker",
        metadata={"participant_id": participant_id},
    )
    return db


def test_canonical_node_id_format():
    assert canonical_node_id("mission-A", "node-X") == "mission-A:node-X"


def test_canonical_node_id_distinct_across_missions():
    assert canonical_node_id("mission-A", "node-X") != canonical_node_id("mission-B", "node-X")


def test_event_with_participant_and_canonical_node_id_both_present(tmp_path: Path):
    db = _db_with_bound_node(tmp_path)

    db.append_team_mission_run_event(
        mission_id="mission-A",
        run_id="run-1",
        event={
            "type": "message.complete",
            "payload": {"text": "done", "node_id": "node-X"},
        },
    )

    event = db.list_run_events("session-1", run_id="run-1")[0]
    payload = event["payload"]
    assert event["participant_id"] == "member:alice"
    assert payload["participant_id"] == "member:alice"
    assert event["node_id"] == "node-X"
    assert payload["node_id"] == "node-X"
    assert event["canonical_node_id"] == "mission-A:node-X"
    assert payload["canonical_node_id"] == "mission-A:node-X"


def test_legacy_node_id_still_usable_for_in_mission_lookup(tmp_path: Path):
    db = _db_with_bound_node(tmp_path, mission_id="mission-A", session_id="session-A")
    db.create_session("session-B", source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id="mission-B",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission B",
        objective="Reuse local node ids",
        mode="supervised_mission",
        leader_session_id="session-B",
    )
    db.upsert_team_mission_node(
        mission_id="mission-B",
        node_id="node-X",
        kind="worker",
        title="Worker B",
        objective="Do other work",
        status="running",
    )

    node_a = db.get_team_mission_node("mission-A", "node-X")
    node_b = db.get_team_mission_node("mission-B", "node-X")

    assert node_a["node_id"] == "node-X"
    assert node_b["node_id"] == "node-X"
    assert node_a["canonical_node_id"] == "mission-A:node-X"
    assert node_b["canonical_node_id"] == "mission-B:node-X"
