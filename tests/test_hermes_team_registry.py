from pathlib import Path

import pytest

from hermes_state import SessionDB


def test_agent_team_registry_is_native_hermes_state(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    profile = db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader v1",
        avatar="https://example.test/leader-v1.png",
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "profiles" / "leader"),
        current_version_id="snapshot-leader",
        current_version_number=1,
    )

    team = db.upsert_agent_team(
        team_id="team-1",
        name="Research Team",
        description="Research and verify.",
        lead_agent_profile_id="profile-leader",
        default_mode="supervised_mission",
        policy={"planApproval": "always"},
    )
    member = db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning", "quality"],
    )

    resolved = db.get_agent_team_with_members("team-1")

    assert team["id"] == "team-1"
    assert member["team_id"] == "team-1"
    assert resolved["name"] == "Research Team"
    assert "default_workspace_id" not in resolved
    assert resolved["members"][0]["id"] == "member-leader"
    assert resolved["members"][0]["capability_tags"] == ["planning", "quality"]
    assert [item["id"] for item in db.list_agent_teams()] == ["team-1"]
    summaries = db.list_agent_team_summaries()
    assert summaries[0]["id"] == "team-1"
    assert summaries[0]["member_count"] == 1
    assert summaries[0]["leader_member"]["agent_profile_id"] == "profile-leader"
    assert summaries[0]["leader_member"]["profile_avatar"] == "https://example.test/leader-v1.png"
    assert summaries[0]["leader_member"]["profile_name"] == "Leader v1"
    assert summaries[0]["display_members"][0]["profile_avatar"] == "https://example.test/leader-v1.png"
    assert "members" not in summaries[0]

    updated_member = db.upsert_agent_team_member(
        member_id="member-leader-v2",
        team_id="team-1",
        agent_profile_id="profile-leader",
        role="lead",
        capability_tags=["planning", "review"],
    )
    updated_members = db.list_agent_team_members("team-1")
    assert updated_member["id"] == "member-leader-v2"
    assert len(updated_members) == 1
    assert updated_members[0]["capability_tags"] == ["planning", "review"]

    archived = db.archive_agent_team("team-1")
    assert archived["status"] == "archived"
    assert db.list_agent_teams() == []
    assert [item["id"] for item in db.list_agent_teams(include_archived=True)] == ["team-1"]
    with pytest.raises(ValueError, match="team archived: team-1"):
        db.upsert_agent_team(
            team_id="team-1",
            name="Research Team v2",
            status="active",
        )
    with pytest.raises(ValueError, match="team archived: team-1"):
        db.upsert_agent_team_member(
            member_id="member-writer",
            team_id="team-1",
            agent_profile_id="profile-writer",
            role="writer",
        )
    with pytest.raises(ValueError, match="team archived: team-1"):
        db.delete_agent_team_member("member-leader-v2")


def test_team_registry_gateway_crud(monkeypatch, tmp_path: Path):
    import importlib

    from tui_gateway import server

    team_registry = importlib.import_module("tui_gateway.methods.team_registry")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_registry, "_get_db", lambda: db)
    profile = db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader v1",
        avatar="https://example.test/leader-v1.png",
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "profiles" / "leader"),
        current_version_id="snapshot-leader",
        current_version_number=1,
    )

    upsert_response = server._methods["team_registry.team.upsert"](
        1,
        {
            "team": {
                "id": "team-1",
                "name": "Engineering Team",
                "description": "Build and verify.",
                "defaultWorkspaceId": "workspace-legacy",
                "defaultMode": "supervised_mission",
                "policy": {"planApproval": "always"},
            },
            "members": [
                {
                    "id": "member-leader",
                    "agentProfileId": "profile-leader",
                    "agentProfileVersionId": "version-leader",
                    "role": "lead",
                    "capabilityTags": ["planning"],
                }
            ],
        },
    )
    get_response = server._methods["team_registry.team.get"](2, {"team_id": "team-1"})
    member_list_response = server._methods["team_registry.member.list"](3, {"team_id": "team-1"})
    full_member_loader = db.list_agent_team_members

    def fail_full_member_load(_team_id: str):
        raise AssertionError("summary team list must not hydrate full members")

    monkeypatch.setattr(db, "list_agent_team_members", fail_full_member_load)
    list_response = server._methods["team_registry.team.list"](4, {})
    monkeypatch.setattr(db, "list_agent_team_members", full_member_loader)
    member_response = server._methods["team_registry.member.upsert"](
        5,
        {
            "member": {
                "id": "member-builder",
                "teamId": "team-1",
                "agentProfileId": "profile-builder",
                "role": "builder",
            }
        },
    )
    removed_response = server._methods["team_registry.member.delete"](
        6,
        {"memberId": "member-builder"},
    )
    archived_response = server._methods["team_registry.team.archive"](
        7,
        {"teamId": "team-1"},
    )
    archived_team_upsert_response = server._methods["team_registry.team.upsert"](
        8,
        {
            "team": {
                "id": "team-1",
                "name": "Engineering Team v2",
                "status": "active",
            },
        },
    )
    archived_member_upsert_response = server._methods["team_registry.member.upsert"](
        9,
        {
            "member": {
                "id": "member-writer",
                "teamId": "team-1",
                "agentProfileId": "profile-writer",
                "role": "writer",
            },
        },
    )
    archived_member_delete_response = server._methods["team_registry.member.delete"](
        10,
        {"memberId": "member-leader"},
    )

    assert upsert_response["result"]["team"]["id"] == "team-1"
    assert "default_workspace_id" not in upsert_response["result"]["team"]
    assert upsert_response["result"]["team"]["members"][0]["id"] == "member-leader"
    assert get_response["result"]["team"]["name"] == "Engineering Team"
    assert "default_workspace_id" not in get_response["result"]["team"]
    assert get_response["result"]["team"]["projection"] == "detail"
    assert get_response["result"]["team"]["members"][0]["id"] == "member-leader"
    assert [team["id"] for team in list_response["result"]["teams"]] == ["team-1"]
    assert list_response["result"]["teams"][0]["projection"] == "summary"
    assert list_response["result"]["teams"][0]["member_count"] == 1
    assert list_response["result"]["teams"][0]["leader_member"]["agent_profile_id"] == "profile-leader"
    assert list_response["result"]["teams"][0]["leader_member"]["profileAvatar"] == "https://example.test/leader-v1.png"
    assert list_response["result"]["teams"][0]["displayMembers"][0]["profileName"] == "Leader v1"
    assert "members" not in list_response["result"]["teams"][0]
    assert member_list_response["result"]["members"][0]["id"] == "member-leader"
    assert member_response["result"]["member"]["id"] == "member-builder"
    assert removed_response["result"]["removed"]["id"] == "member-builder"
    assert archived_response["result"]["team"]["status"] == "archived"
    assert archived_team_upsert_response["error"]["code"] == 4006
    assert archived_team_upsert_response["error"]["message"] == "team archived: team-1"
    assert archived_member_upsert_response["error"]["code"] == 4006
    assert archived_member_upsert_response["error"]["message"] == "team archived: team-1"
    assert archived_member_delete_response["error"]["code"] == 4006
    assert archived_member_delete_response["error"]["message"] == "team archived: team-1"
