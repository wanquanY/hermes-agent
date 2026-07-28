from __future__ import annotations

from pathlib import Path

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.services.run_context_resolver import normalize_run_context_params


def test_direct_run_context_is_resolved_from_canonical_participant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    control_home = tmp_path / "control"
    execution_home = tmp_path / "profiles" / "profile-1"
    control_home.mkdir()
    execution_home.mkdir(parents=True)
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))
    db = open_cli_session_store(control_home / "state.db")
    try:
        db.sessions.create("conversation-1", source="tui")
        db.participants.ensure_user_participant("conversation-1")
        db.participants.ensure_agent_participant(
            "conversation-1",
            agent_profile_id="profile-1",
        )

        normalized = normalize_run_context_params(
            {
                "conversation_session_id": "conversation-1",
                "participant_id": "agent:profile-1",
                "agent_profile_id": "profile-1",
                "runtime_scope_key": "profile:profile-1",
                "activity_kind": "chat",
                "dovie_profile": {
                    "id": "profile-1",
                    "hermesHomePath": str(execution_home),
                },
            },
            conversation_session_id="conversation-1",
            db=db,
        )

        context = RunContext.from_payload(normalized["run_context_json"])
        assert context.conversation_session_id == "conversation-1"
        assert context.participant_id == "agent:profile-1"
        assert context.activity_id == "chat:conversation-1"
        assert context.execution_scope_key == "profile:profile-1"
        assert context.control_home == str(control_home)
        assert context.execution_home == str(execution_home)
    finally:
        db.close()


def test_existing_run_context_cannot_drift_from_submit_identity(tmp_path: Path) -> None:
    payload = RunContext(
        conversation_session_id="conversation-1",
        participant_id="agent:profile-1",
        activity_id="chat:conversation-1",
        activity_kind="chat",
        execution_scope_key="profile:profile-1",
        control_home=str(tmp_path),
        execution_home=str(tmp_path),
    ).to_payload()

    with pytest.raises(ValueError, match="participant_id does not match"):
        normalize_run_context_params(
            {
                "participant_id": "agent:profile-2",
                "run_context_json": payload,
            },
            conversation_session_id="conversation-1",
        )


def test_loose_participant_must_belong_to_conversation_roster(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("conversation-1", source="tui")
        db.participants.ensure_agent_participant(
            "conversation-1",
            agent_profile_id="profile-1",
        )

        with pytest.raises(ValueError, match="not in conversation participant roster"):
            normalize_run_context_params(
                {
                    "participant_id": "agent:profile-2",
                    "runtime_scope_key": "profile:profile-2",
                },
                conversation_session_id="conversation-1",
                db=db,
            )
    finally:
        db.close()
