from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


def _create_team_session(db: CliSessionStore, session_id: str) -> None:
    db.sessions.create(session_id=session_id, source="team_mission")
    db.session_index.upsert(
        session_id=session_id,
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
        started_at=1.0,
        updated_at=1.0,
    )


def test_team_conversation_read_model_keeps_main_messages_and_excludes_node_rows(tmp_path):
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    session_id = "team-session-team-conversation-read-model"
    try:
        _create_team_session(db, session_id)
        db.messages.append(
            session_id,
            role="user",
            content="请执行一个团队任务",
            metadata={
                "message_kind": "user_submission",
                "transcript_activity_kind": "mission_start",
            },
        )
        db.messages.append(
            session_id,
            role="assistant",
            content="leader 工具调用前的规划消息应进入主历史",
            metadata={"transcript_activity_kind": "mission_start"},
        )
        db.messages.append(
            session_id,
            role="tool",
            content='{"ok": true}',
            tool_name="team_mission_node_create",
            metadata={"transcript_activity_kind": "mission_start"},
        )
        db.messages.append(
            session_id,
            role="assistant",
            content="误标为聊天的团队任务启动工具调用",
            tool_calls=[
                {
                    "id": "call-start",
                    "type": "function",
                    "function": {"name": "team_mission_start_task", "arguments": "{}"},
                }
            ],
            metadata={
                "activity_id": "act-team_dispatch-1",
                "activity_kind": "chat",
                "transcript_activity_kind": "leader_chat",
            },
        )
        db.messages.append(
            session_id,
            role="tool",
            content='{"success": true}',
            tool_name="team_mission_start_task",
            tool_call_id="call-start",
            metadata={
                "activity_id": "act-team_dispatch-1",
                "activity_kind": "chat",
                "transcript_activity_kind": "leader_chat",
            },
        )
        db.messages.append(
            session_id,
            role="assistant",
            content="团队任务已进入运行态",
            metadata={
                "activity_id": "act-team_dispatch-1",
                "activity_kind": "chat",
                "tool_handoff": {"tool_name": "team_mission_start_task"},
                "transcript_activity_kind": "leader_chat",
            },
        )
        db.messages.append(
            session_id,
            role="assistant",
            content="正常 leader 聊天应保留",
            metadata={"transcript_activity_kind": "leader_chat"},
        )
        db.messages.append(
            session_id,
            role="assistant",
            content="节点内部执行细节",
            metadata={"transcript_activity_kind": "mission_node"},
        )
        db.messages.append(
            session_id,
            role="assistant",
            content="最终汇总",
            metadata={
                "transcript_activity_kind": "mission_summary",
                "team_mission": {"kind": "mission_summary", "mission_id": "mission-1"},
            },
        )

        messages = db.messages.all_as_conversation(
            session_id,
            include_storage_metadata=True,
        )

        assert [message["content"] for message in messages] == [
            "请执行一个团队任务",
            "leader 工具调用前的规划消息应进入主历史",
            '{"ok": true}',
            "误标为聊天的团队任务启动工具调用",
            '{"success": true}',
            "团队任务已进入运行态",
            "正常 leader 聊天应保留",
            "最终汇总",
        ]
        assert all("message_id" in message for message in messages)
    finally:
        db.close()


def test_team_conversation_pages_after_visible_transcript_filter(tmp_path):
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    session_id = "team-session-team-conversation-page"
    try:
        _create_team_session(db, session_id)
        db.messages.append(
            session_id,
            role="user",
            content="用户发起任务不能被运行期尾部挤掉",
            metadata={
                "message_kind": "user_submission",
                "transcript_activity_kind": "mission_start",
            },
        )
        for index in range(80):
            db.messages.append(
                session_id,
                role="assistant",
                content=f"节点运行日志 {index}",
                metadata={"transcript_activity_kind": "mission_node"},
            )
        db.messages.append(
            session_id,
            role="assistant",
            content="任务完成摘要",
            metadata={
                "transcript_activity_kind": "mission_summary",
                "team_mission": {"kind": "mission_summary", "mission_id": "mission-1"},
            },
        )

        page = db.messages.page_as_conversation(session_id, limit=2)

        assert [message["content"] for message in page["messages"]] == [
            "用户发起任务不能被运行期尾部挤掉",
            "任务完成摘要",
        ]
        assert page["pageInfo"]["totalCount"] == 2
        assert page["pageInfo"]["hasMoreBefore"] is False
    finally:
        db.close()


def test_team_conversation_read_model_dedupes_projected_summary_shadow_rows(tmp_path):
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    session_id = "team-session-team-conversation-summary-dedupe"
    try:
        _create_team_session(db, session_id)
        stable_id = "team-mission-summary:mission-1:completed"
        db.messages.append(
            session_id,
            role="assistant",
            content="最终汇总",
            conversation_message_id=stable_id,
            metadata={
                "conversation_message_id": stable_id,
                "transcript_activity_kind": "mission_summary",
                "team_mission": {"kind": "mission_summary", "mission_id": "mission-1"},
            },
        )
        db.messages.append(
            session_id,
            role="user",
            content="下一轮用户消息",
            metadata={"transcript_activity_kind": "leader_chat"},
        )
        db.messages.append(
            session_id,
            role="assistant",
            content="最终汇总",
            metadata={
                "conversation_message_id": stable_id,
                "transcript_activity_kind": "mission_summary",
                "team_mission": {"kind": "mission_summary", "mission_id": "mission-1"},
            },
        )

        messages = db.messages.all_as_conversation(
            session_id,
            include_storage_metadata=True,
        )

        assert [message["content"] for message in messages] == [
            "最终汇总",
            "下一轮用户消息",
        ]
        assert messages[0]["conversation_message_id"] == stable_id
    finally:
        db.close()


def test_team_transcript_classifier_keeps_legacy_rows_out_of_node_history():
    from hermes_team_mission.domain.transcript_visibility import (
        is_main_transcript_message,
        is_node_transcript_message,
    )

    legacy_message = {"role": "assistant", "content": "旧历史消息", "metadata": {}}
    node_message = {
        "role": "assistant",
        "content": "节点内部消息",
        "metadata": {"transcript_activity_kind": "mission_node"},
    }
    legacy_node_message = {
        "role": "assistant",
        "content": "旧节点内部消息",
        "metadata": {"activity_id": "act-node:mission-1:node-1"},
    }

    assert is_main_transcript_message(legacy_message) is True
    assert is_node_transcript_message(legacy_message) is False
    assert is_main_transcript_message(node_message) is False
    assert is_node_transcript_message(node_message) is True
    assert is_main_transcript_message(legacy_node_message) is False
    assert is_node_transcript_message(legacy_node_message) is True
