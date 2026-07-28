import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from agent.direct_tool_response import build_direct_tool_response
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_tool_call(name: str, arguments: str = "{}", call_id: str | None = None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content: str = "", finish_reason: str = "stop", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.fixture()
def dovie_agent(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setattr(run_agent, "_hermes_home", hermes_home)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("dovie_automation_task_create"),
        ),
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
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def test_build_direct_tool_response_formats_automation_create_success():
    tool_call = _mock_tool_call(
        "dovie_automation_task_create",
        call_id="call_create",
    )
    messages = [
        {
            "role": "tool",
            "name": "dovie_automation_task_create",
            "tool_call_id": "call_create",
            "content": json.dumps(
                {
                    "dovie_event": "automation_job_created",
                    "job": {
                        "id": "job_123",
                        "name": "每日 AI 资讯整理",
                        "scheduleDisplay": "每天 10:00",
                        "enabled": True,
                    },
                },
                ensure_ascii=False,
            ),
        }
    ]

    response = build_direct_tool_response([tool_call], messages)

    assert response == (
        "已创建定时任务「每日 AI 资讯整理」。\n"
        "执行时间：每天 10:00\n"
        "状态：已启用\n"
        "任务 ID：job_123"
    )


def test_build_direct_tool_response_formats_automation_remove_success():
    tool_call = _mock_tool_call(
        "dovie_automation_task_remove",
        call_id="call_remove",
    )
    messages = [
        {
            "role": "tool",
            "name": "dovie_automation_task_remove",
            "tool_call_id": "call_remove",
            "content": json.dumps(
                {
                    "dovie_event": "automation_job_removed",
                    "job": {
                        "id": "job_123",
                        "name": "每日 AI 资讯整理",
                        "scheduleDisplay": "每天 10:00",
                        "enabled": True,
                    },
                },
                ensure_ascii=False,
            ),
        }
    ]

    response = build_direct_tool_response([tool_call], messages)

    assert response == "已删除定时任务「每日 AI 资讯整理」。\n任务 ID：job_123"


def test_run_conversation_returns_directly_after_automation_create(dovie_agent):
    tool_call = _mock_tool_call(
        "dovie_automation_task_create",
        arguments=json.dumps(
            {
                "name": "每日 AI 资讯整理",
                "prompt": "整理最新 AI 资讯",
                "schedule": {"kind": "cron", "expr": "0 10 * * *"},
            },
            ensure_ascii=False,
        ),
        call_id="call_create",
    )
    dovie_agent.client.chat.completions.create.return_value = _mock_response(
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )
    tool_result = json.dumps(
        {
            "dovie_event": "automation_job_created",
            "job": {
                "id": "job_123",
                "name": "每日 AI 资讯整理",
                "scheduleDisplay": "每天 10:00",
                "enabled": True,
            },
        },
        ensure_ascii=False,
    )

    with (
        patch("run_agent.handle_function_call", return_value=tool_result),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch.object(dovie_agent, "_persist_session"),
        patch.object(dovie_agent, "_save_trajectory"),
        patch.object(dovie_agent, "_cleanup_task_resources"),
    ):
        result = dovie_agent.run_conversation("创建一个每天上午十点整理 AI 资讯的定时任务")

    assert dovie_agent.client.chat.completions.create.call_count == 1
    assert result["api_calls"] == 1
    assert result["completed"] is True
    assert result["turn_exit_reason"] == "direct_tool_response(dovie_automation_task_create)"
    assert result["final_response"].startswith("已创建定时任务「每日 AI 资讯整理」。")
    assert result["messages"][-2]["role"] == "tool"
    assert result["messages"][-1] == {
        "role": "assistant",
        "content": result["final_response"],
    }


def test_run_conversation_does_not_direct_return_failed_automation_create(dovie_agent):
    tool_call = _mock_tool_call(
        "dovie_automation_task_create",
        call_id="call_create",
    )
    dovie_agent.client.chat.completions.create.side_effect = [
        _mock_response(finish_reason="tool_calls", tool_calls=[tool_call]),
        _mock_response(content="没有可用的 Dovie 会话上下文。", finish_reason="stop"),
    ]

    with (
        patch(
            "run_agent.handle_function_call",
            return_value=json.dumps({"error": "missing context"}),
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch.object(dovie_agent, "_persist_session"),
        patch.object(dovie_agent, "_save_trajectory"),
        patch.object(dovie_agent, "_cleanup_task_resources"),
    ):
        result = dovie_agent.run_conversation("创建定时任务")

    assert dovie_agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "没有可用的 Dovie 会话上下文。"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
