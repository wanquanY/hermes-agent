from hermes_team_mission.gateway.leader_report_runtime import _leader_report_prompt


def test_cancelled_leader_report_uses_authoritative_terminal_context():
    prompt = _leader_report_prompt(
        mission={
            "mission_id": "mission-cancelled",
            "title": "创建测试文件",
            "objective": "创建 test.txt",
            "status": "cancelled",
        },
        result={
            "result_id": "result-cancelled",
            "outcome": "cancelled",
            "summary_text": "Mission cancelled: no presentable output.",
            "node_results": [],
            "artifact_refs": [],
        },
        outcome="cancelled",
        summary_text="Mission cancelled: no presentable output.",
        artifact_refs=[],
    )

    assert "result context below is authoritative" in prompt
    assert "Do not query, re-check" in prompt
    assert "The task was cancelled. Say so plainly." in prompt
    assert "Do not claim it completed" in prompt
    assert "Do not call tools" in prompt
