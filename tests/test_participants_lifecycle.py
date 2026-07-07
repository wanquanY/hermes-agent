from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB


def _db(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv-1", source="tui", transient=False)
    db.create_session("conv-2", source="tui", transient=False)
    return db


def test_participant_write_requires_existing_session(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")

    with pytest.raises(ValueError, match="existing conversation session"):
        db.ensure_participant(
            "conv-1",
            participant_id="member:m1",
            role="member",
        )


def test_ensure_participant_inserts_row(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.ensure_participant(
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
    original = db.ensure_participant(
        "conv-1",
        participant_id="member:m1",
        role="member",
        member_id="m1",
        agent_profile_id="profile-old",
        display_name="Alice",
        avatar="avatar://old",
        metadata_json='{"old":true}',
    )

    replaced = db.ensure_participant(
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
    assert len(db.list_conversation_participants("conv-1")) == 1


def test_ensure_user_participant_uses_user_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)

    default_user = db.ensure_user_participant("conv-1")
    named_user = db.ensure_user_participant("conv-1", user_id="u1")

    assert default_user["participant_id"] == "user"
    assert default_user["role"] == "user"
    assert named_user["participant_id"] == "user:u1"
    assert named_user["role"] == "user"


def test_ensure_leader_participant_uses_leader_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.ensure_leader_participant(
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

    row = db.ensure_member_participant(
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

    row = db.ensure_agent_participant(
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

    assert db.get_participant("conv-1", "member:missing") is None


def test_list_conversation_participants_returns_all(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.ensure_member_participant("conv-1", member_id="m1", display_name="Alice")
    db.ensure_leader_participant("conv-1", team_id="team-1", display_name="Lead")
    db.ensure_user_participant("conv-1", user_id="u1")
    db.ensure_agent_participant("conv-2", agent_profile_id="profile-2")

    participants = db.list_conversation_participants("conv-1")

    assert [row["participant_id"] for row in participants] == [
        "user:u1",
        "leader:team-1",
        "member:m1",
    ]


def test_update_participant_display_patches_partial(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.ensure_participant(
        "conv-1",
        participant_id="member:m1",
        role="member",
        display_name="Alice",
        avatar="avatar://old",
        metadata_json='{"old":true}',
    )

    assert db.update_participant_display(
        "conv-1",
        "member:m1",
        display_name="Alice Cooper",
        metadata_json='{"updated":true}',
    )

    row = db.get_participant("conv-1", "member:m1")
    assert row is not None
    assert row["display_name"] == "Alice Cooper"
    assert row["avatar"] == "avatar://old"
    assert row["metadata_json"] == '{"updated":true}'
    assert row["metadata"] == {"updated": True}


def test_delete_participant_removes_row(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.ensure_member_participant("conv-1", member_id="m1")

    assert db.delete_participant("conv-1", "member:m1")
    assert db.get_participant("conv-1", "member:m1") is None
    assert not db.delete_participant("conv-1", "member:m1")
