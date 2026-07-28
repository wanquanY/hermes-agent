"""Concurrent tool batches have a caller-visible wall-clock deadline."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent() -> AIAgent:
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
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
    return agent


def _tool_call(call_id: str, query: str):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name="web_search",
            arguments=f'{{"query": "{query}"}}',
        ),
    )


def test_timeout_keeps_finished_result_and_returns_without_joining_worker(monkeypatch):
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "0.1")
    agent = _make_agent()
    blocker = threading.Event()
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[
            _tool_call("call-fast", "fast"),
            _tool_call("call-slow", "slow"),
        ],
    )
    messages = []

    def invoke(_name, args, *_positional, **_kwargs):
        if args["query"] == "slow":
            blocker.wait(5)
            return "late-result"
        return "fast-result"

    started = time.monotonic()
    try:
        with (
            patch.object(agent, "_invoke_tool", side_effect=invoke),
            patch(
                "agent.tool_executor.maybe_persist_tool_result",
                side_effect=lambda **kwargs: kwargs["content"],
            ),
        ):
            agent._execute_tool_calls_concurrent(
                assistant_message,
                messages,
                "task-1",
            )
    finally:
        blocker.set()

    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert [message["tool_call_id"] for message in messages] == [
        "call-fast",
        "call-slow",
    ]
    assert messages[0]["content"] == "fast-result"
    assert "timed out after 0.1s" in messages[1]["content"]


def test_non_positive_timeout_disables_batch_deadline(monkeypatch):
    from agent.tool_executor import _resolve_concurrent_tool_timeout

    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "0")
    assert _resolve_concurrent_tool_timeout() is None
