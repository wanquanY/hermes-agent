"""Regression coverage for team-history projection and current-input binding."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _assistant_response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are the viewing team participant."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tool_delay = 0
    return agent


def test_canonical_current_input_is_sent_once_and_history_events_stay_separate():
    agent = _agent()
    history = [
        {
            "role": "user",
            "content": "滴滴滴滴滴",
            "participant_id": "user",
            "conversation_message_id": "msg-user-1",
        },
        {
            "role": "user",
            "content": (
                "[assistant | 小多 | leader:team]\n"
                "滴滴滴滴滴～小马，有什么需要帮忙的吗？"
            ),
            "participant_id": "leader:team",
            "conversation_message_id": "msg-leader-1",
            "metadata": {
                "speaker_original_role": "assistant",
                "speaker_projected_role": "user",
            },
        },
        {
            "role": "user",
            "content": "你是谁？",
            "participant_id": "user",
            "conversation_message_id": "msg-current",
        },
    ]
    captured_requests: list[dict] = []

    def api_call(api_kwargs: dict) -> SimpleNamespace:
        captured_requests.append(api_kwargs)
        return _assistant_response("我是当前团队成员。")

    agent._interruptible_api_call = api_call
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "你是谁？",
            conversation_history=history,
            current_input_conversation_message_id="msg-current",
            turn_metadata={"turn_id": "turn-current"},
        )

    assert len(captured_requests) == 1
    wire_messages = captured_requests[0]["messages"]
    non_system = [message for message in wire_messages if message["role"] != "system"]
    assert [message["role"] for message in non_system] == ["user", "user", "user"]
    assert [message["content"] for message in non_system] == [
        "滴滴滴滴滴",
        "[assistant | 小多 | leader:team]\n滴滴滴滴滴～小马，有什么需要帮忙的吗？",
        "你是谁？",
    ]
    assert all("name" not in message for message in non_system)
    assert sum(message["content"].count("你是谁？") for message in non_system) == 1
    assert all("conversation_message_id" not in message for message in wire_messages)
    assert all("participant_id" not in message for message in wire_messages)
    assert all("metadata" not in message for message in wire_messages)

    current_rows = [
        message
        for message in result["messages"]
        if message.get("conversation_message_id") == "msg-current"
    ]
    assert len(current_rows) == 1
    assert current_rows[0]["content"] == "你是谁？"
    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == "我是当前团队成员。"


def test_missing_canonical_current_input_fails_instead_of_appending_duplicate():
    agent = _agent()
    agent._interruptible_api_call = MagicMock()

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        pytest.raises(RuntimeError, match="canonical current input is absent"),
    ):
        agent.run_conversation(
            "你是谁？",
            conversation_history=[
                {
                    "role": "user",
                    "content": "older event",
                    "conversation_message_id": "msg-older",
                }
            ],
            current_input_conversation_message_id="msg-current",
        )

    agent._interruptible_api_call.assert_not_called()
