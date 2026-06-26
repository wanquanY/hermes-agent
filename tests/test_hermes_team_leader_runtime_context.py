from pathlib import Path

import pytest

from hermes_state import SessionDB
from hermes_team_leader_runtime_context import resolve_team_leader_runtime_params, resolve_team_runtime_members


def _seed_team_with_leader_profile(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    profile = db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader v1",
        description="Plan and route team work.",
        category="team",
        tags=["leader"],
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "profiles" / "leader"),
        default_model="gpt-5",
        default_provider="openai",
        default_toolsets=["file"],
        platform_base_toolsets_initialized=True,
        current_version_id="snapshot-leader",
        current_version_number=1,
    )
    db.upsert_agent_team(
        team_id="team-1",
        name="Team",
        lead_agent_profile_id=profile["id"],
        default_mode="supervised_mission",
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id=profile["id"],
        agent_profile_version_id="snapshot-leader",
        role="lead",
        capability_tags=["planning"],
    )
    worker_profile = db.upsert_agent_profile(
        profile_id="profile-worker",
        slug="worker",
        name="Worker v1",
        description="Execute assigned work.",
        category="team",
        tags=["worker"],
        hermes_profile_name="worker",
        hermes_home_path=str(tmp_path / "profiles" / "worker"),
        default_model="gpt-5",
        default_provider="openai",
        default_toolsets=["terminal"],
        platform_base_toolsets_initialized=True,
        current_version_id="snapshot-worker",
        current_version_number=1,
    )
    db.upsert_agent_team_member(
        member_id="member-worker",
        team_id="team-1",
        agent_profile_id=worker_profile["id"],
        agent_profile_version_id="snapshot-worker",
        role="worker",
        capability_tags=["implementation"],
    )
    return db


def test_team_leader_runtime_context_resolves_from_team_registry(tmp_path: Path):
    db = _seed_team_with_leader_profile(tmp_path)

    resolution = resolve_team_leader_runtime_params(
        {
            "team_id": "team-1",
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "text": "start",
        },
        db=db,
    )

    params = resolution.params
    assert params["runtime_scope_key"] == "team:conversation-1:leader-conversation"
    assert params["profile_runtime_scope_key"] == "profile:profile-leader"
    assert params["agent_profile_id"] == "profile-leader"
    assert params["agent_profile_version_id"] == "snapshot-leader"
    assert params["dovie_profile"]["hermesHomePath"].endswith("/profiles/leader")
    assert params["members"][0]["profile_id"] == "profile-leader"
    assert params["members"][0]["dovie_profile"]["runtimeScopeKey"] == "profile:profile-leader"
    assert resolution.leader_runtime_context["runtimeScopeKey"] == "team:conversation-1:leader-conversation"


def test_team_leader_runtime_context_rejects_profile_only_payload(tmp_path: Path):
    db = _seed_team_with_leader_profile(tmp_path)

    with pytest.raises(ValueError, match="team_id required"):
        resolve_team_leader_runtime_params(
            {
                "conversation_id": "conversation-1",
                "agentProfileId": "profile-leader",
                "agentProfileVersionId": "version-leader",
                "dovie_profile": {
                    "id": "profile-leader",
                    "agentProfileVersionId": "snapshot-leader",
                    "runtimeScopeKey": "profile:profile-leader",
                    "hermesHomePath": str(tmp_path / "profiles" / "leader"),
                },
            },
            db=db,
        )


def test_team_runtime_members_resolve_all_member_profiles_on_demand(tmp_path: Path):
    db = _seed_team_with_leader_profile(tmp_path)

    members = resolve_team_runtime_members(
        {
            "team_id": "team-1",
            "mission_id": "mission-1",
        },
        db=db,
    )

    by_profile = {member["profile_id"]: member for member in members}
    assert by_profile["profile-leader"]["runtime_scope_key"] == "profile:profile-leader"
    assert by_profile["profile-leader"]["dovie_profile"]["hermesHomePath"].endswith("/profiles/leader")
    assert by_profile["profile-worker"]["runtime_scope_key"] == "profile:profile-worker"
    assert by_profile["profile-worker"]["dovie_profile"]["hermesHomePath"].endswith("/profiles/worker")
    assert by_profile["profile-worker"]["default_toolsets"] == ["terminal"]
