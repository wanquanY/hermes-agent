from __future__ import annotations

from pathlib import Path

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


def _db(tmp_path: Path) -> CliSessionStore:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv-1", source="tui", transient=False)
    db.sessions.create("conv-2", source="tui", transient=False)
    return db


def test_participant_write_requires_existing_session(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")

    with pytest.raises(ValueError, match="existing conversation session"):
        db.participants.ensure_participant(
            "conv-1",
            participant_id="member:m1",
            role="member",
        )


def test_ensure_participant_inserts_row(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.participants.ensure_participant(
        "conv-1",
        participant_id="member:m1",
        role="member",
        member_id="m1",
        agent_profile_id="profile-1",
        agent_profile_version_id="version-1",
        runtime_scope_key="member-chat:conv-1:m1",
        display_name="Alice",
        avatar="avatar://alice",
        metadata_json='{"source":"test"}',
    )

    assert row["conversation_session_id"] == "conv-1"
    assert row["participant_id"] == "member:m1"
    assert row["role"] == "member"
    assert row["member_id"] == "m1"
    assert row["agent_profile_id"] == "profile-1"
    assert row["agent_profile_version_id"] == "version-1"
    assert row["runtime_scope_key"] == "member-chat:conv-1:m1"
    assert row["display_name"] == "Alice"
    assert row["avatar"] == "avatar://alice"
    assert row["metadata_json"] == '{"source":"test"}'
    assert row["metadata"] == {"source": "test"}
    assert row["created_at"] > 0
    assert row["updated_at"] > 0


def test_ensure_participant_upsert_replaces_existing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    original = db.participants.ensure_participant(
        "conv-1",
        participant_id="member:m1",
        role="member",
        member_id="m1",
        agent_profile_id="profile-old",
        display_name="Alice",
        avatar="avatar://old",
        metadata_json='{"old":true}',
    )

    replaced = db.participants.ensure_participant(
        "conv-1",
        participant_id="member:m1",
        role="agent",
        agent_profile_id="profile-new",
        display_name="",
        avatar="avatar://new",
        metadata_json='{"new":true}',
    )

    assert replaced["created_at"] == original["created_at"]
    assert replaced["updated_at"] >= original["updated_at"]
    assert replaced["role"] == "agent"
    assert replaced["member_id"] == ""
    assert replaced["agent_profile_id"] == "profile-new"
    assert replaced["display_name"] == ""
    assert replaced["avatar"] == "avatar://new"
    assert replaced["metadata"] == {"new": True}
    assert len(db.participants.list_conversation_participants("conv-1")) == 1


def test_ensure_user_participant_uses_user_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)

    default_user = db.participants.ensure_user_participant("conv-1")
    named_user = db.participants.ensure_user_participant("conv-1", user_id="u1")

    assert default_user["participant_id"] == "user"
    assert default_user["role"] == "user"
    assert named_user["participant_id"] == "user:u1"
    assert named_user["role"] == "user"


def test_ensure_leader_participant_uses_leader_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.participants.ensure_leader_participant(
        "conv-1",
        team_id="team-1",
        leader_profile_id="leader-profile",
        display_name="Lead",
        avatar="avatar://lead",
    )

    assert row["participant_id"] == "leader:team-1"
    assert row["role"] == "leader"
    assert row["agent_profile_id"] == "leader-profile"
    assert row["runtime_scope_key"] == "team:team-1:leader-conversation"
    assert row["display_name"] == "Lead"
    assert row["avatar"] == "avatar://lead"


def test_ensure_member_participant_uses_member_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.participants.ensure_member_participant(
        "conv-1",
        member_id="m1",
        agent_profile_id="profile-1",
        display_name="Alice",
        avatar="avatar://alice",
    )

    assert row["participant_id"] == "member:m1"
    assert row["role"] == "member"
    assert row["member_id"] == "m1"
    assert row["agent_profile_id"] == "profile-1"
    assert row["display_name"] == "Alice"
    assert row["avatar"] == "avatar://alice"


def test_ensure_agent_participant_uses_agent_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.participants.ensure_agent_participant(
        "conv-1",
        agent_profile_id="profile-1",
        display_name="Agent",
        avatar="avatar://agent",
    )

    assert row["participant_id"] == "agent:profile-1"
    assert row["role"] == "agent"
    assert row["agent_profile_id"] == "profile-1"
    assert row["display_name"] == "Agent"
    assert row["avatar"] == "avatar://agent"


def test_get_participant_returns_none_when_missing(tmp_path: Path) -> None:
    db = _db(tmp_path)

    assert db.participants.get_participant("conv-1", "member:missing") is None


def test_list_conversation_participants_returns_all(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.participants.ensure_member_participant("conv-1", member_id="m1", display_name="Alice")
    db.participants.ensure_leader_participant("conv-1", team_id="team-1", display_name="Lead")
    db.participants.ensure_user_participant("conv-1", user_id="u1")
    db.participants.ensure_agent_participant("conv-2", agent_profile_id="profile-2")

    participants = db.participants.list_conversation_participants("conv-1")

    assert [row["participant_id"] for row in participants] == [
        "user:u1",
        "leader:team-1",
        "member:m1",
    ]


def test_update_participant_display_patches_partial(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.participants.ensure_participant(
        "conv-1",
        participant_id="member:m1",
        role="member",
        display_name="Alice",
        avatar="avatar://old",
        metadata_json='{"old":true}',
    )

    assert db.participants.update_participant_display(
        "conv-1",
        "member:m1",
        display_name="Alice Cooper",
        metadata_json='{"updated":true}',
    )

    row = db.participants.get_participant("conv-1", "member:m1")
    assert row is not None
    assert row["display_name"] == "Alice Cooper"
    assert row["avatar"] == "avatar://old"
    assert row["metadata_json"] == '{"updated":true}'
    assert row["metadata"] == {"updated": True}


def test_delete_participant_removes_row(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.participants.ensure_member_participant("conv-1", member_id="m1")

    assert db.participants.delete_participant("conv-1", "member:m1")
    assert db.participants.get_participant("conv-1", "member:m1") is None
    assert not db.participants.delete_participant("conv-1", "member:m1")


def test_participant_actor_state_has_isolated_namespace_and_cas_cursor(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = db.participants.ensure_participant(
        "conv-1",
        participant_id="member:m1",
        role="member",
    )
    second = db.participants.ensure_participant(
        "conv-1",
        participant_id="member:m2",
        role="member",
    )

    assert first["memory_namespace"] == "conversation:conv-1/participant:member:m1"
    assert second["memory_namespace"] == "conversation:conv-1/participant:member:m2"
    assert first["memory_namespace"] != second["memory_namespace"]

    advanced = db.participants.advance_actor_state(
        "conv-1",
        "member:m1",
        expected_memory_revision=0,
        transcript_cursor=42,
    )
    assert advanced["transcript_cursor"] == 42
    assert advanced["memory_revision"] == 1

    with pytest.raises(RuntimeError, match="revision conflict"):
        db.participants.advance_actor_state(
            "conv-1",
            "member:m1",
            expected_memory_revision=0,
            transcript_cursor=50,
        )

    untouched = db.participants.get_participant("conv-1", "member:m2")
    assert untouched["transcript_cursor"] == 0
    assert untouched["memory_revision"] == 0
