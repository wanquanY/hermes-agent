from __future__ import annotations

from hermes_agent.application.message_service import MessageService
from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
from hermes_agent.composition.session_repository_db import connect_session_repository_db
from hermes_team_mission.domain.transcript_visibility import (
    TeamMissionTranscriptVisibilityPolicy,
)


def _team_message_service(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    sessions = SessionRepoImpl(conn)
    sessions.create(
        SessionSpec(
            session_id="team-session",
            source="team_mission",
            session_kind="team_mission",
            conversation_kind="team",
        )
    )
    service = MessageService(
        conn,
        sessions,
        visibility_policies={"team": TeamMissionTranscriptVisibilityPolicy()},
    )
    return conn, service


def test_visible_transcript_filters_before_pagination(tmp_path):
    conn, messages = _team_message_service(tmp_path)
    try:
        messages.append(
            "team-session",
            "user",
            "用户发起任务",
            metadata={"transcript_activity_kind": "mission_start"},
            timestamp=10.0,
        )
        for index in range(80):
            messages.append(
                "team-session",
                "assistant",
                f"节点内部消息 {index}",
                metadata={"transcript_activity_kind": "mission_node"},
                timestamp=20.0 + index,
            )
        messages.append(
            "team-session",
            "assistant",
            "任务完成摘要",
            metadata={"transcript_activity_kind": "mission_summary"},
            timestamp=200.0,
        )

        page = messages.page_as_conversation("team-session", limit=2)

        assert [message["content"] for message in page["messages"]] == [
            "用户发起任务",
            "任务完成摘要",
        ]
        assert page["pageInfo"] == {
            "prev_cursor_id": None,
            "next_cursor_id": None,
            "hasMoreBefore": False,
            "hasMoreAfter": False,
            "totalCount": 2,
        }
    finally:
        conn.close()


def test_visible_transcript_uses_event_time_and_stable_cursor_positions(tmp_path):
    conn, messages = _team_message_service(tmp_path)
    try:
        first_id = messages.append(
            "team-session",
            "assistant",
            "最终消息",
            metadata={"transcript_activity_kind": "leader_chat"},
            timestamp=30.0,
        )
        middle_id = messages.append(
            "team-session",
            "assistant",
            "回填的前置消息",
            metadata={"transcript_activity_kind": "leader_chat"},
            timestamp=10.0,
        )
        messages.append(
            "team-session",
            "assistant",
            "中间消息",
            metadata={"transcript_activity_kind": "leader_chat"},
            timestamp=20.0,
        )

        tail = messages.page_as_conversation("team-session", limit=2)
        assert [message["content"] for message in tail["messages"]] == [
            "中间消息",
            "最终消息",
        ]
        assert tail["pageInfo"]["prev_cursor_id"] is not None

        before = messages.page_as_conversation(
            "team-session",
            direction="before",
            cursor_id=first_id,
            limit=2,
        )
        assert [message["content"] for message in before["messages"]] == [
            "回填的前置消息",
            "中间消息",
        ]
        assert int(before["messages"][0]["message_id"]) == middle_id
    finally:
        conn.close()


def test_visible_transcript_deduplicates_metadata_shadow_by_stable_identity(tmp_path):
    conn, messages = _team_message_service(tmp_path)
    stable_id = "team-mission-summary:mission-1:completed"
    try:
        messages.append(
            "team-session",
            "assistant",
            "最终汇总",
            conversation_message_id=stable_id,
            metadata={
                "conversation_message_id": stable_id,
                "transcript_activity_kind": "mission_summary",
            },
            timestamp=10.0,
        )
        messages.append(
            "team-session",
            "user",
            "下一轮用户消息",
            metadata={"transcript_activity_kind": "leader_chat"},
            timestamp=20.0,
        )
        messages.append(
            "team-session",
            "assistant",
            "最终汇总",
            metadata={
                "conversation_message_id": stable_id,
                "transcript_activity_kind": "mission_summary",
            },
            timestamp=30.0,
        )

        transcript = messages.all_as_conversation(
            "team-session",
            include_storage_metadata=True,
        )

        assert [message["content"] for message in transcript] == [
            "最终汇总",
            "下一轮用户消息",
        ]
        assert transcript[0]["conversation_message_id"] == stable_id
    finally:
        conn.close()


def test_visibility_policy_preserves_legacy_main_rows_and_excludes_node_rows():
    policy = TeamMissionTranscriptVisibilityPolicy()

    assert policy.includes({
        "role": "assistant",
        "content": "旧历史消息",
        "metadata": {},
    })
    assert not policy.includes({
        "role": "assistant",
        "content": "节点内部消息",
        "metadata": {"transcript_activity_kind": "mission_node"},
    })
    assert not policy.includes({
        "role": "assistant",
        "content": "旧节点内部消息",
        "metadata": {"activity_id": "act-node:mission-1:node-1"},
    })


def test_domain_visibility_policy_matches_runtime_writer_classification():
    from hermes_team_mission.runtime.team_transcript_writer import (
        main_transcript_message_decision as runtime_decision,
    )

    messages = [
        {"metadata": {"transcript_activity_kind": "leader_chat"}},
        {"metadata": {"transcript_activity_kind": "mission_node"}},
        {"metadata": {"activity_id": "act-node:mission-1:node-1"}},
        {"metadata": {"team_mission_conversation_mirror": True}},
        {"metadata": {}},
    ]

    from hermes_team_mission.domain.transcript_visibility import (
        main_transcript_message_decision as domain_decision,
    )

    assert [domain_decision(message) for message in messages] == [
        runtime_decision(message) for message in messages
    ]


def test_message_service_routes_only_matching_conversation_kind_through_policy(
    tmp_path,
):
    conn = connect_session_repository_db(tmp_path / "state.db")
    sessions = SessionRepoImpl(conn)
    sessions.create(
        SessionSpec(
            session_id="direct-session",
            source="cli",
            conversation_kind="direct",
        )
    )
    messages = MessageService(
        conn,
        sessions,
        visibility_policies={"team": TeamMissionTranscriptVisibilityPolicy()},
    )
    try:
        messages.append(
            "direct-session",
            "assistant",
            "普通会话中的原始元数据不能触发团队过滤",
            metadata={"transcript_activity_kind": "mission_node"},
        )

        transcript = messages.all_as_conversation("direct-session")

        assert [message["content"] for message in transcript] == [
            "普通会话中的原始元数据不能触发团队过滤"
        ]
    finally:
        conn.close()
