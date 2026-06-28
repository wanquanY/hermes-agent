from __future__ import annotations

from hermes_team_mission.state.session_views import transform_to_member_perspective


def test_user_messages_pass_through_unchanged() -> None:
    message = {"role": "user", "content": "please review"}

    result = transform_to_member_perspective(
        [message],
        viewing_participant_id="member:alice",
        participants=[],
    )

    assert result == [message]
    assert result[0] is message


def test_self_assistant_message_keeps_assistant_role() -> None:
    message = {
        "role": "assistant",
        "content": "Alice reply",
        "metadata": {"participant_id": "member:alice"},
    }

    result = transform_to_member_perspective(
        [message],
        viewing_participant_id="member:alice",
        participants=[{"participant_id": "member:alice", "display_name": "Alice"}],
    )

    assert result == [message]
    assert result[0]["role"] == "assistant"


def test_other_assistant_message_becomes_user_with_prefix() -> None:
    message = {
        "role": "assistant",
        "content": "I will coordinate the plan.",
        "metadata": {"participant_id": "leader:conv-1"},
    }

    result = transform_to_member_perspective(
        [message],
        viewing_participant_id="member:alice",
        participants=[
            {"participant_id": "leader:conv-1", "display_name": "Leader Name"},
            {"participant_id": "member:alice", "display_name": "Alice"},
        ],
    )

    assert result[0]["role"] == "user"
    assert result[0]["content"] == "[Leader Name] I will coordinate the plan."
    assert result[0]["metadata"]["transformed_from_role"] == "assistant"
    assert result[0]["metadata"]["transformed_speaker_pid"] == "leader:conv-1"
    assert result[0]["metadata"]["transformed_speaker_name"] == "Leader Name"


def test_unknown_participant_uses_role_fallback() -> None:
    message = {
        "role": "assistant",
        "content": "Use the stricter review path.",
        "metadata": {"participant_id": "member:bob", "role": "reviewer"},
    }

    result = transform_to_member_perspective(
        [message],
        viewing_participant_id="member:alice",
        participants=[],
    )

    assert result[0]["role"] == "user"
    assert result[0]["content"] == "[reviewer] Use the stricter review path."


def test_assistant_without_participant_id_treated_as_other() -> None:
    message = {"role": "assistant", "content": "Unstamped assistant text"}

    result = transform_to_member_perspective(
        [message],
        viewing_participant_id="member:alice",
        participants=[],
    )

    assert result[0]["role"] == "user"
    assert result[0]["content"] == "[未知发言者] Unstamped assistant text"
    assert result[0]["metadata"]["transformed_speaker_pid"] == ""
    assert result[0]["metadata"]["transformed_speaker_name"] == "未知发言者"


def test_idempotent_transform_twice_same_result() -> None:
    message = {
        "role": "assistant",
        "content": "Leader instruction",
        "metadata": {"participant_id": "leader:conv-1"},
    }
    participants = [{"participant_id": "leader:conv-1", "display_name": "Leader"}]

    once = transform_to_member_perspective(
        [message],
        viewing_participant_id="member:alice",
        participants=participants,
    )
    twice = transform_to_member_perspective(
        once,
        viewing_participant_id="member:alice",
        participants=participants,
    )

    assert twice == once
    assert twice[0]["content"] == "[Leader] Leader instruction"
    assert twice[0]["content"].count("[Leader]") == 1


def test_other_participant_tool_turns_do_not_leak_into_viewer_history() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-bob", "function": {"name": "search", "arguments": "{}"}}],
            "metadata": {"participant_id": "member:bob"},
        },
        {
            "role": "tool",
            "tool_call_id": "call-bob",
            "content": "{\"ok\": true}",
            "metadata": {"participant_id": "member:bob"},
        },
        {
            "role": "assistant",
            "content": "查完了。",
            "metadata": {"participant_id": "member:bob"},
        },
    ]

    result = transform_to_member_perspective(
        messages,
        viewing_participant_id="leader:conv-1",
        participants=[
            {"participant_id": "leader:conv-1", "display_name": "小多"},
            {"participant_id": "member:bob", "display_name": "后端工程师"},
        ],
    )

    assert [(message["role"], message["content"]) for message in result] == [
        ("user", "[后端工程师] 查完了。"),
    ]
    assert "tool_calls" not in result[0]
    assert "tool_call_id" not in result[0]


def test_viewer_own_tool_turns_remain_valid_tool_sequences() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-leader", "function": {"name": "clarify", "arguments": "{}"}}],
            "metadata": {"participant_id": "leader:conv-1"},
        },
        {
            "role": "tool",
            "tool_call_id": "call-leader",
            "content": "{\"ok\": true}",
            "metadata": {"participant_id": "leader:conv-1"},
        },
        {
            "role": "assistant",
            "content": "我已经确认。",
            "metadata": {"participant_id": "leader:conv-1"},
        },
    ]

    result = transform_to_member_perspective(
        messages,
        viewing_participant_id="leader:conv-1",
        participants=[{"participant_id": "leader:conv-1", "display_name": "小多"}],
    )

    assert [message["role"] for message in result] == ["assistant", "tool", "assistant"]
    assert result[0]["tool_calls"][0]["id"] == "call-leader"
    assert result[1]["tool_call_id"] == "call-leader"
