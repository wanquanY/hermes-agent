from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.domain.identities import canonical_node_id
from hermes_team_mission.domain.member_perspective import transform_to_member_perspective
from hermes_team_mission.domain.runtime_identity import node_participant_id


def _db(tmp_path: Path, session_id: str = "team-session") -> CliSessionStore:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(session_id, source="team_mission", transient=False)
    return db


def _upsert_mission_with_node(
    db: CliSessionStore,
    *,
    mission_id: str,
    conversation_id: str,
    session_id: str,
    node_id: str,
    run_id: str,
    participant_id: str,
) -> None:
    db.upsert_team_mission(
        mission_id=mission_id,
        conversation_id=conversation_id,
        team_id="team-1",
        title=f"Mission {mission_id}",
        objective="Exercise graph identity and speaker identity separation",
        mode="supervised_mission",
        leader_session_id=session_id,
    )
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind="worker",
        title=f"Worker {node_id}",
        objective="Do work",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=session_id,
        execution_session_id=f"runtime-{run_id}",
        runtime_scope_key="member-chat:conversation-1:member-alpha",
        role="worker",
        metadata={"participant_id": participant_id},
    )


def _message_complete(
    *,
    session_id: str,
    run_id: str,
    seq: int,
    text: str,
    canonical_node_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text}
    if canonical_node_id:
        payload["canonical_node_id"] = canonical_node_id
        payload["canonicalNodeId"] = canonical_node_id
    return {
        "type": "message.complete",
        "session_id": session_id,
        "conversation_session_id": session_id,
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "seq": seq,
        "payload": payload,
    }


def test_canonical_node_id_is_graph_identity_only() -> None:
    misleading_node = {
        "node_id": "node-alpha",
        "canonical_node_id": "member:member-beta",
        "metadata": {"participant_id": "member:member-alpha"},
    }

    assert canonical_node_id("mission-A", "member:member-beta") == "mission-A:member:member-beta"
    assert node_participant_id(misleading_node) == "member:member-alpha"
    assert node_participant_id({"canonical_node_id": "member:member-beta"}) == ""


def test_speaker_resolution_uses_participant_id_not_node_id() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "Alpha has the build.",
            "metadata": {
                "participant_id": "member:member-alpha",
                "team_mission": {
                    "canonical_node_id": "mission-A:member:member-beta",
                    "display_name": "Graph Beta",
                },
            },
        }
    ]
    participants = [
        {"participant_id": "member:member-alpha", "display_name": "Alpha"},
        {"participant_id": "member:member-beta", "display_name": "Beta"},
    ]

    alpha_view = transform_to_member_perspective(
        messages,
        viewing_participant_id="member:member-alpha",
        participants=participants,
    )
    beta_view = transform_to_member_perspective(
        messages,
        viewing_participant_id="member:member-beta",
        participants=participants,
    )

    assert alpha_view[0]["metadata"]["team_member_identity_contract"] is True
    assert beta_view[0]["metadata"]["team_member_identity_contract"] is True
    assert alpha_view[1]["role"] == "assistant"
    assert alpha_view[1]["content"] == "Alpha has the build."
    assert beta_view[1]["role"] == "user"
    assert beta_view[1]["content"] == "[Alpha] Alpha has the build."
    assert beta_view[1]["metadata"]["transformed_speaker_pid"] == "member:member-alpha"


def test_render_snapshot_messages_speaker_field_uses_participant_id(tmp_path: Path, monkeypatch) -> None:
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")

    db = _db(tmp_path, "team-session-render")
    try:
        db.messages.append(
            "team-session-render",
            role="assistant",
            content="Alpha rendered from run events.",
            participant_id="member:member-alpha",
            metadata={
                "run_id": "run-alpha",
                "turn_id": "turn-run-alpha",
                "team_mission": {"canonical_node_id": "mission-A:member:member-beta"},
            },
        )
        db.runs.append_event(
            "team-session-render",
            _message_complete(
                session_id="team-session-render",
                run_id="run-alpha",
                seq=1,
                text="Alpha rendered from run events.",
                canonical_node_id="mission-A:member:member-beta",
            ),
            participant_id="member:member-alpha",
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setitem(
            server._methods,
            "team_mission.conversation.resolve",
            lambda rid, params: {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "conversation": {
                        "conversation_id": "conversation-1",
                        "conversation_session_id": "team-session-render",
                    },
                    "mission": {},
                    "team": {},
                    "graph": {},
                },
            },
        )

        response = server._methods["team_mission.conversation.render"](
            1,
            {"conversation_id": "conversation-1", "includeRunEvents": True},
        )

        message = response["result"]["messages"][0]
        assert message["participant_id"] == "member:member-alpha"
        assert message["participantId"] == "member:member-alpha"
        assert message["participant_id"] != "mission-A:member:member-beta"
    finally:
        db.close()


def test_member_node_across_two_missions_has_two_node_ids_but_one_participant_id(tmp_path: Path) -> None:
    db = _db(tmp_path, "team-session-cross-mission")
    try:
        db.participants.upsert_conversation_participant(
            conversation_session_id="team-session-cross-mission",
            participant_id="member:member-alpha",
            role="member",
            member_id="member-alpha",
            agent_profile_id="profile-alpha",
            runtime_scope_key="member-chat:conversation-1:member-alpha",
            display_name="Alpha",
        )
        for mission_id, run_id in (("mission-A", "run-A"), ("mission-B", "run-B")):
            _upsert_mission_with_node(
                db,
                mission_id=mission_id,
                conversation_id="conversation-1",
                session_id="team-session-cross-mission",
                node_id="shared-member-node",
                run_id=run_id,
                participant_id="member:member-alpha",
            )
            db.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=run_id,
                event=_message_complete(
                    session_id="team-session-cross-mission",
                    run_id=run_id,
                    seq=1,
                    text=f"{mission_id} complete",
                ),
            )

        node_a = db.get_team_mission_node("mission-A", "shared-member-node")
        node_b = db.get_team_mission_node("mission-B", "shared-member-node")
        events = {
            event["run_id"]: event
            for event in db.runs.list_events("team-session-cross-mission")
        }

        assert node_a["canonical_node_id"] == "mission-A:shared-member-node"
        assert node_b["canonical_node_id"] == "mission-B:shared-member-node"
        assert node_a["canonical_node_id"] != node_b["canonical_node_id"]
        assert events["run-A"]["participant_id"] == "member:member-alpha"
        assert events["run-B"]["participant_id"] == "member:member-alpha"
    finally:
        db.close()
