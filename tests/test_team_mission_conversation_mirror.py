from pathlib import Path

from tests.team_mission_gateway_test_support import team_mission_gateway


class _MemoryTransport:
    def __init__(self):
        self.frames = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


def test_synthesis_stream_does_not_mirror_and_summary_writer_requests_leader_report_once(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_team_mission.runtime import leader_report_dispatch
    from hermes_team_mission.runtime.team_transcript_writer import MissionSummaryWriter
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    submitted: list[dict] = []

    def fake_submit_leader_report(**kwargs):
        submitted.append(kwargs)
        kwargs["db"].upsert_team_mission_result(
            mission_id=kwargs["mission_id"],
            activity_id=f"mission:{kwargs['mission_id']}",
            status=kwargs["outcome"],
            outcome=kwargs["outcome"],
            summary_text=kwargs["summary_text"],
            node_results=[],
            artifact_refs=kwargs["artifact_refs"],
            leader_report_run_id="leader-report-run-1",
        )
        return {
            "ok": True,
            "status": "queued",
            "mission_id": kwargs["mission_id"],
            "run_id": "leader-report-run-1",
            "conversation_session_id": kwargs["conversation_session_id"],
        }

    monkeypatch.setattr(leader_report_dispatch, "_leader_report_submitter", fake_submit_leader_report)
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

    mirrored_events = db.list_run_events("team-session-1")
    assert mirrored_events == []

    streamed = [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]
    assert streamed == []
    assert db.get_messages("team-session-1") == []

    summary = MissionSummaryWriter.emit_mission_summary(
        db,
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        outcome="completed",
    )
    assert summary["status"] == "queued"
    assert summary["run_id"] == "leader-report-run-1"
    assert len(submitted) == 1
    assert submitted[0]["summary_text"] == "最终汇总"
    assert submitted[0]["artifact_refs"][0]["path"] == "/tmp/final-report.md"
    assert db.get_messages("team-session-1") == []

    MissionSummaryWriter.emit_mission_summary(
        db,
        mission_id="mission-1",
        conversation_session_id="team-session-1",
        outcome="completed",
    )
    messages = db.get_messages("team-session-1")
    assert messages == []
    assert len(submitted) == 1


def test_leader_report_completion_uses_canonical_result_and_records_message_id(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_team_mission.runtime import leader_report_dispatch
    from hermes_team_mission.runtime.team_transcript_writer import MissionSummaryWriter
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-report",
        conversation_id="conversation-1",
        title="生成市场报告",
        objective="生成市场报告",
        mode="autonomous_mission",
        status="completed",
        leader_session_id="team-session-1",
        metadata={"stableTeamSessionId": "team-session-1"},
    )
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        mission_id="mission-report",
        title="生成市场报告",
        objective="生成市场报告",
    )
    result = db.upsert_team_mission_result(
        mission_id="mission-report",
        activity_id="mission:mission-report",
        status="completed",
        outcome="completed",
        summary_text="已完成节点3最终汇总。\n最终结论：PASS\n已按要求提交结构化 handoff。",
        node_results=[
            {"kind": "synthesis", "result": "PASS", "summary": "最终结论：PASS"},
        ],
        artifact_refs=[
            {"path": "/tmp/market-report.md", "title": "market-report.md", "kind": "file", "visibility": "report"},
        ],
    )

    def fake_submit_leader_report(**kwargs):
        run_id = "leader-report-run-1"
        kwargs["db"].bind_team_mission_run(
            mission_id=kwargs["mission_id"],
            node_id="",
            run_id=run_id,
            session_id=kwargs["conversation_session_id"],
            runtime_session_id="",
            runtime_scope_key="team:conversation-1:leader-conversation",
            role="leader",
            metadata={
                "kind": "leader_report",
                "result_id": result["result_id"],
                "outcome": kwargs["outcome"],
                "artifact_refs": kwargs["artifact_refs"],
            },
        )
        kwargs["db"].upsert_team_mission_result(
            result_id=result["result_id"],
            mission_id=kwargs["mission_id"],
            activity_id=result["activity_id"],
            status=result["status"],
            outcome=result["outcome"],
            summary_text=result["summary_text"],
            node_results=result["node_results"],
            artifact_refs=kwargs["artifact_refs"],
            leader_report_run_id=run_id,
        )
        return {
            "ok": True,
            "status": "queued",
            "mission_id": kwargs["mission_id"],
            "run_id": run_id,
            "conversation_session_id": kwargs["conversation_session_id"],
        }

    monkeypatch.setattr(leader_report_dispatch, "_leader_report_submitter", fake_submit_leader_report)
    message = MissionSummaryWriter.emit_mission_summary(
        db,
        mission_id="mission-report",
        conversation_session_id="team-session-1",
        outcome="completed",
    )
    assert message["status"] == "queued"
    assert db.get_messages("team-session-1") == []

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-leader-report",
            "stored_session_id": "team-session-1",
            "run_id": "leader-report-run-1",
            "turn_id": "turn-leader-report",
            "runtime_scope_key": "team:conversation-1:leader-conversation",
            "seq": 1,
            "payload": {
                "text": "市场报告已经完成，结论为 PASS。",
                "status": "complete",
                "message_seq_in_run": 1,
            },
        },
        db=db,
    )
    [projected] = db.get_messages("team-session-1")
    saved_result = db.get_team_mission_result("mission-report")

    assert projected["content"] == "市场报告已经完成，结论为 PASS。"
    assert "已完成本次交付" not in projected["content"]
    assert projected["metadata"]["kind"] == "mission_report"
    assert projected["metadata"]["team_mission"]["kind"] == "mission_report"
    assert projected["metadata"]["artifacts"][0]["path"] == "/tmp/market-report.md"
    assert saved_result["leader_report_run_id"] == "leader-report-run-1"
    assert saved_result["leader_report_message_id"] == projected["conversation_message_id"]
    events = db.list_team_mission_run_events("mission-report")
    report_ready = next(
        event for event in events
        if event["type"] == "team_mission.runtime.event"
        and event["payload"]["source_event_type"] == "mission.report.ready"
    )
    status_events = [
        event for event in events
        if event["type"] == "team_mission.conversation.status"
        and event["payload"]["source_event_type"] == "mission.report.ready"
    ]
    assert status_events
    assert status_events[-1]["seq"] > report_ready["seq"]
    assert status_events[-1]["payload"]["projection"]["leaderReportStatus"] == "ready"
    assert status_events[-1]["payload"]["projection"]["leaderReportMessageId"] == projected["conversation_message_id"]


def test_synthesis_append_deltas_are_not_mirrored_to_conversation_stream(tmp_path: Path):
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
            (3, "message.delta", {"mode": "append", "delta": "队", "text": "队", "offset": 1}),
            (4, "message.delta", {"mode": "append", "delta": "协作", "text": "协作", "offset": 2}),
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
    assert streamed == []

    mirrored_events = db.list_run_events("team-session-1")
    assert mirrored_events == []
    assert db.get_messages("team-session-1") == []


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
    mission_transport = _MemoryTransport()
    node_subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="synthesis-session-1",
        transport=transport,
        active_only=False,
        db=db,
    )
    mission_subscription_id, _ = run_control.subscribe_activity(
        activity_id="mission:mission-1",
        transport=mission_transport,
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
                    "activity_id": "mission:mission-1",
                    "seq": seq,
                    "payload": {
                        "activity_id": "mission:mission-1",
                        "mode": "append",
                        "delta": chunk,
                        "text": chunk,
                        "offset": offset,
                    },
                },
                db=db,
            )

        immediate_raw_deltas = [
            frame.get("params") or {}
            for frame in mission_transport.frames
            if (
                frame.get("method") == "event"
                and (frame.get("params") or {}).get("type") == "message.delta"
                and isinstance((frame.get("params") or {}).get("payload"), dict)
            )
        ]
        assert immediate_raw_deltas == []

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
        assert [event["payload"]["text_stream"]["delta"] for event in mission_deltas] == ["最终", "交付", "完成"]
        assert all(event["payload"]["subject"]["type"] == "node" for event in mission_deltas)

        _, replay_events = run_control.subscribe_activity(
            activity_id="mission:mission-1",
            transport=None,
            db=db,
        )
        replay_deltas = [
            event for event in replay_events
            if event["type"] == "team_mission.runtime.event"
            and event["payload"]["source_event_type"] == "message.delta"
        ]
        assert [event["payload"]["text_stream"]["delta"] for event in replay_deltas] == ["最终", "交付", "完成"]

    finally:
        run_control.unsubscribe_session(subscription_id=node_subscription_id)
        run_control.unsubscribe_activity(subscription_id=mission_subscription_id)


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

    finally:
        run_control.unsubscribe_session(subscription_id=node_subscription_id)


def test_conversation_resolve_recovers_legacy_final_deliverable_state(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
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
    assert response["result"]["messages"] == []
    assert db.get_messages("team-session-1") == []
    deliverable = db.latest_team_mission_deliverable_for_run("run-synthesis")
    assert deliverable["source"] == "legacy_imported"
    assert deliverable["summary"] == "旧任务最终汇总"

    server._methods["team_mission.conversation.resolve"](
        2,
        {"identifier": "conversation-1"},
    )
    assert db.get_messages("team-session-1") == []


def test_final_deliverable_complete_noops_and_preserves_existing_transcript(
    tmp_path: Path,
):
    from hermes_state import SessionDB
    from hermes_team_mission.runtime.conversation_mirror import mirror_event_to_conversation

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

    assert saved == {}
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["id"] == bad_message_id
    assert messages[0]["content"] == polluted_mirror_text


def test_final_deliverable_complete_noops_after_repeated_markdown_chunks(tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_team_mission.runtime.conversation_mirror import mirror_event_to_conversation

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
    chunks = [
        "## 整合报告\n\n",
        "| 项目 | 内容 |\n",
        "|---|---|\n",
        "| **任务名称** | team_stream_refactor_check.txt |\n",
        "|---|---|\n",
        "✅ 验证通过\n",
    ]
    for seq, chunk in enumerate(chunks, start=1):
        db.append_run_event(
            "synthesis-session-1",
            {
                "type": "message.delta",
                "stored_session_id": "synthesis-session-1",
                "run_id": "run-synthesis",
                "turn_id": "turn-synthesis",
                "runtime_scope_key": "team:mission-1:synthesis",
                "seq": seq,
                "payload": {"mode": "append", "text": chunk, "delta": chunk},
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

    assert saved == {}
    assert db.get_messages("team-session-1") == []


def test_conversation_resolve_hides_legacy_final_message_without_rewriting_stream_history(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
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

    assert response["result"]["messages"] == []
    messages = db.get_messages("team-session-1")
    assert len(messages) == 1
    assert messages[0]["id"] == bad_message_id
    assert messages[0]["content"] == polluted_text
    events = db.list_run_events("team-session-1", run_id=mirror_run_id)
    delta_event = next(event for event in events if event["type"] == "message.delta")
    complete_event = next(event for event in events if event["type"] == "message.complete")
    assert delta_event["payload"]["mode"] == "append"
    assert delta_event["payload"]["text"] == polluted_text
    assert delta_event["payload"]["delta"] == polluted_text
    assert complete_event["payload"]["text"] == polluted_text


def test_conversation_projection_exposes_final_deliverable_artifacts_to_list_and_resolve(
    monkeypatch,
    tmp_path: Path,
):
    import importlib

    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
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

    team_mission = team_mission_gateway()
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


def test_leader_chat_complete_with_team_chat_activity_projects_to_transcript(tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_state_participants import leader_participant_id
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    conversation_id = "team-conversation-progress"
    session_id = f"team-session-{conversation_id}"
    activity_id = f"chat:{session_id}"
    participant_id = leader_participant_id(conversation_id)
    run_id = "team-leader-run-progress"
    turn_id = "team-leader-turn-progress"

    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=session_id,
        team_id="team-1",
        title="团队会话",
        active_mission_id="mission-progress",
    )

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-leader-progress",
            "stored_session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": "profile:leader",
            "activity_id": activity_id,
            "activityId": activity_id,
            "participant_id": participant_id,
            "participantId": participant_id,
            "seq": 7,
            "payload": {
                "text": "当前任务进度：Worker 正在执行，验证节点等待中。",
                "status": "complete",
                "message_seq_in_run": 1,
                "messageSeqInRun": 1,
                "activity_id": activity_id,
                "activityId": activity_id,
                "participant_id": participant_id,
                "participantId": participant_id,
            },
        },
        db=db,
    )

    [message] = db.get_messages(session_id)
    assert message["role"] == "assistant"
    assert message["content"] == "当前任务进度：Worker 正在执行，验证节点等待中。"
    assert message["participant_id"] == participant_id
    assert message["metadata"]["activity_kind"] == "leader_chat"
    assert message["metadata"]["transcript_activity_kind"] == "leader_chat"
    assert message["metadata"]["team_mission"]["kind"] == "leader_chat"

    row = db._conn.execute(  # noqa: SLF001 - regression verifies projection wiring.
        """
        SELECT projected_message_id, projection_state
        FROM run_events
        WHERE run_id = ? AND event_type = 'message.complete'
        """,
        (run_id,),
    ).fetchone()
    assert row["projected_message_id"] == message["conversation_message_id"]
    assert row["projection_state"] == "projected"


def test_leader_chat_projection_persists_run_artifacts_on_assistant_message(tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_state_participants import leader_participant_id
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    conversation_id = "team-conversation-artifacts"
    session_id = f"team-session-{conversation_id}"
    activity_id = f"chat:{session_id}"
    participant_id = leader_participant_id(conversation_id)
    run_id = "team-leader-run-artifacts"
    turn_id = "team-leader-turn-artifacts"

    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=session_id,
        team_id="team-1",
        title="团队会话",
    )

    run_control.record_event(
        {
            "type": "artifact.created",
            "session_id": "runtime-leader-artifacts",
            "stored_session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": "profile:leader",
            "activity_id": activity_id,
            "participant_id": participant_id,
            "seq": 6,
            "payload": {
                "id": "artifact-report",
                "path": "/tmp/workspace/report.md",
                "title": "report.md",
                "mime_type": "text/markdown",
                "size_bytes": 128,
            },
        },
        db=db,
    )
    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-leader-artifacts",
            "stored_session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": "profile:leader",
            "activity_id": activity_id,
            "participant_id": participant_id,
            "seq": 7,
            "payload": {
                "text": "Created report.md",
                "status": "complete",
                "message_seq_in_run": 1,
                "activity_id": activity_id,
                "participant_id": participant_id,
            },
        },
        db=db,
    )

    [message] = db.get_messages(session_id)
    assert message["content"] == "Created report.md"
    assert message["metadata"]["artifacts"][0]["path"] == "/tmp/workspace/report.md"
    assert message["metadata"]["artifacts"][0]["mimeType"] == "text/markdown"
    assert message["metadata"]["team_mission"]["artifactRefs"][0]["path"] == "/tmp/workspace/report.md"


def test_late_artifact_event_merges_into_projected_leader_chat_message(tmp_path: Path):
    from hermes_state import SessionDB
    from hermes_state_participants import leader_participant_id
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    conversation_id = "team-conversation-late-artifact"
    session_id = f"team-session-{conversation_id}"
    activity_id = f"chat:{session_id}"
    participant_id = leader_participant_id(conversation_id)
    run_id = "team-leader-run-late-artifact"
    turn_id = "team-leader-turn-late-artifact"

    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=session_id,
        team_id="team-1",
        title="团队会话",
    )

    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-leader-late-artifact",
            "stored_session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": "profile:leader",
            "activity_id": activity_id,
            "participant_id": participant_id,
            "seq": 7,
            "payload": {
                "text": "Created report.md",
                "status": "complete",
                "message_seq_in_run": 1,
                "activity_id": activity_id,
                "participant_id": participant_id,
            },
        },
        db=db,
    )
    [message_before] = db.get_messages(session_id)
    assert "artifacts" not in message_before["metadata"]

    run_control.record_event(
        {
            "type": "artifact.created",
            "session_id": "runtime-leader-late-artifact",
            "stored_session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": "profile:leader",
            "activity_id": activity_id,
            "participant_id": participant_id,
            "seq": 8,
            "payload": {
                "id": "artifact-report",
                "path": "/tmp/workspace/report.md",
                "title": "report.md",
                "mime_type": "text/markdown",
                "size_bytes": 128,
            },
        },
        db=db,
    )

    [message_after] = db.get_messages(session_id)
    assert message_after["metadata"]["artifacts"][0]["path"] == "/tmp/workspace/report.md"
    assert message_after["metadata"]["team_mission"]["artifactRefs"][0]["path"] == "/tmp/workspace/report.md"


def test_team_conversation_read_model_backfills_unprojected_leader_chat(tmp_path: Path):
    import json

    from hermes_state import SessionDB
    from hermes_state_participants import leader_participant_id

    db = SessionDB(tmp_path / "state.db")
    conversation_id = "team-conversation-backfill"
    session_id = f"team-session-{conversation_id}"
    activity_id = f"chat:{session_id}"
    participant_id = leader_participant_id(conversation_id)
    run_id = "team-leader-run-backfill"
    event = {
        "type": "message.complete",
        "session_id": "runtime-leader-backfill",
        "stored_session_id": session_id,
        "run_id": run_id,
        "turn_id": "team-leader-turn-backfill",
        "runtime_scope_key": "profile:leader",
        "activity_id": activity_id,
        "activityId": activity_id,
        "participant_id": participant_id,
        "participantId": participant_id,
        "seq": 3,
        "timestamp": 123.0,
        "payload": {
            "text": "历史 Leader 回复应该被恢复。",
            "status": "complete",
            "message_seq_in_run": 1,
            "messageSeqInRun": 1,
            "activity_id": activity_id,
            "activityId": activity_id,
            "participant_id": participant_id,
            "participantId": participant_id,
        },
    }

    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=session_id,
        team_id="team-1",
        title="团队会话",
        active_mission_id="mission-backfill",
    )

    def insert_raw_event(conn):
        conn.execute(
            """
            INSERT INTO run_events (
                session_id, run_id, turn_id, runtime_session_id, runtime_scope_key,
                activity_id, event_type, seq, timestamp, payload_json, event_json,
                status, participant_id, projection_state, runtime_source_seq
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                event["turn_id"],
                event["session_id"],
                event["runtime_scope_key"],
                activity_id,
                "message.complete",
                3,
                123.0,
                json.dumps(event["payload"], ensure_ascii=False),
                json.dumps(event, ensure_ascii=False),
                "completed",
                participant_id,
                "raw",
                3,
            ),
        )

    db._execute_write(insert_raw_event)  # noqa: SLF001 - regression seeds a legacy raw row.
    assert db.get_messages(session_id) == []

    messages = db.get_conversation_message_read_model(
        session_id,
        include_storage_metadata=True,
    )

    assert [message["content"] for message in messages] == ["历史 Leader 回复应该被恢复。"]
    assert messages[0]["metadata"]["transcript_activity_kind"] == "leader_chat"
    row = db._conn.execute(  # noqa: SLF001 - regression verifies repaired projection metadata.
        """
        SELECT projected_message_id, projection_state
        FROM run_events
        WHERE run_id = ? AND event_type = 'message.complete'
        """,
        (run_id,),
    ).fetchone()
    assert row["projected_message_id"] == messages[0]["conversation_message_id"]
    assert row["projection_state"] == "projected"


def test_team_conversation_read_model_backfills_projected_leader_artifacts(tmp_path: Path):
    import json

    from hermes_state import SessionDB
    from hermes_state_participants import leader_participant_id
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    conversation_id = "team-conversation-projected-artifact-backfill"
    session_id = f"team-session-{conversation_id}"
    activity_id = f"chat:{session_id}"
    participant_id = leader_participant_id(conversation_id)
    run_id = "team-leader-run-projected-artifact-backfill"
    turn_id = "team-leader-turn-projected-artifact-backfill"

    db.create_session(session_id, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=session_id,
        team_id="team-1",
        title="团队会话",
    )

    run_control.record_event(
        {
            "type": "artifact.created",
            "session_id": "runtime-leader-projected-artifact-backfill",
            "stored_session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": "profile:leader",
            "activity_id": activity_id,
            "participant_id": participant_id,
            "seq": 2,
            "payload": {
                "id": "artifact-json",
                "path": "/tmp/workspace/test_file_3.json",
                "title": "test_file_3.json",
                "mime_type": "application/json",
                "size_bytes": 130,
            },
        },
        db=db,
    )
    run_control.record_event(
        {
            "type": "message.complete",
            "session_id": "runtime-leader-projected-artifact-backfill",
            "stored_session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": "profile:leader",
            "activity_id": activity_id,
            "participant_id": participant_id,
            "seq": 3,
            "payload": {
                "text": "已创建 1 个文件。",
                "status": "complete",
                "message_seq_in_run": 1,
                "activity_id": activity_id,
                "participant_id": participant_id,
            },
        },
        db=db,
    )

    [projected_message] = db.get_messages(session_id)
    assert projected_message["metadata"]["artifacts"][0]["path"] == "/tmp/workspace/test_file_3.json"

    def strip_persisted_artifacts(conn):
        row = conn.execute(
            """
            SELECT id, metadata_json
            FROM messages
            WHERE session_id = ?
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata.pop("artifacts", None)
        team_metadata = dict(metadata["team_mission"])
        team_metadata.pop("artifact_refs", None)
        team_metadata.pop("artifactRefs", None)
        metadata["team_mission"] = team_metadata
        conn.execute(
            "UPDATE messages SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), row["id"]),
        )

    db._execute_write(strip_persisted_artifacts)  # noqa: SLF001 - regression seeds a legacy projected row.
    [legacy_message] = db.get_messages(session_id)
    assert "artifacts" not in legacy_message["metadata"]
    assert "artifactRefs" not in legacy_message["metadata"]["team_mission"]

    messages = db.get_conversation_message_read_model(
        session_id,
        include_storage_metadata=True,
    )

    assert messages[0]["metadata"]["artifacts"][0]["path"] == "/tmp/workspace/test_file_3.json"
    assert messages[0]["metadata"]["artifacts"][0]["mimeType"] == "application/json"
    assert (
        messages[0]["metadata"]["team_mission"]["artifactRefs"][0]["path"]
        == "/tmp/workspace/test_file_3.json"
    )
