from pathlib import Path


class _MemoryTransport:
    def __init__(self):
        self.frames = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


def test_synthesis_empty_complete_closes_conversation_mirror_run(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesis",
        title="汇总交付",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )
    artifact_refs = [
        {"path": "/tmp/final-report.md", "title": "final-report.md", "kind": "file"},
    ]
    transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )
    try:
        for seq, event_type, payload in (
            (1, "message.start", {}),
            (2, "message.delta", {"delta": "最终汇总", "text": "最终汇总"}),
            (3, "message.complete", {"status": "complete", "artifact_refs": artifact_refs}),
        ):
            run_control.record_event(
                {
                    "type": event_type,
                    "session_id": "runtime-synthesis",
                    "stored_session_id": "synthesis-session-1",
                    "run_id": "run-synthesis",
                    "turn_id": "turn-synthesis",
                    "runtime_scope_key": "team:mission-1:synthesis",
                    "seq": seq,
                    "payload": payload,
                },
                db=db,
            )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)

    mirror_run_id = "team-mission:mission-1:conversation:run-synthesis"
    mirrored_events = db.list_run_events("team-session-1")
    assert [event["type"] for event in mirrored_events] == [
        "message.start",
        "message.delta",
        "message.complete",
    ]
    mirrored_complete = mirrored_events[2]
    assert mirrored_complete["run_id"] == mirror_run_id
    assert mirrored_complete["payload"]["status"] == "complete"
    assert mirrored_complete["payload"]["team_mission_conversation_mirror"] is True
    assert mirrored_complete["payload"]["team_mission_final_deliverable"] is True
    assert mirrored_complete["payload"]["text"] == "最终汇总"
    assert mirrored_complete["conversation_id"] == "conversation-1"
    assert mirrored_complete["stable_session_id"] == "team-session-1"
    assert mirrored_complete["task_id"] == "task-1"
    assert mirrored_complete["task_frame_id"] == "mission-frame:mission-1"
    assert mirrored_complete["source_seq"] == "3"
    assert mirrored_complete["payload"]["conversation_id"] == "conversation-1"
    assert mirrored_complete["payload"]["conversationId"] == "conversation-1"
    assert mirrored_complete["payload"]["stable_session_id"] == "team-session-1"
    assert mirrored_complete["payload"]["task_id"] == "task-1"
    assert mirrored_complete["payload"]["taskFrameId"] == "mission-frame:mission-1"
    assert mirrored_complete["payload"]["source_seq"] == "3"
    assert db.get_run(mirror_run_id)["status"] == "completed"
    assert db.get_session_run_status("team-session-1")["running"] is False

    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    assert [event["type"] for event in streamed] == [
        "message.start",
        "message.delta",
        "message.complete",
    ]
    assert streamed[2]["run_id"] == mirror_run_id
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "最终汇总"
    assert messages[0]["metadata"]["team_mission"]["kind"] == "final_deliverable"
    assert messages[0]["metadata"]["team_mission"]["artifactRefs"] == artifact_refs
    summary = db.get_team_mission_conversation_runtime_summary("conversation-1")
    assert summary["final_deliverables"][0]["artifactRefs"] == artifact_refs
    assert summary["task_frames"][0]["artifactRefs"] == artifact_refs


def test_synthesis_cumulative_deltas_mirror_as_append_suffixes(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesis",
        title="汇总交付",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )

    transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )
    try:
        for seq, event_type, payload in (
            (1, "message.start", {}),
            (2, "message.delta", {"mode": "append", "delta": "团", "text": "团"}),
            (3, "message.delta", {"mode": "append", "delta": "团队", "text": "团队"}),
            (4, "message.delta", {"mode": "append", "delta": "团队协作", "text": "团队协作"}),
            (5, "message.complete", {"status": "complete"}),
        ):
            run_control.record_event(
                {
                    "type": event_type,
                    "session_id": "runtime-synthesis",
                    "stored_session_id": "synthesis-session-1",
                    "run_id": "run-synthesis",
                    "turn_id": "turn-synthesis",
                    "runtime_scope_key": "team:mission-1:synthesis",
                    "seq": seq,
                    "payload": payload,
                },
                db=db,
            )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)

    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    streamed_deltas = [event for event in streamed if event["type"] == "message.delta"]
    assert [event["payload"]["delta"] for event in streamed_deltas] == ["团", "队", "协作"]
    assert [event["payload"]["text"] for event in streamed_deltas] == ["团", "队", "协作"]
    assert [event["payload"]["offset"] for event in streamed_deltas] == [0, 1, 2]

    mirrored_events = db.list_run_events("team-session-1")
    assert [event["type"] for event in mirrored_events] == [
        "message.start",
        "message.delta",
        "message.complete",
    ]
    assert mirrored_events[1]["payload"]["text"] == "团队协作"
    assert mirrored_events[1]["payload"]["delta"] == "团队协作"
    assert mirrored_events[2]["payload"]["text"] == "团队协作"
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["content"] == "团队协作"


def test_team_mission_poll_delivers_domain_projection_for_directly_delivered_node_stream_tail(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesis",
        title="汇总交付",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )

    transport = _MemoryTransport()
    node_subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="synthesis-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )
    mission_subscription_id, _ = run_control.subscribe_team_mission_with_id(
        mission_id="mission-1",
        transport=transport,
        db=db,
    )
    try:
        for seq, offset, chunk in (
            (1, 0, "最终"),
            (2, 2, "交付"),
            (3, 4, "完成"),
        ):
            run_control.publish_recorded_event(
                {
                    "type": "message.delta",
                    "session_id": "runtime-synthesis",
                    "stored_session_id": "synthesis-session-1",
                    "run_id": "run-synthesis",
                    "turn_id": "turn-synthesis",
                    "runtime_scope_key": "team:mission-1:synthesis",
                    "seq": seq,
                    "payload": {
                        "mode": "append",
                        "delta": chunk,
                        "text": chunk,
                        "offset": offset,
                    },
                },
                db=db,
            )

        streamed = [
            frame.get("params") or {}
            for frame in transport.frames
            if frame.get("method") == "event"
        ]
        assert [event["payload"]["delta"] for event in streamed] == ["最终", "交付", "完成"]

        mission_deltas = [
            event
            for event in db.list_team_mission_run_events("mission-1", after_seq=0)
            if (
                event["type"] == "team_mission.runtime.event"
                and event["payload"]["source_event_type"] == "message.delta"
            )
        ]
        assert mission_deltas
        assert all(event.get("mission_id") == "mission-1" for event in mission_deltas)
        assert all(event["payload"]["source_event"]["type"] == "message.delta" for event in mission_deltas)

        subscription = run_control._subscriptions_by_id[mission_subscription_id]
        redelivered = [
            run_control._delta_event_for_subscription(subscription, event)
            for event in mission_deltas
        ]
        assert redelivered == mission_deltas
    finally:
        run_control.unsubscribe_session(subscription_id=node_subscription_id)
        run_control.unsubscribe_session(subscription_id=mission_subscription_id)


def test_team_mission_poll_delivers_domain_projection_for_directly_delivered_node_terminal(tmp_path: Path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:root",
        kind="root",
        title="规划",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:root",
        run_id="run-root",
        session_id="root-session-1",
        runtime_session_id="runtime-root",
        runtime_scope_key="team:mission-1:root",
        role="leader",
    )

    transport = _MemoryTransport()
    node_subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="root-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )
    mission_subscription_id, _ = run_control.subscribe_team_mission_with_id(
        mission_id="mission-1",
        transport=transport,
        db=db,
    )
    try:
        direct_event = {
            "type": "message.complete",
            "session_id": "runtime-root",
            "stored_session_id": "root-session-1",
            "run_id": "run-root",
            "turn_id": "turn-root",
            "runtime_scope_key": "team:mission-1:root",
            "seq": 1168,
            "payload": {"status": "complete", "text": "规划完成"},
        }
        run_control.remember_transport_delivery(transport, direct_event)
        db.append_run_event("root-session-1", direct_event)

        mission_terminals = [
            event
            for event in db.list_team_mission_run_events("mission-1", after_seq=0)
            if (
                event["type"] == "team_mission.runtime.event"
                and event["payload"]["source_event_type"] == "message.complete"
            )
        ]
        assert mission_terminals
        assert mission_terminals[0]["seq"] != direct_event["seq"]
        assert mission_terminals[0]["source_seq"] == direct_event["seq"]
        assert mission_terminals[0]["payload"]["source_event"]["type"] == "message.complete"

        subscription = run_control._subscriptions_by_id[mission_subscription_id]
        redelivered = [
            run_control._delta_event_for_subscription(subscription, event)
            for event in mission_terminals
        ]
        assert redelivered == mission_terminals
    finally:
        run_control.unsubscribe_session(subscription_id=node_subscription_id)
        run_control.unsubscribe_session(subscription_id=mission_subscription_id)


def test_conversation_resolve_recovers_legacy_empty_final_deliverable_message(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        status="completed",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesis",
        title="汇总交付",
        status="done",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )
    mirror_run_id = "team-mission:mission-1:conversation:run-synthesis"
    common_payload = {
        "team_mission_conversation_mirror": True,
        "team_mission_final_deliverable": True,
        "mission_id": "mission-1",
        "node_id": "team-mission:mission-1:synthesis",
        "source_run_id": "run-synthesis",
        "source_session_id": "synthesis-session-1",
        "source_seq": "42",
    }
    db.append_run_event("team-session-1", {
        "type": "message.delta",
        "stored_session_id": "team-session-1",
        "run_id": mirror_run_id,
        "turn_id": "turn-synthesis",
        "runtime_scope_key": "team_mission:mission-1",
        "payload": {
            **common_payload,
            "mode": "snapshot",
            "text": "旧任务最终汇总",
        },
    })
    db.append_run_event("team-session-1", {
        "type": "message.complete",
        "stored_session_id": "team-session-1",
        "run_id": mirror_run_id,
        "turn_id": "turn-synthesis",
        "runtime_scope_key": "team_mission:mission-1",
        "payload": {
            **common_payload,
            "status": "complete",
        },
    })
    assert db.get_messages("team-session-1") == []

    response = server._methods["team_mission.conversation.resolve"](
        1,
        {"identifier": "conversation-1"},
    )
    assert response["result"]["conversation"]["conversation_id"] == "conversation-1"
    assert response["result"]["messages"][0]["text"] == "旧任务最终汇总"
    assert response["result"]["graph"]["recent_messages"][0]["text"] == "旧任务最终汇总"
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "旧任务最终汇总"

    server._methods["team_mission.conversation.resolve"](
        2,
        {"identifier": "conversation-1"},
    )
    assert len(db.get_messages("team-session-1")) == 1


def test_final_deliverable_complete_prefers_source_markdown_over_polluted_mirror_history(
    tmp_path: Path,
):
    from hermes_state import SessionDB
    from hermes_team_mission_conversation_utils import mirror_event_to_conversation

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesis",
        title="汇总交付",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )
    source_markdown = (
        "## ✅ 团队任务执行结果整合\n\n"
        "本次团队任务目标：**创建一个测试文件。**\n\n"
        "---\n\n"
        "## 一、执行结果\n\n"
        "```text\n"
        "创建测试文件 → 验证测试文件\n"
        "```\n"
    )
    polluted_mirror_text = (
        "##✅团队任务执行结果整合本"
        "## ✅ 团队任务执行结果整合\n\n"
        "本次团队任务目标：**创建一个测试文件。**---##一、执行结果"
    )
    db.append_run_event("synthesis-session-1", {
        "type": "message.delta",
        "stored_session_id": "synthesis-session-1",
        "run_id": "run-synthesis",
        "turn_id": "turn-synthesis",
        "runtime_scope_key": "team:mission-1:synthesis",
        "seq": 7,
        "payload": {"mode": "append", "text": source_markdown, "delta": source_markdown},
    })
    mirror_run_id = "team-mission:mission-1:conversation:run-synthesis"
    db.append_run_event("team-session-1", {
        "type": "message.delta",
        "stored_session_id": "team-session-1",
        "run_id": mirror_run_id,
        "turn_id": "turn-synthesis",
        "runtime_scope_key": "team_mission:mission-1",
        "seq": 8,
        "payload": {
            "mode": "append",
            "text": polluted_mirror_text,
            "delta": polluted_mirror_text,
            "mission_id": "mission-1",
            "node_id": "team-mission:mission-1:synthesis",
            "source_run_id": "run-synthesis",
            "source_session_id": "synthesis-session-1",
            "team_mission_conversation_mirror": True,
            "team_mission_final_deliverable": True,
        },
    })
    bad_message_id = db.append_message(
        "team-session-1",
        "assistant",
        polluted_mirror_text,
        metadata={
            "team_mission": {
                "kind": "final_deliverable",
                "mission_id": "mission-1",
                "node_id": "team-mission:mission-1:synthesis",
                "source_run_id": "run-synthesis",
                "source_session_id": "synthesis-session-1",
            }
        },
    )

    saved = mirror_event_to_conversation(
        db,
        mission_id="mission-1",
        event={
            "type": "message.complete",
            "session_id": "runtime-synthesis",
            "stored_session_id": "synthesis-session-1",
            "run_id": "run-synthesis",
            "turn_id": "turn-synthesis",
            "runtime_scope_key": "team:mission-1:synthesis",
            "seq": 42,
            "payload": {"status": "complete"},
        },
    )

    assert saved["payload"]["text"] == source_markdown.strip()
    assert saved["payload"]["text"] != polluted_mirror_text
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["id"] == bad_message_id
    assert messages[0]["content"] == source_markdown.strip()
    assert "\n\n---\n\n## 一、执行结果" in messages[0]["content"]


def test_conversation_resolve_repairs_polluted_final_deliverable_from_source_history(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        status="completed",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        kind="synthesis",
        title="汇总交付",
        status="done",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="team-mission:mission-1:synthesis",
        run_id="run-synthesis",
        session_id="synthesis-session-1",
        runtime_session_id="runtime-synthesis",
        runtime_scope_key="team:mission-1:synthesis",
        role="member",
    )
    source_markdown = (
        "## ✅ 团队任务执行结果整合\n\n"
        "本次团队任务目标：**创建一个测试文件。**\n\n"
        "---\n\n"
        "## 一、执行结果\n\n"
        "| 节点 | 状态 |\n"
        "|---|---|\n"
        "| 创建测试文件 | ✅ 已完成 |\n"
    )
    polluted_text = (
        "##✅团队任务执行结果整合本## ✅ 团队任务执行结果整合\n\n"
        "本次团队任务目标：**创建一个测试文件。**---##一、执行结果|节点|状态|"
    )
    db.append_run_event("synthesis-session-1", {
        "type": "message.delta",
        "stored_session_id": "synthesis-session-1",
        "run_id": "run-synthesis",
        "turn_id": "turn-synthesis",
        "runtime_scope_key": "team:mission-1:synthesis",
        "seq": 7,
        "payload": {"mode": "append", "text": source_markdown, "delta": source_markdown},
    })
    mirror_run_id = "team-mission:mission-1:conversation:run-synthesis"
    common_payload = {
        "team_mission_conversation_mirror": True,
        "team_mission_final_deliverable": True,
        "mission_id": "mission-1",
        "node_id": "team-mission:mission-1:synthesis",
        "source_run_id": "run-synthesis",
        "source_session_id": "synthesis-session-1",
        "source_seq": "42",
    }
    db.append_run_event("team-session-1", {
        "type": "message.delta",
        "stored_session_id": "team-session-1",
        "run_id": mirror_run_id,
        "turn_id": "turn-synthesis",
        "runtime_scope_key": "team_mission:mission-1",
        "seq": 7,
        "payload": {
            **common_payload,
            "mode": "append",
            "text": polluted_text,
            "delta": polluted_text,
        },
    })
    db.append_run_event("team-session-1", {
        "type": "message.complete",
        "stored_session_id": "team-session-1",
        "run_id": mirror_run_id,
        "turn_id": "turn-synthesis",
        "runtime_scope_key": "team_mission:mission-1",
        "seq": 8,
        "payload": {
            **common_payload,
            "status": "complete",
            "text": polluted_text,
        },
    })
    bad_message_id = db.append_message(
        "team-session-1",
        "assistant",
        polluted_text,
        metadata={"team_mission": {"kind": "final_deliverable", **common_payload}},
    )

    response = server._methods["team_mission.conversation.resolve"](
        1,
        {"identifier": "conversation-1"},
    )

    assert response["result"]["messages"][0]["text"] == source_markdown.strip()
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["id"] == bad_message_id
    assert messages[0]["content"] == source_markdown.strip()
    assert "\n\n---\n\n## 一、执行结果" in messages[0]["content"]
    events = db.list_run_events("team-session-1", run_id=mirror_run_id)
    delta_event = next(event for event in events if event["type"] == "message.delta")
    complete_event = next(event for event in events if event["type"] == "message.complete")
    assert delta_event["payload"]["mode"] == "snapshot"
    assert delta_event["payload"]["snapshot"] == source_markdown.strip()
    assert "delta" not in delta_event["payload"]
    assert complete_event["payload"]["text"] == source_markdown.strip()


def test_conversation_projection_exposes_final_deliverable_artifacts_to_list_and_resolve(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    artifact_refs = [
        {"path": "/tmp/final.md", "title": "final.md", "kind": "file"},
        {"path": "/tmp/chart.png", "title": "chart.png", "kind": "image"},
    ]
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.create_session("team-session-1", source="team_mission", transient=False)
    user_message_id = db.append_message("team-session-1", "user", "请生成最终报告")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="交付任务",
        objective="生成最终报告",
        mode="supervised_mission",
        status="completed",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1", "task_id": "task-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="synthesis",
        kind="synthesis",
        title="最终汇总",
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
                "task_id": "task-1",
                "node_id": "synthesis",
                "source_run_id": "run-synthesis",
                "source_session_id": "synthesis-session-1",
                "source_seq": "42",
            },
        },
    )
    db.upsert_team_mission_memory_item(
        memory_id="memory-artifacts-1",
        team_id="team-1",
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        task_id="task-1",
        content="最终报告文件",
        source_node_ids=["synthesis"],
        source_run_ids=["run-synthesis"],
        artifact_refs=artifact_refs,
    )

    list_response = server._methods["team_mission.conversation.list"](
        1,
        {"team_id": "team-1"},
    )
    listed = list_response["result"]["conversations"][0]
    assert listed["last_message_preview"] == "最终交付内容"
    assert listed["last_message"]["messageId"] == str(message_id)
    assert listed["last_message"]["teamMission"]["kind"] == "final_deliverable"
    assert listed["last_message"]["teamMission"]["artifactRefs"] == artifact_refs
    assert listed["artifact_refs"] == artifact_refs
    assert listed["final_deliverables"][0]["messageId"] == str(message_id)
    assert listed["final_deliverables"][0]["artifactRefs"] == artifact_refs

    resolve_response = server._methods["team_mission.conversation.resolve"](
        2,
        {"identifier": "conversation-1"},
    )
    resolved = resolve_response["result"]
    resolved_conversation = resolved["conversation"]
    graph = resolved["graph"]
    assert [message["text"] for message in resolved["messages"]] == ["请生成最终报告", "最终交付内容"]
    assert [message["message_id"] for message in graph["recent_messages"]] == [
        str(user_message_id),
        str(message_id),
    ]
    assert graph["message_page_info"]["totalCount"] == 2
    assert graph["messagePageInfo"] == graph["message_page_info"]
    assert graph["last_message"]["teamMission"]["artifactRefs"] == artifact_refs
    assert resolved_conversation["artifact_refs"] == artifact_refs
    assert resolved_conversation["final_deliverables"][0]["artifactRefs"] == artifact_refs
    assert graph["final_deliverables"][0]["artifactRefs"] == artifact_refs
    assert graph["task_frames"][0]["finalDeliverable"]["artifactRefs"] == artifact_refs
    assert graph["task_frames"][0]["artifactRefs"] == artifact_refs


def test_conversation_list_recovers_completed_mission_with_active_mirror_run(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    team_mission = importlib.import_module("tui_gateway.methods.team_mission")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="监督执行",
        objective="规划审批后执行",
        mode="supervised_mission",
        status="completed",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    mirror_run_id = "team-mission:mission-1:conversation:run-synthesis"
    db.upsert_run(
        run_id=mirror_run_id,
        session_id="team-session-1",
        runtime_scope_key="team_mission:mission-1",
        turn_id="turn-synthesis",
        runtime_session_id="runtime-synthesis",
        status="running",
    )
    transport = _MemoryTransport()
    subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="team-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )
    try:
        response = server._methods["team_mission.conversation.list"](
            1,
            {"team_id": "team-1"},
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)

    conversation = response["result"]["conversations"][0]
    assert conversation["conversation_id"] == "conversation-1"
    assert conversation["running"] is False
    assert conversation["active_run_id"] == ""
    assert conversation["run_state"] == "completed"
    assert db.get_run(mirror_run_id)["status"] == "completed"
    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    assert [event["type"] for event in streamed] == ["message.complete"]
    assert streamed[0]["run_id"] == mirror_run_id
    assert streamed[0]["runtime_scope_key"] == "team_mission:mission-1"

    resolved = server._methods["team_mission.conversation.resolve"](
        2,
        {"identifier": "conversation-1"},
    )
    resolved_conversation = resolved["result"]["conversation"]
    assert resolved_conversation["running"] is False
    assert resolved_conversation["active_run_id"] == ""
    assert resolved_conversation["run_state"] == "completed"
