from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store


def test_agent_profile_registry_latest_profile_is_native_hermes_state(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    profile_home = tmp_path / "profiles" / "research-agent"

    profile = db.profiles.upsert_agent_profile(
        profile_id="agent-1",
        slug="research-agent",
        name="Research Agent",
        description="Research and verify.",
        category="工作",
        tags=["research", "review"],
        hermes_profile_name="research-agent",
        hermes_home_path=str(profile_home),
        default_model="gpt-5",
        default_provider="openai",
        default_toolsets=["file", "terminal"],
        recommended_skills=["search"],
        platform_base_toolsets_initialized=True,
        current_version_id="snapshot-2",
        current_version_number=2,
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T03:00:00Z",
    )
    draft = db.profiles.upsert_agent_profile_draft(
        draft_id="draft-1",
        draft_kind="revision",
        base_agent_profile_id=profile["id"],
        target_agent_profile_id=profile["id"],
        source_session_id="session-1",
        source_agent_profile_id="agent-default",
        name="Research Agent draft",
        tags=["research", "analysis"],
        recommended_toolsets=["file", "terminal"],
        recommended_skills=["search"],
        files={"soulMarkdown": "# Research Agent\n"},
        created_at="2026-06-14T02:00:00Z",
        updated_at="2026-06-14T02:00:00Z",
    )

    resolved = db.profiles.get_agent_profile(profile["id"])

    assert resolved["id"] == "agent-1"
    assert resolved["currentVersionId"] == "snapshot-2"
    assert resolved["agentProfileVersionId"] == "snapshot-2"
    assert resolved["runtimeHomePath"] == str(profile_home)
    assert resolved["runtimeScopeKey"] == "profile:agent-1"
    assert resolved["recommendedSkills"] == ["search"]
    assert db.profiles.get_agent_profile_by_slug("research-agent")["id"] == "agent-1"
    listed_profiles = db.profiles.list_agent_profiles()
    assert [item["id"] for item in listed_profiles] == ["agent-1"]
    assert listed_profiles[0]["runtimeHomePath"] == str(profile_home)
    assert listed_profiles[0]["runtimeScopeKey"] == "profile:agent-1"
    assert listed_profiles[0]["name"] == "Research Agent"

    drafts = db.profiles.list_agent_profile_drafts(source_session_id="session-1")
    assert drafts[0]["id"] == draft["id"]
    assert drafts[0]["draftKind"] == "revision"
    assert drafts[0]["recommendedToolsets"] == ["file", "terminal"]

    discarded = db.profiles.discard_agent_profile_draft("draft-1")
    assert discarded["status"] == "discarded"
    assert db.profiles.list_agent_profile_drafts(source_session_id="session-1") == []
    assert db.profiles.list_agent_profile_drafts(source_session_id="session-1", include_discarded=True)[0]["id"] == "draft-1"

    archived = db.profiles.archive_agent_profile("agent-1")
    assert archived["status"] == "archived"
    assert db.profiles.list_agent_profiles() == []
    assert [item["id"] for item in db.profiles.list_agent_profiles(include_archived=True)] == ["agent-1"]


def test_agent_profile_growth_summary_reads_latest_profile_home(tmp_path: Path):
    control_db = open_cli_session_store(tmp_path / "state.db")
    profile_home = tmp_path / "profiles" / "research-agent"
    (profile_home / "memories").mkdir(parents=True)
    (profile_home / "skills" / "research" / "search").mkdir(parents=True)
    (profile_home / "memories" / "MEMORY.md").write_text("- Project finding\n", encoding="utf-8")
    (profile_home / "memories" / "USER.md").write_text("- User preference\n", encoding="utf-8")
    (profile_home / "skills" / "research" / "search" / "SKILL.md").write_text("# Search\n", encoding="utf-8")

    control_db.profiles.upsert_agent_profile(
        profile_id="agent-1",
        slug="research-agent",
        name="Research Agent",
        hermes_profile_name="research-agent",
        hermes_home_path=str(profile_home),
        current_version_id="snapshot-1",
        current_version_number=1,
    )
    runtime_db = open_cli_session_store(profile_home / "state.db")
    runtime_db.sessions.create("session-1", source="tui")
    runtime_db.messages.append("session-1", "user", "Build a market research brief.")
    runtime_db.sessions.create("tool-session", source="tool")
    runtime_db.messages.append("tool-session", "user", "internal")

    growth = control_db.profiles.agent_profile_growth_summary("agent-1", range_preset="week")

    assert growth["projectMemoryItems"] == 1
    assert growth["userMemoryItems"] == 1
    assert growth["memoryItems"] == 2
    assert growth["skillCount"] == 1
    assert growth["sessionCount"] == 1
    assert growth["dailyGrowth"][-1]["memoryItems"] == 2
    assert growth["dailyGrowth"][-1]["skillCount"] == 1
    assert growth["dailyGrowth"][-1]["sessionCount"] == 1
    assert any(event["type"] == "session" and event["source"] == "state.db" for event in growth["recentEvents"])


def test_profile_registry_gateway_crud_is_latest_only(monkeypatch, tmp_path: Path):
    import importlib

    from tui_gateway import server

    profile_registry = importlib.import_module("tui_gateway.methods.profile_registry")
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(profile_registry, "_get_db", lambda: db)

    assert "profile.version.upsert" not in server._methods
    assert "profile.version.list" not in server._methods
    assert "profile.version.get" not in server._methods
    assert "profile.version.prune" not in server._methods

    upsert_response = server._methods["profile.upsert"](
        1,
        {
            "profile": {
                "id": "agent-1",
                "slug": "research-agent",
                "name": "Research Agent",
                "description": "Build and verify.",
                "hermesProfileName": "research-agent",
                "hermesHomePath": str(tmp_path / "profiles" / "research-agent"),
                "defaultToolsets": ["file", "terminal"],
                "recommendedSkills": ["search"],
                "currentVersionId": "snapshot-2",
                "currentVersionNumber": 2,
                "platformBaseToolsetsInitialized": True,
                "sourceKind": "dovie-public-market",
                "publicProfileId": "public-profile-1",
                "publicVersionId": "public-version-2",
                "publicContentHash": "sha256:abc",
                "marketInstalledAt": "2026-06-14T04:00:00Z",
                "templateId": "template-research",
                "templateVersion": "1.2.0",
                "templateSource": "system-template",
                "templateInstalledAt": "2026-06-14T03:00:00Z",
            }
        },
    )
    get_response = server._methods["profile.get"](2, {"slug": "research-agent"})
    list_response = server._methods["profile.list"](3, {})
    growth_response = server._methods["profile.growth.summary"](
        4,
        {
            "agentProfileId": "agent-1",
            "rangePreset": "week",
        },
    )
    draft_response = server._methods["profile.draft.upsert"](
        5,
        {
            "draft": {
                "id": "draft-1",
                "draftKind": "revision",
                "baseAgentProfileId": "agent-1",
                "targetAgentProfileId": "agent-1",
                "sourceSessionId": "session-1",
                "name": "Research Agent draft",
                "recommendedToolsets": ["file"],
                "recommendedSkills": ["search"],
                "files": {"soulMarkdown": "# Research Agent\n"},
            }
        },
    )
    draft_list_response = server._methods["profile.draft.list"](6, {"sourceSessionId": "session-1"})
    discarded_response = server._methods["profile.draft.discard"](7, {"draftId": "draft-1"})

    assert upsert_response["result"]["profile"]["id"] == "agent-1"
    assert upsert_response["result"]["profile"]["runtimeScopeKey"] == "profile:agent-1"
    assert upsert_response["result"]["profile"]["runtimeHomePath"].endswith("/profiles/research-agent")
    assert get_response["result"]["profile"]["slug"] == "research-agent"
    assert list_response["result"]["profiles"][0]["projection"] == "summary"
    assert list_response["result"]["profiles"][0]["agentProfileVersionId"] == "snapshot-2"
    assert list_response["result"]["profiles"][0]["runtimeScopeKey"] == "profile:agent-1"
    assert list_response["result"]["profiles"][0]["runtimeHomePath"].endswith("/profiles/research-agent")
    assert list_response["result"]["profiles"][0]["sourceKind"] == "dovie-public-market"
    assert list_response["result"]["profiles"][0]["publicProfileId"] == "public-profile-1"
    assert list_response["result"]["profiles"][0]["publicVersionId"] == "public-version-2"
    assert list_response["result"]["profiles"][0]["publicContentHash"] == "sha256:abc"
    assert list_response["result"]["profiles"][0]["metadata"]["marketInstall"]["installedAt"] == "2026-06-14T04:00:00Z"
    assert list_response["result"]["profiles"][0]["metadata"]["templateInstall"]["templateId"] == "template-research"
    assert list_response["result"]["profiles"][0]["metadata"]["templateInstall"]["templateVersion"] == "1.2.0"
    assert "files" not in list_response["result"]["profiles"][0]
    assert growth_response["result"]["growth"]["dailyGrowth"]
    assert draft_response["result"]["draft"]["id"] == "draft-1"
    assert draft_list_response["result"]["drafts"][0]["id"] == "draft-1"
    assert discarded_response["result"]["draft"]["status"] == "discarded"


def test_profile_upsert_uses_control_db_when_profile_context_has_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from tui_gateway import server

    control_home = tmp_path / "control-home"
    profile_home = tmp_path / "profiles" / "agent-a"
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))
    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})

    response = server.handle_request({
        "id": "profile-upsert",
        "method": "profile.upsert",
        "params": {
            "profile": {
                "id": "agent-a",
                "slug": "agent-a",
                "name": "Agent A",
                "hermesHomePath": str(profile_home),
            },
            "dovie_profile": {
                "id": "agent-a",
                "runtimeScopeKey": "profile:agent-a",
                "hermesHomePath": str(profile_home),
            },
        },
    })

    assert "error" not in response
    assert (control_home / "state.db").exists()
    assert not (profile_home / "state.db").exists()


def test_team_mission_control_home_falls_back_to_hermes_home(monkeypatch, tmp_path: Path):
    from hermes_team_mission.runtime.profile_scope import team_mission_control_home

    monkeypatch.delenv("DOVIE_HERMES_CONTROL_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "control-home"))

    assert team_mission_control_home() == str(tmp_path / "control-home")


def test_profile_registry_gateway_returns_validation_errors(monkeypatch, tmp_path: Path):
    import importlib

    from tui_gateway import server

    profile_registry = importlib.import_module("tui_gateway.methods.profile_registry")
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(profile_registry, "_get_db", lambda: db)

    response = server._methods["profile.upsert"](
        1,
        {
            "profile": {
                "id": "invalid",
                "slug": "invalid",
                "name": "",
                "hermesHomePath": str(tmp_path / "profiles" / "invalid"),
            }
        },
    )

    assert response["error"]["code"] == 4006
    assert "profile name required" in response["error"]["message"]
