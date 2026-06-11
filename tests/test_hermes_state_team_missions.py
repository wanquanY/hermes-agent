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
        runtime_scope_key="team:mission-1:leader",
        role="leader",
    )

    graph = db.get_team_mission_graph("mission-1")

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

    resolved = db.resolve_team_mission_conversation("conversation-1")

    assert resolved["conversation"]["conversation_id"] == "conversation-1"
    assert resolved["conversation"]["stable_session_id"] == "team-session-1"
    assert resolved["mission"]["mission_id"] == "mission-1"
    assert resolved["graph"]["nodes"][0]["node_id"] == "node-1"
    assert db.get_session("team-session-1")["source"] == "team_mission"


def test_team_mission_conversation_rename_updates_canonical_session_without_mutating_mission_title(tmp_path: Path):
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
    assert db.get_team_mission_graph("mission-1")["mission"]["title"] == "旧标题"
    assert db.get_session("team-session-1")["title"] == "新团队任务"


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
    assert resolved["conversation"]["active_mission_id"] == "mission-1"
    assert resolved["mission"]["title"] == "用户第一条任务消息"


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

    assert [item["conversation_id"] for item in db.list_team_mission_conversations()] == [
        "newer-created-conversation",
        "old-conversation",
    ]

    db.create_session("old-team-session", source="team_mission", transient=False)
    db.append_message("old-team-session", role="user", content="真实新消息")

    conversations = db.list_team_mission_conversations()

    assert [item["conversation_id"] for item in conversations] == [
        "old-conversation",
        "newer-created-conversation",
    ]
    assert conversations[0]["updated_at"] > conversations[1]["updated_at"]


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
    assert db.resolve_team_mission_conversation("conversation-1") == {}
    assert db.get_team_mission_graph("mission-1") == {}
    assert db.get_team_mission_node("mission-1", "node-worker") == {}
    assert db.list_team_mission_memory_items(conversation_session_id="team-session-1") == []
    assert db.list_team_mission_memory_edges(from_memory_id=memory_item["id"]) == []
    assert db.get_session("team-session-1")["source"] == "team_mission"


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


def test_team_mission_runtime_events_reuse_ordinary_run_event_coalescing(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="supervised_mission",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-leader",
        kind="root",
        title="Plan",
        status="running",
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
        "message.delta",
        "tool.start",
        "message.delta",
    ]
    assert events[0]["source_seq"] == 2
    assert events[0]["team_mission_event_seq"] == events[0]["seq"]
    assert events[0]["payload"]["delta"] == "Plan graph"
    assert events[0]["payload"]["mission_id"] == "mission-1"
    assert events[0]["payload"]["node_id"] == "node-leader"
    assert events[2]["payload"]["delta"] == " after tool"


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
    assert events[-1]["type"] == "mission.memory.compiled"
    assert events[-1]["payload"]["memory_item_ids"] == [items[0]["id"]]


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
