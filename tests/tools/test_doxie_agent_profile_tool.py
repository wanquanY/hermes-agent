import json

from tools.doxie_agent_profile_tool import (
    design_agent_profile,
    install_skill_to_agent_profile_draft,
    test_agent_profile as run_agent_profile_test,
)


def test_design_agent_profile_inspects_real_design_context():
    result = json.loads(design_agent_profile(operation="inspect_context"))

    assert result["doxie_event"] == "agent_profile_design_context"
    assert result["catalogKind"] == "overview"
    assert "web" in result["systemToolsets"]
    assert "doxie" not in result["systemToolsets"]
    assert result["architectureTemplates"]
    assert result["avatarAssets"]
    assert result["installedSkillsSummary"] is not None
    assert result["summary"]["toolsetCount"] >= len(result["systemToolsets"])
    assert result["rules"]["skillCatalogSource"] == "hermes.skills"
    assert result["rules"]["useSkillManageForCreation"] is True
    assert "installedSkills" not in result


def test_design_agent_profile_inspects_catalog_slice_on_demand():
    result = json.loads(
        design_agent_profile(
            operation="inspect_context",
            catalog_kind="toolsets",
            query="web",
            limit=5,
        )
    )

    assert result["catalogKind"] == "toolsets"
    assert result["page"]["limit"] == 5
    assert any(item["name"] == "web" for item in result["catalog"])
    assert all(item["name"] != "doxie" for item in result["catalog"])


def test_design_agent_profile_emits_composite_design_event():
    result = json.loads(
        design_agent_profile(
            operation="create",
            name="产品经理分身",
            recommended_toolsets=["web"],
            recommended_skills=["notion"],
            missing_capabilities=["技能未安装：bilibili-video"],
            skill_creation_plans=[{"id": "skill-plan-1", "name": "bilibili-video"}],
        )
    )

    assert result["doxie_event"] == "agent_profile_design_draft_requested"
    assert result["operation"] == "create"
    assert result["draft"]["recommendedToolsets"] == ["web"]
    assert result["draft"]["recommendedSkills"] == ["notion"]
    assert result["draft"]["missingCapabilities"] == ["技能未安装：bilibili-video"]
    assert result["draft"]["skillCreationPlans"] == [{"id": "skill-plan-1", "name": "bilibili-video"}]
    assert "skillInstallationPlans" not in result["draft"]


def test_design_agent_profile_saves_draft_through_backend_bridge(monkeypatch):
    monkeypatch.setenv("DOXIE_BACKEND_BRIDGE_URL", "http://127.0.0.1:1/api/doxie/invoke")
    monkeypatch.setenv("DOXIE_BACKEND_BRIDGE_TOKEN", "token")
    calls = []

    def fake_backend_call(command, payload):
        calls.append((command, payload))
        return {
            "id": "draft-1",
            "name": payload["name"],
            "recommendedToolsets": payload["recommendedToolsets"],
            "files": payload["files"],
        }

    monkeypatch.setattr("tools.doxie_agent_profile_tool._backend_call", fake_backend_call)
    result = json.loads(
        design_agent_profile(
            operation="create",
            name="产品经理分身",
            recommended_toolsets=["web"],
            soul_markdown="# 产品经理分身\n",
        )
    )

    assert calls[0][0] == "doxie_agent_profile_draft_create"
    assert calls[0][1]["name"] == "产品经理分身"
    assert result["doxie_event"] == "agent_profile_design_draft_saved"
    assert result["draftId"] == "draft-1"


def test_design_agent_profile_creates_revision_from_selected_target(monkeypatch):
    monkeypatch.setenv("DOXIE_BACKEND_BRIDGE_URL", "http://127.0.0.1:1/api/doxie/invoke")
    monkeypatch.setenv("DOXIE_BACKEND_BRIDGE_TOKEN", "token")
    monkeypatch.setattr(
        "tools.doxie_agent_profile_tool._session_design_context",
        lambda: {"designMode": "revision", "targetAgentProfileId": "profile-1"},
    )
    calls = []

    def fake_backend_call(command, payload):
        calls.append((command, payload))
        assert command == "doxie_agent_profile_draft_create_revision"
        return {
            "id": "draft-revision-1",
            "draftKind": "revision",
            "targetAgentProfileId": payload["agentProfileId"],
            "name": payload["name"],
        }

    monkeypatch.setattr("tools.doxie_agent_profile_tool._backend_call", fake_backend_call)
    result = json.loads(
        design_agent_profile(
            operation="upsert",
            name="产品经理分身增强版",
            recommended_toolsets=["web", "file"],
        )
    )

    assert calls[0][0] == "doxie_agent_profile_draft_create_revision"
    assert calls[0][1]["agentProfileId"] == "profile-1"
    assert calls[0][1]["targetAgentProfileId"] == "profile-1"
    assert calls[0][1]["designMode"] == "revision"
    assert result["operation"] == "revision"
    assert result["draftId"] == "draft-revision-1"


def test_install_skill_to_agent_profile_draft_copies_skill_and_updates_draft(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXIE_BACKEND_BRIDGE_URL", "http://127.0.0.1:1/api/doxie/invoke")
    monkeypatch.setenv("DOXIE_BACKEND_BRIDGE_TOKEN", "token")
    source_home = tmp_path / "source"
    source_skill = source_home / "skills" / "product" / "meeting-prd"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text(
        "---\nname: meeting-prd\ndescription: Meeting to PRD\n---\n\n# Meeting PRD\n",
        encoding="utf-8",
    )
    (source_skill / "templates").mkdir()
    (source_skill / "templates" / "prd.md").write_text("template\n", encoding="utf-8")
    target_home = tmp_path / "draft-home"
    calls = []

    def fake_backend_call(command, payload):
        calls.append((command, payload))
        if command == "doxie_agent_profile_draft_prepare_runtime":
            return {
                "prepared": True,
                "draft": {
                    "id": "draft-1",
                    "recommendedSkills": [],
                    "missingCapabilities": ["技能未安装：meeting-prd", "其他缺失"],
                },
                "runtimeProfileId": "draft:draft-1",
                "hermesHomePath": str(target_home),
            }
        if command == "doxie_agent_profile_draft_update":
            return {
                "id": payload["draftId"],
                "recommendedSkills": payload["recommendedSkills"],
                "missingCapabilities": payload["missingCapabilities"],
            }
        raise AssertionError(command)

    monkeypatch.setattr("tools.doxie_agent_profile_tool._backend_call", fake_backend_call)
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", source_home / "skills")

    result = json.loads(
        install_skill_to_agent_profile_draft(
            draft_id="draft-1",
            skill_name="meeting-prd",
        )
    )

    assert result["doxie_event"] == "agent_profile_draft_skill_installed"
    assert result["draftId"] == "draft-1"
    assert result["skillName"] == "meeting-prd"
    assert (target_home / "skills" / "product" / "meeting-prd" / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (target_home / "skills" / "product" / "meeting-prd" / "templates" / "prd.md").read_text(encoding="utf-8") == "template\n"
    assert calls[-1][0] == "doxie_agent_profile_draft_update"
    assert calls[-1][1]["recommendedSkills"] == ["meeting-prd"]
    assert calls[-1][1]["missingCapabilities"] == ["其他缺失"]


def test_test_agent_profile_runs_draft_through_delegation(monkeypatch, tmp_path):
    draft_home = tmp_path / "drafts" / "draft-1"
    memories = draft_home / "memories"
    memories.mkdir(parents=True)
    (draft_home / "SOUL.md").write_text("# 产品经理分身\n你负责输出 PRD。", encoding="utf-8")
    (memories / "MEMORY.md").write_text("偏好结构化输出。", encoding="utf-8")
    (draft_home / "config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - web\n    - file\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.doxie_agent_profile_tool._draft_home", lambda _draft_id: draft_home)

    calls = {}
    progress_events = []

    class ParentAgent:
        def tool_progress_callback(self, *args, **kwargs):
            progress_events.append((args, kwargs))

    def fake_delegate_task(**kwargs):
        calls.update(kwargs)
        kwargs["parent_agent"]._delegate_child_stream_delta_callback("实时")
        return json.dumps({"results": [{"status": "success", "summary": "这是 PRD 草稿。", "api_calls": 1}]})

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    parent_agent = ParentAgent()

    result = json.loads(
        run_agent_profile_test(
            draft_id="draft-1",
            message="帮我写一个 PRD",
            expectation="需要结构化",
            parent_agent=parent_agent,
        )
    )

    assert calls["goal"] == "帮我写一个 PRD"
    assert calls["toolsets"] == ["web", "file"]
    assert "SOUL.md" in calls["context"]
    assert result["doxie_event"] == "agent_profile_test_completed"
    assert result["draftId"] == "draft-1"
    assert result["status"] == "completed"
    assert result["response"] == "这是 PRD 草稿。"
    assert progress_events[0][0][:3] == ("subagent.output_delta", "test_agent_profile", "实时")
    assert not hasattr(parent_agent, "_delegate_child_stream_delta_callback")
