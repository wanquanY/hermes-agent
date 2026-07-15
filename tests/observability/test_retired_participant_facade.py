from __future__ import annotations

from pathlib import Path

from hermes_agent.domain.participant_service import ParticipantService
from hermes_agent.storage.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_participant_facade_and_top_level_shim_are_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "participant_facade.py"
    ).exists()
    assert not (REPO_ROOT / "hermes_state_participants.py").exists()


def test_legacy_state_store_is_deleted() -> None:
    assert not (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").exists()


def test_cli_store_owns_participants_through_domain_service(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.sessions.create("conversation-1", source="test")
        assert isinstance(store.participants, ParticipantService)

        user = store.participants.ensure_user_participant("conversation-1")
        member = store.participants.ensure_member_participant(
            "conversation-1",
            member_id="member-1",
            agent_profile_id="profile-1",
            display_name="Member One",
        )

        assert user["participant_id"] == "user"
        assert member["participant_id"] == "member:member-1"
        assert {
            participant["participant_id"]
            for participant in store.participants.list_conversation_participants(
                "conversation-1"
            )
        } == {"user", "member:member-1"}
        assert store.participants.resolve_participant_id(
            conversation_session_id="conversation-1",
            member_id="member-1",
        ) == "member:member-1"
        assert not hasattr(store, "ensure_participant")
        assert not hasattr(store, "list_conversation_participants")
    finally:
        store.close()
