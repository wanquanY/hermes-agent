"""Regression tests for issue #30963 — partial-stream stub finish_reason.

Pins the contract:

- partial network streams use a distinct ``stream_error`` finish reason.
- network failures never enter the model output-length continuation loop.
- genuine provider-reported length truncation keeps the existing retry path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import (
    FINISH_REASON_LENGTH,
    FINISH_REASON_STREAM_ERROR,
    PARTIAL_STREAM_STUB_ID,
)
from agent.conversation_loop import _get_continuation_prompt


# ── Helpers (mirrors test_streaming.py) ────────────────────────────────────

def _make_stream_chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(
        content=content, tool_calls=tool_calls,
        reasoning_content=None, reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=None, usage=None)


def _make_tool_call_delta(index=0, tc_id=None, name=None, arguments=None):
    func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=tc_id, function=func)


def _make_agent():
    from run_agent import AIAgent
    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


# ── Stub finish_reason ────────────────────────────────────────────────────

class TestPartialStreamStubFinishReason:
    """The stub returned by interruptible_streaming_api_call when the
    upstream connection dies mid-flight."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_text_only_partial_returns_stream_error(self, _mock_close, mock_create, monkeypatch):

        def _stalling_stream():
            yield _make_stream_chunk(content="Here's my answer so far")
            raise RuntimeError("simulated upstream stall")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stalling_stream()
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._current_streamed_assistant_text = "Here's my answer so far"

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_STREAM_ERROR
        assert response.choices[0].message.content == "Here's my answer so far"
        assert response.choices[0].message.tool_calls is None

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_partial_tool_call_uses_stream_error(self, _mock_close, mock_create, monkeypatch):

        def _stalling_stream():
            yield _make_stream_chunk(content="Let me write the audit: ")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_1", name="write_file"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments='{"path": "/tmp/x", '),
            ])
            raise RuntimeError("simulated upstream stall")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stalling_stream()
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Let me write the audit: "

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_STREAM_ERROR
        assert response.choices[0].message.tool_calls is None, (
            "tool_calls must remain None (no auto-execution of side-effectful "
            "tool calls)."
        )
        # The stub should carry dropped tool names for continuation prompt
        assert getattr(response, "_dropped_tool_names", None) == ["write_file"]
        content = response.choices[0].message.content or ""
        assert "Stream stalled mid tool-call" in content
        assert "write_file" in content


# ── Clean stream-end mid-tool-call (no exception, no finish_reason) ─────────

class TestCleanStreamEndWithoutCompletionFrame:
    """A clean EOF without ``finish_reason`` is still an incomplete stream."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_no_finish_reason_after_text_routes_to_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        def _clean_ending_stream():
            yield _make_stream_chunk(content="Partial answer")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _clean_ending_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_STREAM_ERROR
        assert response.choices[0].message.content == "Partial answer"
        assert response.choices[0].message.tool_calls is None

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_no_finish_reason_partial_tool_args_routes_to_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        def _clean_ending_stream():
            # Reasoning + tool name + the lone opening brace, then the
            # generator simply RETURNS (StopIteration) — no raise, no
            # finish_reason chunk, no [DONE].
            yield _make_stream_chunk(content="\n")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_x", name="execute_code"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments="{"),
            ])
            # falls off the end — clean close, no terminator

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _clean_ending_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID, (
            "A clean stream-end mid tool-call (no finish_reason) must be "
            "tagged as a partial-stream stub, not a 'stream-<uuid>' "
            "truncation — otherwise the loop reports the false 'output "
            "length limit' error."
        )
        assert response.choices[0].finish_reason == FINISH_REASON_STREAM_ERROR
        assert response.choices[0].message.tool_calls is None, (
            "Incomplete tool args must never auto-execute."
        )
        assert getattr(response, "_dropped_tool_names", None) == ["execute_code"]

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_real_length_truncation_still_uses_uuid_id(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """Control: when the provider DOES send finish_reason='length' with
        partial tool args, it is a genuine output cap — keep the existing
        non-stub behaviour (boost max_tokens and retry)."""

        def _capped_stream():
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_y", name="execute_code"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments="{"),
            ])
            # Provider explicitly reports the output cap.
            yield _make_stream_chunk(finish_reason="length")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _capped_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id != PARTIAL_STREAM_STUB_ID, (
            "A provider-reported finish_reason='length' is a real output cap "
            "and must keep the existing truncation path, not the stream-drop "
            "stub path."
        )
        assert response.id.startswith("stream-")
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH


# ── Length-continuation prompt branching ──────────────────────────────────

class TestLengthContinuationPrompt:
    def test_real_truncation_uses_length_prompt(self):
        prompt = _get_continuation_prompt()
        assert "output length limit" in prompt
        assert "network error" not in prompt


# ── Integration: live conversation loop ───────────────────────────────────

@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client."""
    from run_agent import AIAgent
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = False
        a.save_trajectories = False
        return a


class TestConversationLoopPartialStreamTermination:
    """A partial network stream terminates with its recovered text."""

    def test_partial_stream_stub_does_not_start_hidden_continuation(self, loop_agent):

        from tests.run_agent.test_run_agent import _mock_assistant_msg

        partial_stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content="The first half of "),
                finish_reason=FINISH_REASON_STREAM_ERROR,
            )],
            usage=None,
        )
        loop_agent.client.chat.completions.create.return_value = partial_stub

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("ask me something")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result["partial"] is True
        assert result["stream_error"] is True
        assert "first half of" in result["final_response"]
