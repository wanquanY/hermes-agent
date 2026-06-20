import json
from pathlib import Path

from hermes_state import SessionDB


def test_team_mission_graph_and_run_binding_are_native_hermes_state(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    mission = db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Research AI direction",
        objective="Find the latest AI development directions",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
        mode="supervised_mission",
    )
    node = db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-leader",
        kind="root",
        title="Plan mission",
        objective="Create a mission graph",
        status="running",
        runtime_scope_key="team:mission-1:leader",
    )
    edge = db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id="node-leader",
        to_node_id="node-worker",
    )
    run = db.upsert_run(
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        status="running",
    )
    binding = db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-leader",
        run_id="run-leader",
        session_id="session-leader",
        runtime_session_id="runtime-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )

    graph = db.get_team_mission_graph("mission-1")
    graph_node = graph["nodes"][0]
    direct_node = db.get_team_mission_node("mission-1", "node-leader")
    durable_node = db._conn.execute(  # noqa: SLF001 - contract test for node-owned runtime identity.
        """
        SELECT canonical_node_id, task_frame_id, runtime_stable_session_id,
               runtime_session_id, runtime_scope_key
        FROM team_mission_nodes
        WHERE mission_id = ? AND node_id = ?
        """,
        ("mission-1", "node-leader"),
    ).fetchone()

    assert mission["mission_id"] == "mission-1"
    assert node["node_id"] == "node-leader"
    assert edge["from_node_id"] == "node-leader"
    assert run["run_id"] == "run-leader"
    assert binding["role"] == "leader"
    assert graph["mission"]["mission_id"] == "mission-1"
    assert graph["mission"]["conversation_id"] == "mission-1"
    assert graph["conversation"]["conversation_id"] == "mission-1"
    assert graph["conversation"]["active_mission_id"] == "mission-1"
    assert [item["node_id"] for item in graph["nodes"]] == ["node-leader"]
    assert [item["run_id"] for item in graph["run_bindings"]] == ["run-leader"]
    assert graph_node["run_id"] == "run-leader"
    assert graph_node["stored_session_id"] == "session-leader"
    assert graph_node["actual_stable_session_id"] == "session-leader"
    assert graph_node["runtime_session_id"] == "runtime-leader"
    assert graph_node["runtime_scope_key"] == "team:mission-1:leader"
    assert graph_node["runtime_binding"]["run_id"] == "run-leader"
    assert direct_node["run_id"] == "run-leader"
    assert direct_node["stored_session_id"] == "session-leader"
    assert direct_node["runtime_session_id"] == "runtime-leader"
    assert durable_node["canonical_node_id"] == "mission-1:node-leader"
    assert durable_node["task_frame_id"] == "mission-frame:mission-1"
    assert durable_node["runtime_stable_session_id"] == "session-leader"
    assert durable_node["runtime_session_id"] == "runtime-leader"
    assert durable_node["runtime_scope_key"] == "team:mission-1:leader"


def test_team_mission_node_runtime_projection_uses_latest_run_binding(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        objective="Run node twice",
        mode="supervised_mission",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Complete task",
        status="running",
        runtime_scope_key="profile:worker",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-a-old",
        session_id="session-old",
        runtime_session_id="runtime-old",
        runtime_scope_key="profile:worker",
        role="worker",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-z-new",
        session_id="session-new",
        runtime_session_id="runtime-new",
        runtime_scope_key="profile:worker",
        role="worker",
    )

    graph_node = db.get_team_mission_graph("mission-1")["nodes"][0]

    assert graph_node["run_id"] == "run-z-new"
    assert graph_node["stored_session_id"] == "session-new"
    assert graph_node["runtime_session_id"] == "runtime-new"
    assert graph_node["runtime_binding"]["run_id"] == "run-z-new"


def test_team_mission_conversation_is_canonical_and_resolvable(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Research",
        objective="Do research",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-1",
        kind="root",
        title="Plan",
        status="completed",
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    user_message_id = db.append_message("team-session-1", "user", "请调研 AI 方向")
    assistant_message_id = db.append_message("team-session-1", "assistant", "已完成调研计划")

    resolved = db.resolve_team_mission_conversation("conversation-1")

    assert resolved["conversation"]["conversation_id"] == "conversation-1"
    assert resolved["conversation"]["stable_session_id"] == "team-session-1"
    assert resolved["mission"]["mission_id"] == "mission-1"
    assert resolved["graph"]["nodes"][0]["node_id"] == "mission-1:node-1"
    assert resolved["graph"]["nodes"][0]["metadata"]["hermes_node_id"] == "node-1"
    assert [message["text"] for message in resolved["messages"]] == ["请调研 AI 方向", "已完成调研计划"]
    assert [message["message_id"] for message in resolved["graph"]["recent_messages"]] == [
        str(user_message_id),
        str(assistant_message_id),
    ]
    assert resolved["pageInfo"] == {
        "prevCursor": "",
        "nextCursor": "",
        "prev_cursor_id": None,
        "next_cursor_id": None,
        "hasMoreBefore": False,
        "hasMoreAfter": False,
        "totalCount": 2,
    }
    assert resolved["graph"]["message_page_info"] == resolved["pageInfo"]
    assert db.get_session("team-session-1")["source"] == "team_mission"


def test_team_mission_conversation_resolve_returns_all_mission_frames(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    for mission_id, title, created_at in (
        ("mission-1", "第一次任务", 1.0),
        ("mission-2", "第二次任务", 2.0),
    ):
        db.upsert_team_mission(
            mission_id=mission_id,
            conversation_id="conversation-1",
            team_id="team-1",
            title=title,
            objective=title,
            workspace_id="workspace-1",
            workspace_path="/tmp/workspace",
            mode="supervised_mission",
            leader_session_id="team-session-1",
            created_at=created_at,
            updated_at=created_at,
            metadata={"stableTeamSessionId": "team-session-1", "task_id": f"task-{mission_id}"},
        )
        db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id="root",
            kind="root",
            title=f"{title}计划",
            status="completed",
            metadata={"task_id": f"task-{mission_id}"},
        )
        db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id="worker",
            kind="worker",
            title=f"{title}执行",
            status="running",
            metadata={"task_id": f"task-{mission_id}"},
        )
        db.upsert_team_mission_edge(
            mission_id=mission_id,
            from_node_id="root",
            to_node_id="worker",
        )

    resolved = db.resolve_team_mission_conversation("conversation-1")
    graph = resolved["graph"]

    assert resolved["mission"]["mission_id"] == "mission-2"
    assert [frame["missionId"] for frame in graph["task_frames"]] == ["mission-1", "mission-2"]
    assert [frame["nodeIds"] for frame in graph["task_frames"]] == [
        ["mission-1:root", "mission-1:worker"],
        ["mission-2:root", "mission-2:worker"],
    ]
    assert [node["node_id"] for node in graph["nodes"]] == [
        "mission-1:root",
        "mission-1:worker",
        "mission-2:root",
        "mission-2:worker",
    ]
    assert graph["nodes"][0]["metadata"]["hermes_mission_id"] == "mission-1"
    assert graph["nodes"][0]["metadata"]["hermes_node_id"] == "root"
    assert [(edge["from_node_id"], edge["to_node_id"]) for edge in graph["edges"]] == [
        ("mission-1:root", "mission-1:worker"),
        ("mission-2:root", "mission-2:worker"),
    ]


def test_team_mission_conversation_runtime_summary_returns_frames_and_runtime_sessions(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        team_id="team-1",
        stable_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-2",
    )
    for mission_id, title, status, created_at in (
        ("mission-1", "第一次任务", "completed", 1.0),
        ("mission-2", "第二次任务", "waiting_approval", 2.0),
    ):
        db.upsert_team_mission(
            mission_id=mission_id,
            conversation_id="conversation-1",
            team_id="team-1",
            title=title,
            objective=title,
            mode="supervised_mission",
            status=status,
            leader_session_id="team-session-1",
            created_at=created_at,
            updated_at=created_at,
            metadata={"task_id": f"task-{mission_id}"},
        )
        db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id="root",
            kind="root",
            title=f"{title}计划",
            status="completed",
        )

    db.upsert_team_mission_node(
        mission_id="mission-2",
        node_id="approval",
        kind="approval_gate",
        title="审批任务图",
        status="waiting_approval",
    )
    db.bind_team_mission_run(
        mission_id="mission-2",
        node_id="approval",
        run_id="run-approval",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker-1",
        role="approval_gate",
    )

    summary = db.get_team_mission_conversation_runtime_summary("conversation-1")

    assert summary["mission"]["mission_id"] == "mission-2"
    assert summary["mission_status"] == "waiting_approval"
    assert summary["task_frame_count"] == 2
    assert [frame["missionId"] for frame in summary["task_frames"]] == ["mission-1", "mission-2"]
    assert summary["active_task_frame"]["missionId"] == "mission-2"
    assert summary["pending_approval_count"] == 1
    assert summary["pending_approvals"][0]["nodeId"] == "mission-2:approval"
    assert summary["run_session_ids"] == ["worker-session-1", "runtime-worker-1"]


def test_team_mission_conversation_runtime_session_ids_are_lightweight(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        team_id="team-1",
        workspace_id="workspace-1",
        stable_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="测试任务",
        objective="测试任务",
        mode="supervised_mission",
        status="running",
        leader_session_id="team-session-1",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker-1",
        runtime_scope_key="team:mission-1:node:worker",
        role="worker",
    )

    assert db.list_team_mission_conversation_runtime_session_ids(
        team_id="team-1",
        workspace_id="workspace-1",
        mission_id="mission-1",
    ) == ["team-session-1", "worker-session-1", "runtime-worker-1"]

    assert db.list_team_mission_conversation_runtime_session_ids(
        mission_id="conversation-1",
    ) == ["team-session-1", "worker-session-1", "runtime-worker-1"]


def test_team_mission_conversation_projection_returns_final_deliverable_and_artifacts(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        team_id="team-1",
        stable_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="交付任务",
        objective="生成交付文件",
        mode="supervised_mission",
        status="completed",
        leader_session_id="team-session-1",
        metadata={"task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="synthesis",
        kind="synthesizer",
        title="汇总",
        status="completed",
        metadata={"task_id": "task-1"},
    )
    message_id = db.append_message(
        "team-session-1",
        "assistant",
        "最终交付内容",
        metadata={
            "team_mission": {
                "kind": "final_deliverable",
                "mission_id": "mission-1",
                "node_id": "synthesis",
                "task_id": "task-1",
                "source_run_id": "run-synthesis",
                "source_session_id": "worker-session-1",
                "source_seq": "42",
            }
        },
    )
    db.upsert_team_mission_memory_item(
        memory_id="memory-artifact-1",
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        content="交付文件",
        source_node_ids=["synthesis"],
        source_run_ids=["run-synthesis"],
        artifact_refs=[
            {
                "path": "/tmp/report.md",
                "title": "report.md",
                "kind": "file",
            }
        ],
    )

    summary = db.get_team_mission_conversation_runtime_summary("conversation-1")
    [frame] = summary["task_frames"]

    assert summary["final_deliverables"][0]["messageId"] == str(message_id)
    assert summary["artifact_refs"] == [{"path": "/tmp/report.md", "title": "report.md", "kind": "file"}]
    assert summary["last_message"]["teamMission"]["artifactRefs"] == [
        {"path": "/tmp/report.md", "title": "report.md", "kind": "file"}
    ]
    assert frame["finalDeliverable"]["content"] == "最终交付内容"
    assert frame["finalDeliverable"]["nodeId"] == "mission-1:synthesis"
    assert frame["deliverableMessageId"] == str(message_id)
    assert frame["artifactRefs"] == [{"path": "/tmp/report.md", "title": "report.md", "kind": "file"}]
    assert frame["finalDeliverable"]["artifactRefs"] == frame["artifactRefs"]

    resolved = db.resolve_team_mission_conversation("conversation-1")
    [graph_frame] = resolved["graph"]["task_frames"]
    assert resolved["graph"]["final_deliverables"][0]["content"] == "最终交付内容"
    assert resolved["graph"]["last_message"]["teamMission"]["artifactRefs"] == [
        {"path": "/tmp/report.md", "title": "report.md", "kind": "file"}
    ]
    assert graph_frame["finalDeliverable"]["messageId"] == str(message_id)
    assert graph_frame["artifactRefs"] == frame["artifactRefs"]


def test_team_mission_conversation_projection_keeps_multi_round_deliverables_isolated(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        team_id="team-1",
        stable_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-2",
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    message_ids = {}
    for index, mission_id in enumerate(("mission-1", "mission-2"), start=1):
        task_id = f"task-{index}"
        db.upsert_team_mission(
            mission_id=mission_id,
            conversation_id="conversation-1",
            team_id="team-1",
            title=f"第 {index} 轮任务",
            objective=f"完成第 {index} 轮任务",
            mode="supervised_mission",
            status="completed",
            leader_session_id="team-session-1",
            created_at=float(index),
            updated_at=float(index),
            metadata={"stableTeamSessionId": "team-session-1", "task_id": task_id},
        )
        db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id="synthesis",
            kind="synthesizer",
            title="最终汇总",
            status="completed",
            metadata={"task_id": task_id},
        )
        db.append_message("team-session-1", "user", f"第 {index} 轮用户请求")
        message_ids[mission_id] = db.append_message(
            "team-session-1",
            "assistant",
            f"第 {index} 轮最终交付",
            metadata={
                "team_mission": {
                    "kind": "final_deliverable",
                    "mission_id": mission_id,
                    "node_id": "synthesis",
                    "task_id": task_id,
                    "source_run_id": f"run-synthesis-{index}",
                    "source_session_id": f"synthesis-session-{index}",
                    "source_seq": str(index),
                }
            },
        )
        db.upsert_team_mission_memory_item(
            memory_id=f"memory-artifact-{index}",
            team_id="team-1",
            mission_id=mission_id,
            conversation_session_id="team-session-1",
            task_id=task_id,
            content=f"第 {index} 轮过程文件",
            source_node_ids=["synthesis"],
            source_run_ids=[f"run-synthesis-{index}"],
            artifact_refs=[
                {
                    "path": f"/tmp/round-{index}.md",
                    "title": f"round-{index}.md",
                    "kind": "file",
                }
            ],
        )

    resolved = db.resolve_team_mission_conversation("conversation-1")
    graph = resolved["graph"]
    frames = graph["task_frames"]

    assert [message["text"] for message in graph["recent_messages"]] == [
        "第 1 轮用户请求",
        "第 1 轮最终交付",
        "第 2 轮用户请求",
        "第 2 轮最终交付",
    ]
    assert [deliverable["messageId"] for deliverable in graph["final_deliverables"]] == [
        str(message_ids["mission-1"]),
        str(message_ids["mission-2"]),
    ]
    assert [frame["missionId"] for frame in frames] == ["mission-1", "mission-2"]
    assert frames[0]["finalDeliverable"]["content"] == "第 1 轮最终交付"
    assert frames[0]["artifactRefs"] == [{"path": "/tmp/round-1.md", "title": "round-1.md", "kind": "file"}]
    assert frames[0]["finalDeliverable"]["artifactRefs"] == frames[0]["artifactRefs"]
    assert frames[1]["finalDeliverable"]["content"] == "第 2 轮最终交付"
    assert frames[1]["artifactRefs"] == [{"path": "/tmp/round-2.md", "title": "round-2.md", "kind": "file"}]
    assert frames[1]["finalDeliverable"]["artifactRefs"] == frames[1]["artifactRefs"]
    assert graph["last_message"]["text"] == "第 2 轮最终交付"
    assert graph["last_message"]["teamMission"]["artifactRefs"] == frames[1]["artifactRefs"]


def test_team_mission_conversation_rename_updates_canonical_conversation_without_mutating_mission_or_session_title(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="旧标题",
        objective="Do research",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    result = db.rename_team_mission_conversation("conversation-1", "新团队任务")

    assert result["conversation_id"] == "conversation-1"
    assert result["stable_session_id"] == "team-session-1"
    assert result["title"] == "新团队任务"
    assert result["conversation"]["title"] == "新团队任务"
    assert result["conversation"]["display_title"] == "新团队任务"
    assert result["conversation"]["display_title_source"] == "user"
    assert db.get_team_mission_graph("mission-1")["mission"]["title"] == "旧标题"
    assert db.get_session("team-session-1")["source"] == "team_mission"
    assert db.get_session("team-session-1")["title"] is None


def test_team_mission_task_binding_preserves_existing_conversation_title(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="正常生成的会话标题",
        objective="和 Leader 日常沟通",
    )

    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="用户第一条任务消息",
        objective="执行一个团队任务",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )

    resolved = db.resolve_team_mission_conversation("conversation-1")

    assert resolved["conversation"]["title"] == "正常生成的会话标题"
    assert resolved["conversation"]["display_title"] == "正常生成的会话标题"
    assert resolved["conversation"]["display_title_source"] == "first_user_message"
    assert resolved["conversation"]["active_mission_id"] == "mission-1"
    assert resolved["mission"]["title"] == "用户第一条任务消息"


def test_team_mission_first_user_message_replaces_placeholder_conversation_title(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team Mission",
        objective="占位会话",
    )

    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="滴滴滴",
        objective="用户首条消息",
    )

    conversation = db.get_team_mission_conversation("conversation-1")

    assert conversation["title"] == "滴滴滴"
    assert conversation["display_title"] == "滴滴滴"
    assert conversation["display_title_source"] == "first_user_message"


def test_team_mission_first_user_title_source_is_authoritative_even_when_text_matches_placeholder(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team Mission",
        objective="用户首条消息就是这个文本",
        metadata={"display_title_source": "first_user_message"},
    )

    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="后续任务标题不能覆盖首条消息标题",
        objective="后续任务",
    )

    conversation = db.get_team_mission_conversation("conversation-1")

    assert conversation["title"] == "Team Mission"
    assert conversation["display_title_source"] == "first_user_message"


def test_team_mission_conversation_ensure_does_not_touch_activity_time(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="历史团队会话",
        created_at=100,
        updated_at=100,
    )

    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        mission_id="mission-1",
        title="打开历史时不应该触活",
    )

    conversation = db.get_team_mission_conversation("conversation-1")

    assert conversation["updated_at"] == 100
    assert conversation["active_mission_id"] == "mission-1"
    assert conversation["title"] == "历史团队会话"


def test_team_mission_conversation_list_uses_message_activity_like_normal_sessions(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="old-conversation",
        stable_session_id="old-team-session",
        team_id="team-1",
        title="旧团队会话",
        created_at=100,
        updated_at=10_000,
    )
    db.upsert_team_mission_conversation(
        conversation_id="newer-created-conversation",
        stable_session_id="newer-team-session",
        team_id="team-1",
        title="创建时间较新的团队会话",
        created_at=200,
        updated_at=200,
    )

    assert db.list_team_mission_conversations() == []

    db.create_session("old-team-session", source="team_mission", transient=False)
    db.append_message("old-team-session", role="user", content="旧消息")
    db.create_session("newer-team-session", source="team_mission", transient=False)
    db.append_message("newer-team-session", role="user", content="新消息")

    resolved = db.resolve_team_mission_conversation("old-conversation")
    assert resolved["conversation"]["conversation_id"] == "old-conversation"
    assert resolved["mission"] == {}
    assert [item["conversation_id"] for item in db.list_team_mission_conversations()] == [
        "newer-created-conversation",
        "old-conversation",
    ]

    db.append_message("old-team-session", role="user", content="真实新消息")

    conversations = db.list_team_mission_conversations()

    assert [item["conversation_id"] for item in conversations] == [
        "old-conversation",
        "newer-created-conversation",
    ]
    assert conversations[0]["updated_at"] > conversations[1]["updated_at"]
    assert conversations[0]["message_count"] == 2
    assert conversations[1]["message_count"] == 1


def test_team_mission_conversation_list_reads_session_summary_not_messages(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        created_at=100,
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    db.append_message("team-session-1", role="user", content="列表只需要摘要")

    statements = []
    with db._lock:
        db._conn.set_trace_callback(statements.append)
    try:
        conversations = db.list_team_mission_conversations()
    finally:
        with db._lock:
            db._conn.set_trace_callback(None)

    assert [item["conversation_id"] for item in conversations] == ["conversation-1"]
    assert conversations[0]["message_count"] == 1
    traced_sql = "\n".join(statements).lower()
    assert "from messages" not in traced_sql
    assert "join messages" not in traced_sql


def test_empty_team_mission_conversation_shells_are_not_history_and_are_pruned(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.ensure_team_mission_conversation(
        conversation_id="empty-conversation",
        stable_session_id="empty-team-session",
        team_id="team-1",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="mission-conversation",
        team_id="team-1",
        title="Mission shell",
        mode="supervised_mission",
        leader_session_id="mission-team-session",
    )

    assert [item["conversation_id"] for item in db.list_team_mission_conversations()] == [
        "mission-conversation",
    ]
    assert db.get_team_mission_conversation("empty-conversation")["conversation_id"] == "empty-conversation"
    assert db.resolve_team_mission_conversation("empty-conversation") == {}
    db.close()

    reopened = SessionDB(db_path)

    assert reopened.get_team_mission_conversation("empty-conversation") == {}
    assert reopened.get_session("empty-team-session") is None
    assert reopened.get_team_mission_conversation("mission-conversation")["conversation_id"] == "mission-conversation"


def test_placeholder_team_mission_conversation_title_repairs_from_first_user_message(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team Mission",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
    )
    db.append_message("team-session-1", role="user", content="你好啊")
    db.append_message("team-session-1", role="assistant", content="你好")
    db.close()

    reopened = SessionDB(db_path)
    conversation = reopened.get_team_mission_conversation("conversation-1")
    session = reopened.get_session("team-session-1")

    assert conversation["title"] == "你好啊"
    assert conversation["display_title_source"] == "first_user_message"
    assert session["title"] is None
    assert [item["title"] for item in reopened.list_team_mission_conversations()] == ["你好啊"]


def test_placeholder_team_mission_conversation_title_repair_does_not_conflict_with_session_titles(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("ordinary-session", source="tui", transient=False)
    db.set_session_title("ordinary-session", "重复标题")
    for suffix in ("1", "2"):
        db.ensure_team_mission_conversation(
            conversation_id=f"conversation-{suffix}",
            stable_session_id=f"team-session-{suffix}",
            team_id="team-1",
            title="Team Mission",
        )
        db.append_message(f"team-session-{suffix}", role="user", content="重复标题")
    db.close()

    reopened = SessionDB(db_path)

    assert reopened.get_team_mission_conversation("conversation-1")["title"] == "重复标题"
    assert reopened.get_team_mission_conversation("conversation-2")["title"] == "重复标题"
    assert reopened.get_session("team-session-1")["title"] is None
    assert reopened.get_session("team-session-2")["title"] is None
    assert reopened.get_session("ordinary-session")["title"] == "重复标题"


def test_legacy_leader_prompt_session_reopens_as_team_conversation(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("team-session-1", source="tui", transient=False)
    db.set_session_title("team-session-1", "历史团队会话")
    db.append_message(
        "team-session-1",
        role="user",
        content="你好啊",
        metadata={
            "doxie_product_context": json.dumps({
                "team_mission": {
                    "kind": "leader_conversation",
                    "conversation_id": "conversation-1",
                    "conversation_session_id": "team-session-1",
                    "team_id": "team-1",
                    "workspace_id": "workspace-1",
                    "workspace_path": "/tmp/workspace",
                },
            }),
        },
    )
    db.close()

    reopened = SessionDB(db_path)

    session = reopened.get_session("team-session-1")
    conversation = reopened.resolve_team_mission_conversation("conversation-1")["conversation"]

    assert session["source"] == "team_mission"
    assert conversation["conversation_id"] == "conversation-1"
    assert conversation["stable_session_id"] == "team-session-1"
    assert conversation["title"] == "历史团队会话"
    assert [item["conversation_id"] for item in reopened.list_team_mission_conversations()] == ["conversation-1"]


def test_team_mission_conversation_delete_removes_canonical_graph_and_returns_runtime_sessions(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Do research",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="completed",
    )
    db.create_session("worker-session-1", source="team_mission", transient=False)
    db.create_session("runtime-worker-1", source="team_mission", transient=False)
    db.append_message("team-session-1", role="user", content="Start team mission")
    db.append_message("worker-session-1", role="assistant", content="Worker result")
    db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id="node-leader",
        to_node_id="node-worker",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker-1",
        runtime_scope_key="team:mission-1:node-worker",
        status="completed",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker-1",
        runtime_scope_key="team:mission-1:node-worker",
        role="worker",
    )
    db.append_run_event(
        "team-session-1",
        {
            "type": "message.delta",
            "session_id": "team-session-1",
            "stored_session_id": "team-session-1",
            "run_id": "run-leader",
            "turn_id": "turn-leader",
            "runtime_scope_key": "team:mission-1:leader-conversation",
            "seq": 1,
            "payload": {"text": "Planning"},
        },
    )
    db.append_run_event(
        "worker-session-1",
        {
            "type": "message.complete",
            "session_id": "runtime-worker-1",
            "stored_session_id": "worker-session-1",
            "run_id": "run-worker",
            "turn_id": "turn-worker",
            "runtime_scope_key": "team:mission-1:node-worker",
            "seq": 1,
            "payload": {"text": "Worker result", "status": "complete"},
        },
    )
    memory_item = db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        scope="mission_task",
        kind="summary",
        content="Worker result",
        source_node_ids=["node-worker"],
        source_run_ids=["run-worker"],
        visibility="team",
    )
    db.upsert_team_mission_memory_edge(
        from_memory_id=memory_item["id"],
        to_memory_id=memory_item["id"],
        relation="self",
    )

    result = db.delete_team_mission_conversation("conversation-1")

    assert result["deleted"] is True
    assert result["conversation_id"] == "conversation-1"
    assert result["stable_session_id"] == "team-session-1"
    assert result["mission_ids"] == ["mission-1"]
    assert result["run_session_ids"] == ["worker-session-1", "runtime-worker-1"]
    assert result["deleted_session_ids"] == [
        "team-session-1",
        "worker-session-1",
        "runtime-worker-1",
    ]
    assert db.resolve_team_mission_conversation("conversation-1") == {}
    assert db.get_team_mission_graph("mission-1") == {}
    assert db.get_team_mission_node("mission-1", "node-worker") == {}
    assert db.list_team_mission_memory_items(conversation_session_id="team-session-1") == []
    assert db.list_team_mission_memory_edges(from_memory_id=memory_item["id"]) == []
    assert db.get_session("team-session-1") is None
    assert db.get_session("worker-session-1") is None
    assert db.get_session("runtime-worker-1") is None
    assert db.get_messages("team-session-1") == []
    assert db.get_messages("worker-session-1") == []
    assert db.get_run("run-worker") is None
    assert db.list_run_events("team-session-1") == []
    assert db.list_run_events("worker-session-1") == []


def test_cancel_team_mission_marks_active_graph_and_returns_run_bindings(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-running",
        kind="worker",
        title="Running node",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-done",
        kind="worker",
        title="Done node",
        status="completed",
    )
    db.upsert_run(
        run_id="run-running",
        session_id="node-session-running",
        runtime_scope_key="team:mission-1:node-running",
        status="running",
    )
    db.upsert_run(
        run_id="run-done",
        session_id="node-session-done",
        runtime_scope_key="team:mission-1:node-done",
        status="completed",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-running",
        run_id="run-running",
        session_id="node-session-running",
        runtime_scope_key="team:mission-1:node-running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-done",
        run_id="run-done",
        session_id="node-session-done",
        runtime_scope_key="team:mission-1:node-done",
    )

    result = db.cancel_team_mission(
        mission_id="mission-1",
        canceled_by="user",
        reason="用户终止团队任务",
    )

    assert result["mission_status"] == "cancelled"
    assert result["graph"]["mission"]["status"] == "cancelled"
    assert db.get_team_mission_node("mission-1", "node-running")["status"] == "cancelled"
    assert db.get_team_mission_node("mission-1", "node-done")["status"] == "completed"
    assert [binding["run_id"] for binding in result["cancel_run_bindings"]] == ["run-running"]
    assert result["graph"]["mission"]["metadata"]["cancel_reason"] == "用户终止团队任务"


def test_cancel_team_mission_reaps_zombie_running_run(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="verify-stats-report",
        kind="worker",
        title="Verify",
        status="running",
    )
    # A member-node run that was started by the scheduler around/after cancel and
    # is left 'running' in the control-plane runs table.
    db.upsert_run(
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="verify-stats-report",
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
    )

    result = db.cancel_team_mission(mission_id="mission-1", canceled_by="user")

    assert result["mission_status"] == "cancelled"
    assert [b["run_id"] for b in result["cancel_run_bindings"]] == ["run-verify"]
    # The reaper must have forced the zombie run terminal in the runs table.
    assert db.get_run("run-verify")["status"] == "cancelled"


def test_cancel_already_terminal_mission_reaps_leftover_running_run(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    # Mission is ALREADY terminal (a prior cancel completed) but a member-node
    # worker run got scheduled after that and is still 'running'.
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="cancelled",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="verify-stats-report",
        kind="worker",
        title="Verify",
        status="running",
    )
    db.upsert_run(
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="verify-stats-report",
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
    )

    result = db.cancel_team_mission(mission_id="mission-1", canceled_by="user")

    # Even though the mission was already terminal, the leftover running run is
    # surfaced for worker termination AND reaped terminal in the DB.
    assert [b["run_id"] for b in result["cancel_run_bindings"]] == ["run-verify"]
    assert db.get_run("run-verify")["status"] == "cancelled"


def test_complete_plan_approval_gate_inherits_leader_owner(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    # Mission whose metadata members list is empty, but the leader/root node
    # already carries the real leader profile. The approval gate created on plan
    # completion must be owned by that leader, not the synthetic placeholder.
    db.upsert_team_mission(
        mission_id="m1",
        team_id="t1",
        title="Supervised",
        mode="supervised_mission",
        status="planning",
        leader_session_id="team-session-1",
    )
    db.upsert_team_mission_node(
        mission_id="m1",
        node_id="m1::root",
        kind="root",
        title="Plan",
        status="running",
        assignee_profile_id="profile-leader",
        assignee_profile_version_id="version-leader",
        metadata={"role": "leader", "phase": "planning"},
    )
    db.upsert_team_mission_node(
        mission_id="m1",
        node_id="w1",
        kind="worker",
        title="Work",
        status="todo",
    )

    db.complete_team_mission_plan(mission_id="m1")

    graph = db.get_team_mission_graph("m1")
    approval = [n for n in graph["nodes"] if n["kind"] == "approval_gate"]
    assert len(approval) == 1
    assert approval[0]["assignee_profile_id"] == "profile-leader"
    assert approval[0]["assignee_profile_version_id"] == "version-leader"
    # No synthetic "Leader" placeholder owner.
    assert approval[0].get("assignee_display_name") != "Leader"


def test_team_mission_runtime_events_reuse_ordinary_run_event_coalescing(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="Mission",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"task_id": "task-1", "stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-leader",
        kind="root",
        title="Plan",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.upsert_run(
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-leader",
        run_id="run-leader",
        session_id="session-leader",
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={
            "type": "message.delta",
            "seq": 1,
            "payload": {"delta": "Plan ", "mode": "append"},
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={
            "type": "message.delta",
            "seq": 2,
            "payload": {"delta": "graph", "mode": "append"},
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={
            "type": "tool.start",
            "seq": 3,
            "payload": {"tool_call_id": "tool-1", "name": "team_mission.node.create"},
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-leader",
        event={
            "type": "message.delta",
            "seq": 4,
            "payload": {"delta": " after tool", "mode": "append"},
        },
    )

    events = db.list_team_mission_run_events("mission-1")

    assert [event["type"] for event in events] == [
        "team_mission.runtime.event",
        "team_mission.runtime.event",
        "team_mission.runtime.event",
        "team_mission.runtime.event",
    ]
    assert [event["payload"]["source_event_type"] for event in events] == [
        "message.delta",
        "message.delta",
        "tool.start",
        "message.delta",
    ]
    assert events[0]["source_seq"] == 1
    assert events[0]["team_mission_event_seq"] == events[0]["seq"]
    assert events[0]["mission_id"] == "mission-1"
    assert events[0]["conversation_id"] == "conversation-1"
    assert events[0]["stable_session_id"] == "team-session-1"
    assert events[0]["node_id"] == "node-leader"
    assert events[0]["task_id"] == "task-1"
    assert events[0]["task_frame_id"] == "mission-frame:mission-1"
    assert events[0]["payload"]["source_event"]["payload"]["delta"] == "Plan "
    assert events[0]["payload"]["subject"]["type"] == "node"
    assert events[0]["payload"]["subject"]["node_id"] == "node-leader"
    assert events[0]["payload"]["subject"]["canonical_node_id"] == "mission-1:node-leader"
    assert events[0]["payload"]["text_stream"]["mode"] == "append"
    assert events[0]["payload"]["text_stream"]["delta"] == "Plan "
    assert events[1]["payload"]["source_event"]["payload"]["delta"] == "graph"
    assert events[1]["payload"]["text_stream"]["delta"] == "graph"
    assert events[0]["payload"]["mission_id"] == "mission-1"
    assert events[0]["payload"]["missionId"] == "mission-1"
    assert events[0]["payload"]["conversation_id"] == "conversation-1"
    assert events[0]["payload"]["conversationId"] == "conversation-1"
    assert events[0]["payload"]["stable_session_id"] == "team-session-1"
    assert events[0]["payload"]["node_id"] == "node-leader"
    assert events[0]["payload"]["nodeId"] == "node-leader"
    assert events[0]["payload"]["task_id"] == "task-1"
    assert events[0]["payload"]["taskFrameId"] == "mission-frame:mission-1"
    assert events[0]["payload"]["source_seq"] == 1
    assert events[0]["payload"]["team_mission_event_seq"] == events[0]["seq"]
    assert events[3]["payload"]["source_event"]["payload"]["delta"] == " after tool"
    rows = db._conn.execute(  # noqa: SLF001 - contract test for the canonical mission event log.
        """
        SELECT seq, event_type, source_event_type
        FROM team_mission_events
        WHERE mission_id = ?
        ORDER BY seq ASC
        """,
        ("mission-1",),
    ).fetchall()
    assert [(row["event_type"], row["source_event_type"]) for row in rows] == [
        ("team_mission.runtime.event", "message.delta"),
        ("team_mission.runtime.event", "message.delta"),
        ("team_mission.runtime.event", "tool.start"),
        ("team_mission.runtime.event", "message.delta"),
    ]
    assert [row["seq"] for row in rows] == [event["seq"] for event in events]


def test_team_mission_structural_events_use_subject_node_identity_not_bound_runtime_node(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="Mission",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"task_id": "task-1", "stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:root",
        kind="root",
        title="Plan",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.upsert_run(
        run_id="run-root",
        session_id="session-root",
        runtime_scope_key="profile:leader",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:root",
        run_id="run-root",
        session_id="session-root",
        runtime_scope_key="profile:leader",
        role="leader",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-root",
        event={
            "type": "mission.node.created",
            "seq": 1,
            "payload": {
                "node": {
                    "node_id": "node-worker",
                    "kind": "worker",
                    "title": "Worker",
                    "status": "pending",
                },
            },
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-root",
        event={
            "type": "mission.approval.requested",
            "seq": 2,
            "payload": {
                "approval_id": "team-mission:mission-1:approval-plan",
                "message": "Approve plan",
            },
        },
    )

    raw_events = db.list_run_events("session-root")
    assert "node_id" not in raw_events[0]
    assert "node_id" not in raw_events[0]["payload"]
    assert raw_events[0]["payload"]["node"]["node_id"] == "node-worker"
    assert "node_id" not in raw_events[1]
    assert "node_id" not in raw_events[1]["payload"]
    assert raw_events[1]["payload"]["approval_id"] == "team-mission:mission-1:approval-plan"

    mission_events = db.list_team_mission_run_events("mission-1")
    node_event = next(
        event for event in mission_events
        if event["type"] == "team_mission.runtime.event"
        and event["payload"]["source_event_type"] == "mission.node.created"
    )
    approval_event = next(
        event for event in mission_events
        if event["type"] == "team_mission.runtime.event"
        and event["payload"]["source_event_type"] == "mission.approval.requested"
    )
    assert node_event["node_id"] == "node-worker"
    assert node_event["payload"]["node_id"] == "node-worker"
    assert node_event["payload"]["nodeId"] == "node-worker"
    assert node_event["payload"]["subject"]["canonical_node_id"] == "mission-1:node-worker"
    assert node_event["payload"]["source_payload"]["node"]["node_id"] == "node-worker"
    assert "node_id" not in node_event["payload"]["source_payload"]
    assert approval_event["node_id"] == "team-mission:mission-1:approval-plan"
    assert approval_event["payload"]["node_id"] == "team-mission:mission-1:approval-plan"
    assert approval_event["payload"]["approval_id"] == "team-mission:mission-1:approval-plan"
    assert approval_event["payload"]["subject"]["canonical_node_id"] == "team-mission:mission-1:approval-plan"
    assert "node_id" not in approval_event["payload"]["source_payload"]


def test_team_mission_run_events_include_conversation_status_projection(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Create launch plan",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
        mode="autonomous_mission",
        leader_session_id="team-session-1",
        metadata={"task_id": "task-1", "stableTeamSessionId": "team-session-1"},
    )
    mission = db.get_team_mission_graph("mission-1")["mission"]
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        mission=mission,
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        objective="Create launch plan",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {"status": "complete"},
        },
    )

    events = db.list_team_mission_run_events("mission-1")
    complete_event = next(
        event
        for event in events
        if (
            event["type"] == "team_mission.runtime.event"
            and event["payload"]["source_event_type"] == "message.complete"
        )
    )
    status_event = next(event for event in events if event["type"] == "team_mission.conversation.status")
    projection = status_event["payload"]["conversation"]

    assert status_event["seq"] > complete_event["seq"]
    assert status_event["mission_id"] == "mission-1"
    assert status_event["conversation_id"] == "conversation-1"
    assert status_event["stable_session_id"] == "team-session-1"
    assert status_event["payload"]["protocol"] == "team_mission.event.v1"
    assert status_event["payload"]["kind"] == "conversation.status.updated"
    assert status_event["payload"]["source_event_type"] == "message.complete"
    assert status_event["payload"]["source_event_seq"] == complete_event["seq"]
    assert projection["conversation_id"] == "conversation-1"
    assert projection["stable_session_id"] == "team-session-1"
    assert projection["active_mission_id"] == "mission-1"
    assert projection["mission_status"] == "ready"
    assert projection["running"] is False
    assert projection["run_state"] == "idle"

    after_complete = db.list_team_mission_run_events("mission-1", after_seq=complete_event["seq"])
    assert any(event["type"] == "team_mission.conversation.status" for event in after_complete)
    assert all(
        event["seq"] > status_event["seq"]
        for event in db.list_team_mission_run_events("mission-1", after_seq=status_event["seq"])
    )


def test_team_mission_conversation_status_projection_uses_active_member_run_binding(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        active_mission_id="mission-1",
        title="Mission",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="ready",
        leader_session_id="team-session-1",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-verify",
        kind="verifier",
        title="Verify",
        status="ready",
        runtime_scope_key="team:mission-1:node:node-verify",
    )
    db.upsert_run(
        run_id="run-verify",
        session_id="team:mission-1:node:node-verify",
        runtime_scope_key="team:mission-1:node:node-verify",
        runtime_session_id="runtime-verify",
        turn_id="turn-verify",
        status="running",
        updated_at=300,
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-verify",
        run_id="run-verify",
        session_id="team:mission-1:node:node-verify",
        runtime_session_id="runtime-verify",
        runtime_scope_key="team:mission-1:node:node-verify",
        role="verifier",
    )

    projection = db.get_team_mission_conversation_status_projection("conversation-1")

    assert projection["mission_status"] == "ready"
    assert projection["running"] is True
    assert projection["run_state"] == "running"
    assert projection["active_run_id"] == "run-verify"
    assert projection["active_runtime_session_id"] == "runtime-verify"
    assert projection["active_node_count"] == 1


def test_team_mission_runtime_projection_uses_structured_final_node_contract(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Create report",
        workspace_id="workspace-1",
        workspace_path="/tmp/workspace",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        output_contract={"format": "artifact"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesis",
        title="Synthesis",
        status="running",
        output_contract={"format": "final_deliverable"},
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node-worker",
        status="running",
    )
    db.upsert_run(
        run_id="run-synthesis",
        session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node-worker",
        role="worker",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="synthesis",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={"type": "message.delta", "seq": 1, "payload": {"delta": "worker"}},
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-synthesis",
        event={"type": "message.delta", "seq": 1, "payload": {"delta": "final"}},
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-synthesis",
        event={"type": "message.complete", "seq": 2, "payload": {"status": "complete", "text": "final text"}},
    )

    runtime_events = [
        event
        for event in db.list_team_mission_run_events("mission-1")
        if event["type"] == "team_mission.runtime.event"
    ]
    worker_delta = next(
        event
        for event in runtime_events
        if event["payload"]["source_event_type"] == "message.delta"
        and event["payload"]["run_id"] == "run-worker"
    )
    synthesis_delta = next(
        event
        for event in runtime_events
        if event["payload"]["source_event_type"] == "message.delta"
        and event["payload"]["run_id"] == "run-synthesis"
    )
    synthesis_complete = next(
        event
        for event in runtime_events
        if event["payload"]["source_event_type"] == "message.complete"
        and event["payload"]["run_id"] == "run-synthesis"
    )

    assert worker_delta["payload"]["kind"] == "node.output.delta"
    assert worker_delta["payload"]["node_kind"] == "worker"
    assert worker_delta["payload"]["subject"]["runtime_stable_session_id"] == "worker-session-1"
    assert synthesis_delta["payload"]["kind"] == "final.output.delta"
    assert synthesis_delta["payload"]["node_kind"] == "synthesis"
    assert synthesis_delta["payload"]["output_contract_format"] == "final_deliverable"
    assert synthesis_delta["payload"]["subject"]["runtime_stable_session_id"] == "synthesis-session-1"
    assert synthesis_delta["payload"]["text_stream"]["mode"] == "append"
    assert synthesis_delta["payload"]["text_stream"]["delta"] == "final"
    assert synthesis_complete["payload"]["kind"] == "final.completed"
    assert synthesis_complete["payload"]["subject"]["node_id"] == "team-mission:mission-1:synthesis"
    assert synthesis_complete["payload"]["text_stream"]["text"] == "final text"


def test_team_mission_event_cursor_is_global_across_node_sessions(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="supervised_mission")
    for node_id, run_id, session_id in (
        ("node-leader-1", "run-leader-1", "session-leader-1"),
        ("node-leader-2", "run-leader-2", "session-leader-2"),
    ):
        db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id=node_id,
            kind="root",
            title=node_id,
            status="running",
        )
        db.bind_team_mission_run(
            mission_id="mission-1",
            node_id=node_id,
            run_id=run_id,
            session_id=session_id,
            runtime_scope_key=f"team:mission-1:{node_id}",
            role="leader",
        )
        db.append_team_mission_run_event(
            mission_id="mission-1",
            run_id=run_id,
            event={
                "type": "message.delta",
                "seq": 1,
                "payload": {"delta": node_id},
            },
        )

    first_page = db.list_team_mission_run_events("mission-1", limit=1)
    second_page = db.list_team_mission_run_events("mission-1", after_seq=first_page[-1]["seq"])

    assert len(first_page) == 1
    assert [event["payload"]["node_id"] for event in second_page] == ["node-leader-2"]
    assert second_page[0]["source_seq"] == 1
    assert second_page[0]["seq"] > first_page[0]["seq"]


def test_team_mission_terminal_run_event_reduces_node_status(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {"status": "complete"},
        },
    )

    node = db.get_team_mission_node("mission-1", "node-worker")
    assert node["status"] == "completed"
    assert node["metadata"]["last_run_id"] == "run-worker"
    assert node["metadata"]["last_run_terminal_status"] == "completed"
    assert node["metadata"]["last_run_terminal_seq"] == 1


def test_team_mission_duplicate_terminal_event_does_not_reduce_or_compile_memory_again(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        objective="Build",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.delta",
            "seq": 1,
            "payload": {"delta": "Keep one final memory summary.", "mode": "append"},
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 2,
            "payload": {"status": "complete"},
        },
    )
    duplicate = db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 3,
            "payload": {"status": "complete"},
        },
    )

    events = db.list_team_mission_run_events("mission-1")
    event_types = [event["type"] for event in events]
    source_event_types = [
        event["payload"]["source_event_type"]
        for event in events
        if event["type"] == "team_mission.runtime.event"
    ]

    assert duplicate["_persistence_disposition"] == "duplicate_terminal"
    assert source_event_types.count("message.complete") == 1
    assert source_event_types.count("mission.memory.compiled") == 1


def test_team_mission_late_stream_event_after_terminal_does_not_reopen_node(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        objective="Build",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.delta",
            "seq": 1,
            "payload": {"delta": "final text", "mode": "append"},
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 2,
            "payload": {"status": "complete"},
        },
    )
    late_delta = db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.delta",
            "seq": 3,
            "payload": {"delta": "late text", "mode": "append"},
        },
    )

    node = db.get_team_mission_node("mission-1", "node-worker")
    source_event_types = [
        event["payload"]["source_event_type"]
        for event in db.list_team_mission_run_events("mission-1")
        if event["type"] == "team_mission.runtime.event"
    ]

    assert late_delta["_persistence_disposition"] == "ignored_after_terminal"
    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_seq"] == 2
    assert source_event_types.count("message.delta") == 1
    assert source_event_types.count("message.complete") == 1


def test_team_mission_successful_terminal_event_is_not_overwritten_by_late_failure(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 10,
            "payload": {"status": "complete"},
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 12,
            "payload": {
                "status": "failed",
                "message": "late recovery failure must not downgrade success",
            },
        },
    )

    node = db.get_team_mission_node("mission-1", "node-worker")
    mission = db.get_team_mission_graph("mission-1")["mission"]
    run = db.get_run("run-worker")
    events = db.list_team_mission_run_events("mission-1")

    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_status"] == "completed"
    assert node["metadata"]["last_run_terminal_seq"] == 10
    assert mission["status"] != "failed"
    assert run["status"] == "completed"
    assert run["error"] == ""
    assert [
        event["payload"]["source_event"]["payload"]["status"]
        for event in events
        if (
            event["type"] == "team_mission.runtime.event"
            and event["payload"]["source_event_type"] == "message.complete"
        )
    ] == [
        "complete",
    ]


def test_team_mission_error_terminal_with_deliverable_text_completes_node(tmp_path: Path):
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-verifier",
        kind="worker",
        title="Verifier",
        status="running",
    )
    db.upsert_run(
        run_id="run-verifier",
        session_id="session-verifier",
        runtime_scope_key="team:mission-1:node:node-verifier",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-verifier",
        run_id="run-verifier",
        session_id="session-verifier",
        runtime_scope_key="team:mission-1:node:node-verifier",
        role="worker",
    )

    run_control.record_event(
        {
            "type": "subagent.output_delta",
            "session_id": "runtime-verifier",
            "stored_session_id": "session-verifier",
            "run_id": "run-verifier",
            "runtime_scope_key": "team:mission-1:node:node-verifier",
            "seq": 1,
            "payload": {"delta": "验收结论：工具步骤失败，但已形成可传递验证意见。"},
        },
        db=db,
    )
    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-verifier",
            "stored_session_id": "session-verifier",
            "run_id": "run-verifier",
            "runtime_scope_key": "team:mission-1:node:node-verifier",
            "seq": 2,
            "payload": {"status": "error", "message": "prompt worker terminal event did not close active run"},
        },
        db=db,
    )

    node = db.get_team_mission_node("mission-1", "node-verifier")
    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_status"] == "completed"


def test_team_mission_failed_complete_with_deliverable_text_completes_node_and_run(tmp_path: Path):
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-verifier",
        kind="verifier",
        title="Verifier",
        status="running",
    )
    db.upsert_run(
        run_id="run-verifier",
        session_id="session-verifier",
        runtime_scope_key="team:mission-1:node:node-verifier",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-verifier",
        run_id="run-verifier",
        session_id="session-verifier",
        runtime_scope_key="team:mission-1:node:node-verifier",
        role="worker",
    )

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-verifier",
            "stored_session_id": "session-verifier",
            "run_id": "run-verifier",
            "runtime_scope_key": "team:mission-1:node:node-verifier",
            "seq": 1,
            "payload": {
                "status": "failed",
                "text": "验收结论：需修正后交付，结构完整且可继续推进。",
                "message": "tool subprocess reported a nonfatal error",
            },
        },
        db=db,
    )

    node = db.get_team_mission_node("mission-1", "node-verifier")
    run = db.get_run("run-verifier")
    event = db.list_run_events("session-verifier")[0]

    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_status"] == "completed"
    assert run["status"] == "completed"
    assert run["error"] == ""
    assert event["payload"]["status"] == "complete"
    assert event["payload"]["team_mission_terminal_status_recovered"] == "failed"
    assert event["payload"]["nonfatal_error"] == "tool subprocess reported a nonfatal error"


def test_team_mission_late_success_clears_stale_run_error(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 10,
            "payload": {
                "status": "failed",
                "message": "fallback failure before worker terminal replay",
            },
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 12,
            "payload": {"status": "complete"},
        },
    )

    node = db.get_team_mission_node("mission-1", "node-worker")
    run = db.get_run("run-worker")

    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_status"] == "completed"
    assert node["metadata"]["last_run_terminal_seq"] == 12
    assert run["status"] == "completed"
    assert run["error"] == ""


def test_team_mission_terminal_run_event_compiles_structured_memory(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        objective="Create launch plan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Market research",
        objective="Find launch market",
        status="running",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.delta",
            "seq": 1,
            "payload": {"delta": "Launch market is Japan; use partner channel."},
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 2,
            "payload": {"status": "complete"},
        },
    )

    items = db.list_team_mission_memory_items(
        conversation_session_id="team-session-1",
        statuses=["committed"],
    )
    assert len(items) == 1
    assert items[0]["kind"] == "summary"
    assert "Japan" in items[0]["content"]
    assert items[0]["source_node_ids"] == ["node-worker"]
    assert items[0]["source_run_ids"] == ["run-worker"]

    events = db.list_team_mission_run_events("mission-1")
    memory_event = next(
        event for event in events
        if event["type"] == "team_mission.runtime.event"
        and event["payload"]["source_event_type"] == "mission.memory.compiled"
    )
    assert memory_event["payload"]["protocol"] == "team_mission.event.v1"
    assert memory_event["payload"]["kind"] == "memory.compiled"
    assert memory_event["payload"]["source_event"]["payload"]["memory_item_ids"] == [items[0]["id"]]


def test_team_mission_memory_pack_reuses_previous_task_in_same_conversation(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="First task",
        objective="Create launch plan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Market research",
        objective="Find launch market",
        status="completed",
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="completed",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    compiled = db.compile_team_mission_memory(mission_id="mission-1", emit_event=False)
    assert compiled["memory_item_ids"]

    db.upsert_team_mission(
        mission_id="mission-2",
        team_id="team-1",
        title="Second task",
        objective="Continue launch plan for Japan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-2"},
    )

    pack = db.build_team_mission_memory_pack(
        mission_id="mission-2",
        objective="Continue launch plan for Japan",
    )

    item_ids = pack["memory_pack"]["item_ids"]
    assert item_ids == compiled["memory_item_ids"]
    assert "team-session-1" == pack["conversation_session_id"]
    edges = db.list_team_mission_memory_edges(
        from_memory_id=item_ids[0],
        relation="referenced_by_task",
    )
    assert edges
    assert edges[0]["metadata"]["task_id"] == "task-2"


def test_team_mission_memory_compile_collects_runtime_artifact_sources(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        team_id="team-1",
        stable_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="交付任务",
        objective="生成并验证文件",
        mode="supervised_mission",
        status="running",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.upsert_run(
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_scope_key="team:mission-1:node:node-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="worker-session-1",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "artifact.created",
            "seq": 1,
            "payload": {
                "id": "artifact-output",
                "path": "/tmp/workspace/output.md",
                "title": "output.md",
                "kind": "file",
                "mime_type": "text/markdown",
            },
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "subagent.tool",
            "seq": 2,
            "payload": {
                "files_read": ["/tmp/workspace/input.csv"],
                "files_written": ["relative-report.md"],
            },
        },
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 3,
            "payload": {
                "status": "complete",
                "artifacts": [
                    {"path": "/tmp/workspace/final.pdf", "title": "final.pdf", "kind": "file"}
                ],
            },
        },
    )

    summary = db.get_team_mission_conversation_runtime_summary("conversation-1")
    [frame] = summary["task_frames"]
    artifacts_by_path = {
        ref.get("path"): ref
        for ref in frame["artifactRefs"]
        if ref.get("path")
    }
    assert set(artifacts_by_path) == {
        "/tmp/workspace/output.md",
        "/tmp/workspace/input.csv",
        "relative-report.md",
        "/tmp/workspace/final.pdf",
    }
    assert artifacts_by_path["/tmp/workspace/input.csv"]["kind"] == "file_read"
    assert summary["artifact_refs"] == frame["artifactRefs"]


def test_team_mission_memory_pack_isolates_different_conversations_by_default(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="First conversation",
        objective="Create launch plan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    memory_item = db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        scope="mission_task",
        kind="summary",
        content="Previous filescan.py delivery should not leak into a new conversation.",
        source_node_ids=["node-old"],
        source_run_ids=["run-old"],
        visibility="team",
    )
    assert memory_item["id"]
    db.upsert_team_mission(
        mission_id="mission-2",
        team_id="team-1",
        title="Fresh conversation",
        objective="你好",
        mode="supervised_mission",
        metadata={"stableTeamSessionId": "team-session-2", "task_id": "task-2"},
    )

    isolated_pack = db.build_team_mission_memory_pack(
        mission_id="mission-2",
        objective="你好",
    )
    team_scope_pack = db.build_team_mission_memory_pack(
        mission_id="mission-2",
        objective="你好",
        include_team_scope=True,
    )

    assert isolated_pack["conversation_session_id"] == "team-session-2"
    assert isolated_pack["memory_pack"]["item_ids"] == []
    assert team_scope_pack["memory_pack"]["item_ids"] == [memory_item["id"]]


def test_team_mission_memory_slice_filters_worker_visibility(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        objective="Build launch plan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="completed",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-b",
        kind="worker",
        title="B",
        objective="Use launch constraints",
        status="ready",
    )
    db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id="node-a",
        to_node_id="node-b",
        kind="depends_on",
    )
    public_item = db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        scope="conversation",
        kind="constraint",
        content="Launch constraints: use partner channel.",
        source_node_ids=["node-a"],
        source_run_ids=["run-a"],
        visibility="team",
    )
    private_item = db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        scope="conversation",
        kind="decision",
        content="Leader-only budget assumption.",
        source_node_ids=["node-a"],
        source_run_ids=["run-a"],
        visibility="leader_only",
    )

    memory_slice = db.build_team_mission_memory_slice(
        mission_id="mission-1",
        node_id="node-b",
        objective="Use launch constraints",
    )

    item_ids = memory_slice["memory_slice"]["item_ids"]
    assert public_item["id"] in item_ids
    assert private_item["id"] not in item_ids
    assert memory_slice["memory_slice"]["dependency_node_ids"] == ["node-a"]


def test_deleted_team_mission_memory_is_excluded_from_pack(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        objective="Build launch plan",
        mode="autonomous_mission",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    item = db.upsert_team_mission_memory_item(
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        scope="conversation",
        kind="summary",
        content="Reusable launch summary.",
        source_node_ids=["node-a"],
        source_run_ids=["run-a"],
        visibility="team",
    )
    db.delete_team_mission_memory_item(item["id"])

    pack = db.build_team_mission_memory_pack(
        mission_id="mission-1",
        objective="launch",
    )

    assert pack["memory_pack"]["item_ids"] == []


def test_team_mission_graph_reducer_unlocks_dependency_after_parent_completion(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="ready",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-b",
        kind="worker",
        title="B",
        status="todo",
    )
    db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id="node-a",
        to_node_id="node-b",
        kind="depends_on",
    )

    first = db.reduce_team_mission_graph("mission-1")
    assert first["ready_node_ids"] == ["node-a"]
    assert db.get_team_mission_node("mission-1", "node-b")["status"] == "blocked_waiting_dependency"

    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="completed",
    )
    second = db.reduce_team_mission_graph("mission-1")

    assert second["ready_node_ids"] == ["node-b"]
    assert db.get_team_mission_node("mission-1", "node-b")["status"] == "ready"


def test_team_mission_graph_reducer_marks_dependency_waiting_mission_without_blocking(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-b",
        kind="worker",
        title="B",
        status="todo",
    )
    db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id="node-a",
        to_node_id="node-b",
        kind="depends_on",
    )

    reduced = db.reduce_team_mission_graph("mission-1")

    assert reduced["ready_node_ids"] == []
    assert reduced["mission_status"] == "waiting_dependency"
    assert db.get_team_mission_node("mission-1", "node-b")["status"] == "blocked_waiting_dependency"


def test_team_mission_graph_reducer_does_not_complete_supervised_mission_after_approval_only(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="supervised_mission",
        status="waiting_approval",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="root",
        kind="root",
        title="Plan",
        status="completed",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="approval",
        kind="approval_gate",
        title="Approve graph",
        status="completed",
    )

    reduced = db.reduce_team_mission_graph("mission-1")

    assert reduced["mission_status"] == "running"
    assert reduced["ready_node_ids"] == []
    assert db.get_team_mission_graph("mission-1")["mission"]["status"] == "running"


def test_team_mission_node_start_claim_is_atomic(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="ready",
    )

    first = db.claim_team_mission_node_start(
        mission_id="mission-1",
        node_id="node-a",
        metadata={"scheduler_trigger": "test"},
    )
    second = db.claim_team_mission_node_start(
        mission_id="mission-1",
        node_id="node-a",
        metadata={"scheduler_trigger": "test-duplicate"},
    )

    assert first["status"] == "starting"
    assert first["metadata"]["scheduler_trigger"] == "test"
    assert second["status"] == "starting"
    assert db.get_team_mission_node("mission-1", "node-a")["metadata"]["scheduler_trigger"] == "test"


def test_team_mission_graph_reducer_creates_verifier_and_synthesis_for_execution_modes(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        metadata={
            "members": [
                {"member_id": "leader", "profile_id": "profile-leader", "role": "leader"},
                {"member_id": "builder", "profile_id": "profile-builder", "role": "builder"},
            ],
        },
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="A",
        status="completed",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-b",
        kind="worker",
        title="B",
        status="completed",
    )

    verifier_step = db.reduce_team_mission_graph("mission-1")
    verifier_nodes = [node for node in verifier_step["graph"]["nodes"] if node["kind"] == "verifier"]
    assert [node["node_id"] for node in verifier_nodes] == ["team-mission:mission-1:verifier"]
    assert verifier_nodes[0]["status"] == "ready"
    assert verifier_nodes[0]["assignee_member_id"] == "leader"
    assert verifier_nodes[0]["assignee_profile_id"] == "profile-leader"
    assert verifier_step["ready_node_ids"] == ["team-mission:mission-1:verifier"]
    assert {
        (edge["from_node_id"], edge["to_node_id"])
        for edge in verifier_step["graph"]["edges"]
        if edge["metadata"].get("auto_finalizer")
    } == {
        ("node-a", "team-mission:mission-1:verifier"),
        ("node-b", "team-mission:mission-1:verifier"),
    }
    verifier_events = db.list_team_mission_events("mission-1")
    verifier_node_events = [
        event for event in verifier_events
        if event["payload"]["source_event_type"] == "mission.node.created"
    ]
    assert [event["payload"]["node_id"] for event in verifier_node_events] == ["team-mission:mission-1:verifier"]

    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:verifier",
        kind="verifier",
        title="验收执行结果",
        status="completed",
    )
    synthesis_step = db.reduce_team_mission_graph("mission-1")
    synthesis_nodes = [node for node in synthesis_step["graph"]["nodes"] if node["kind"] == "synthesis"]

    assert [node["node_id"] for node in synthesis_nodes] == ["team-mission:mission-1:synthesis"]
    assert synthesis_nodes[0]["status"] == "ready"
    assert synthesis_nodes[0]["assignee_member_id"] == "leader"
    assert synthesis_nodes[0]["assignee_profile_id"] == "profile-leader"
    assert synthesis_step["ready_node_ids"] == ["team-mission:mission-1:synthesis"]
    synthesis_events = db.list_team_mission_events("mission-1")
    finalizer_node_events = [
        event for event in synthesis_events
        if event["payload"]["source_event_type"] == "mission.node.created"
    ]
    assert [event["payload"]["node_id"] for event in finalizer_node_events] == [
        "team-mission:mission-1:verifier",
        "team-mission:mission-1:synthesis",
    ]
    db.reduce_team_mission_graph("mission-1")
    assert [
        event["payload"]["node_id"]
        for event in db.list_team_mission_events("mission-1")
        if event["payload"]["source_event_type"] == "mission.node.created"
    ] == [
        "team-mission:mission-1:verifier",
        "team-mission:mission-1:synthesis",
    ]


def test_team_mission_node_upsert_normalizes_kind_and_replaces_invalid_member_assignee(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        metadata={
            "members": [
                {"member_id": "leader", "profile_id": "profile-leader", "role": "leader"},
                {"member_id": "builder", "profile_id": "profile-builder", "role": "builder"},
            ],
        },
    )

    node = db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-legacy-summary",
        kind="synthesizer",
        title="Legacy summary",
        status="ready",
        metadata={
            "assignee_member_id": "run-leader",
            "assigneeMemberId": "run-leader",
        },
    )

    assert node["kind"] == "synthesis"
    assert node["metadata"]["original_kind"] == "synthesizer"
    assert node["assignee_member_id"] == "leader"
    assert node["assignee_profile_id"] == "profile-leader"
    assert node["metadata"]["assignee_member_id"] == "leader"
    assert node["metadata"]["assigneeMemberId"] == "leader"
    assert node["metadata"]["assignee_resolved_by"] == "default_leader"


def test_team_mission_graph_read_resolves_legacy_invalid_member_assignee(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        metadata={
            "members": [
                {"member_id": "leader", "profile_id": "profile-leader", "role": "leader"},
                {"member_id": "builder", "profile_id": "profile-builder", "role": "builder"},
            ],
        },
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-summary",
        kind="synthesis",
        title="Summary",
        status="ready",
    )

    def _corrupt_assignee(conn):
        conn.execute(
            """
            UPDATE team_mission_nodes
               SET assignee_profile_id = ?,
                   assignee_profile_version_id = ?,
                   runtime_scope_key = ?,
                   metadata_json = ?
             WHERE mission_id = ? AND node_id = ?
            """,
            (
                "",
                "",
                "",
                json.dumps({
                    "assignee_member_id": "run-leader",
                    "assigneeMemberId": "run-leader",
                }),
                "mission-1",
                "node-summary",
            ),
        )

    db._execute_write(_corrupt_assignee)

    node = db.get_team_mission_node("mission-1", "node-summary")
    graph_node = next(item for item in db.get_team_mission_graph("mission-1")["nodes"] if item["node_id"] == "node-summary")

    assert node["assignee_member_id"] == "leader"
    assert node["assignee_profile_id"] == "profile-leader"
    assert node["metadata"]["assigneeMemberId"] == "leader"
    assert graph_node["assignee_member_id"] == "leader"
    assert graph_node["assignee_profile_id"] == "profile-leader"


def test_team_mission_graph_reducer_treats_verification_kind_as_worker_work_type(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        metadata={
            "members": [
                {"member_id": "leader", "profile_id": "profile-leader", "role": "leader"},
                {"member_id": "builder", "profile_id": "profile-builder", "role": "builder"},
            ],
        },
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-verification-work",
        kind="verification",
        title="Verification work",
        status="completed",
        assignee_profile_id="profile-builder",
    )

    work_node = db.get_team_mission_node("mission-1", "node-verification-work")
    assert work_node["kind"] == "worker"
    assert work_node["metadata"]["work_type"] == "verification"

    reduced = db.reduce_team_mission_graph("mission-1")
    verifier_nodes = [node for node in reduced["graph"]["nodes"] if node["kind"] == "verifier"]

    assert [node["node_id"] for node in verifier_nodes] == ["team-mission:mission-1:verifier"]
    assert verifier_nodes[0]["assignee_member_id"] == "leader"


def test_team_mission_graph_reducer_does_not_duplicate_legacy_synthesis_alias(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="autonomous_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="completed",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-verifier",
        kind="verifier",
        title="Verifier",
        status="completed",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-legacy-synthesis",
        kind="synthesizer",
        title="Legacy synthesis",
        status="ready",
    )

    reduced = db.reduce_team_mission_graph("mission-1")
    synthesis_nodes = [node for node in reduced["graph"]["nodes"] if node["kind"] == "synthesis"]

    assert [node["node_id"] for node in synthesis_nodes] == ["node-legacy-synthesis"]


def test_team_mission_graph_reducer_scopes_finalizers_to_active_task(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="创建文件扫描工具",
        objective="创建 filescan.py",
        mode="autonomous_mission",
        metadata={"active_task_id": "task-2", "task_id": "task-2"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="old-worker",
        kind="worker",
        title="旧问候任务",
        status="completed",
        metadata={"task_id": "task-1", "task_objective": "你好"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="current-worker",
        kind="worker",
        title="创建文件扫描工具",
        status="completed",
        metadata={"task_id": "task-2", "task_objective": "创建 filescan.py"},
    )

    verifier_step = db.reduce_team_mission_graph("mission-1")
    verifier_id = "team-mission:mission-1:task-2:verifier"
    verifier_nodes = [node for node in verifier_step["graph"]["nodes"] if node["kind"] == "verifier"]
    assert [node["node_id"] for node in verifier_nodes] == [verifier_id]
    assert verifier_nodes[0]["metadata"]["task_id"] == "task-2"
    assert {
        (edge["from_node_id"], edge["to_node_id"])
        for edge in verifier_step["graph"]["edges"]
        if edge["metadata"].get("auto_finalizer")
    } == {("current-worker", verifier_id)}

    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id=verifier_id,
        kind="verifier",
        title="验收执行结果",
        status="completed",
        metadata={"task_id": "task-2"},
    )
    synthesis_step = db.reduce_team_mission_graph("mission-1")
    synthesis_nodes = [node for node in synthesis_step["graph"]["nodes"] if node["kind"] == "synthesis"]
    assert [node["node_id"] for node in synthesis_nodes] == ["team-mission:mission-1:task-2:synthesis"]
    assert synthesis_nodes[0]["metadata"]["task_id"] == "task-2"


def test_reap_terminal_mission_runs_clears_zombie_running_run(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="cancelled",
    )
    db.upsert_run(
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="verify-stats-report",
        run_id="run-verify",
        session_id="team:mission-1:node:verify-stats-report",
        runtime_scope_key="team:mission-1:node:verify-stats-report",
    )

    reaped = db.reap_terminal_mission_runs("mission-1")

    assert reaped == 1
    assert db.get_run("run-verify")["status"] in {"interrupted", "cancelled", "canceled"}


def test_reap_terminal_mission_runs_leaves_active_mission_runs(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_run(
        run_id="run-impl",
        session_id="team:mission-1:node:impl",
        runtime_scope_key="team:mission-1:node:impl",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="impl",
        run_id="run-impl",
        session_id="team:mission-1:node:impl",
        runtime_scope_key="team:mission-1:node:impl",
    )

    reaped = db.reap_terminal_mission_runs("mission-1")

    assert reaped == 0
    assert db.get_run("run-impl")["status"] == "running"
