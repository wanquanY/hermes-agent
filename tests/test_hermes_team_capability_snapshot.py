from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store


def _source_packet(description: str = "A balanced engineering team") -> dict:
    return {
        "teamId": "team-1",
        "teamRevision": "rev-1",
        "team": {
            "name": "Launch Team",
            "description": description,
            "defaultMode": "supervised_mission",
            "policy": {"maxParallelNodes": 3},
        },
        "members": [
            {
                "memberId": "member-builder",
                "agentProfileId": "profile-builder",
                "agentProfileVersionId": "version-builder",
                "displayName": "推进工程师",
                "role": "engineer",
                "capabilityTags": ["code", "automation"],
                "profile": {
                    "description": "负责代码实现、终端命令、文件编辑和测试自动化。",
                    "tags": ["engineering"],
                    "defaultToolsets": ["terminal", "file", "git"],
                    "recommendedSkills": ["software-development"],
                },
            },
            {
                "memberId": "member-reviewer",
                "agentProfileId": "profile-reviewer",
                "agentProfileVersionId": "version-reviewer",
                "displayName": "星舰质检官",
                "role": "reviewer",
                "capabilityTags": ["qa", "verification"],
                "profile": {
                    "description": "负责质量验收、事实核验和交付审查。",
                    "tags": ["quality"],
                    "defaultToolsets": ["file"],
                    "recommendedSkills": ["test-review"],
                },
            },
        ],
    }


def test_team_capability_snapshot_is_canonical_hermes_state(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")

    snapshot = db.team_capabilities.resolve(source_packet=_source_packet())
    reused = db.team_capabilities.resolve(source_packet=_source_packet())

    assert snapshot["snapshot_id"] == reused["snapshot_id"]
    assert snapshot["team_id"] == "team-1"
    assert snapshot["version"] == 1
    assert snapshot["status"] == "ready"
    assert snapshot["team_profile"]["collaboration_mode"] == "supervised_mission"
    assert any(
        score["axis_id"] == "engineering" and score["score"] >= 3
        for score in snapshot["team_profile"]["radar_scores"]
    )
    builder = snapshot["member_profiles"][0]
    assert builder["member_id"] == "member-builder"
    assert "适合承担" in builder["best_for_tasks"][0]
    assert any(score["axis_id"] == "engineering" and score["score"] >= 3 for score in builder["radar_scores"])


def test_team_capability_snapshot_stale_then_refresh_creates_new_version(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")

    first = db.team_capabilities.resolve(source_packet=_source_packet("Initial team"))
    stale = db.team_capabilities.resolve(source_packet=_source_packet("Changed team"))
    refreshed = db.team_capabilities.resolve(source_packet=_source_packet("Changed team"), force_refresh=True)

    assert stale["snapshot_id"] == first["snapshot_id"]
    assert stale["status"] == "stale"
    assert stale["stale_reason"] == "source packet changed"
    assert refreshed["snapshot_id"] != first["snapshot_id"]
    assert refreshed["version"] == 2
    assert refreshed["status"] == "ready"


def test_team_capability_snapshot_binding_is_pinned_to_mission(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Build",
        objective="Build a tool",
        mode="supervised_mission",
    )
    snapshot = db.team_capabilities.resolve(source_packet=_source_packet())

    binding = db.team_capabilities.bind(
        mission_id="mission-1",
        conversation_id="conversation-1",
        snapshot_id=snapshot["snapshot_id"],
    )

    assert binding["mission_id"] == "mission-1"
    assert binding["snapshot_id"] == snapshot["snapshot_id"]
    assert db.team_capabilities.get_bound("mission-1")["snapshot_id"] == snapshot["snapshot_id"]
    mission = db.team_mission_graphs.get_team_mission_graph("mission-1")["mission"]
    assert mission["metadata"]["team_capability_snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
