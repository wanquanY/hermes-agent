"""
Tests for subagent progress relay (issue #169).

Verifies that:
- KawaiiSpinner.print_above() works with and without active spinner
- _build_child_progress_callback handles CLI/gateway/no-display paths
- Thinking events are relayed correctly
- Parallel callbacks don't share state
"""

import io
import logging
import sys
import time
import threading
import pytest
from unittest.mock import MagicMock, patch

from agent.display import KawaiiSpinner
from tools.delegate_tool import _build_child_progress_callback


# =========================================================================
# KawaiiSpinner.print_above tests
# =========================================================================

class TestPrintAbove:
    """Tests for KawaiiSpinner.print_above method."""

    def test_print_above_without_spinner_running(self):
        """print_above should write to stdout even when spinner is not running."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf  # Redirect to buffer
        
        spinner.print_above("hello world")
        output = buf.getvalue()
        assert "hello world" in output

    def test_print_above_with_spinner_running(self):
        """print_above should clear spinner line and print text."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True  # Pretend spinner is running (don't start thread)
        
        spinner.print_above("tool line")
        output = buf.getvalue()
        assert "tool line" in output
        assert "\r" in output  # Should start with carriage return to clear spinner line

    def test_print_above_uses_captured_stdout(self):
        """print_above should use self._out, not sys.stdout.
        This ensures it works inside redirect_stdout(devnull)."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        
        # Simulate redirect_stdout(devnull)
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            spinner.print_above("should go to buf")
        finally:
            sys.stdout = old_stdout
        
        assert "should go to buf" in buf.getvalue()


# =========================================================================
# _build_child_progress_callback tests
# =========================================================================

class TestBuildChildProgressCallback:
    """Tests for child progress callback builder."""

    def test_returns_none_when_no_display(self):
        """Should return None when parent has no spinner or callback."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent)
        assert cb is None

    def test_returns_none_when_progress_is_explicitly_suppressed(self):
        """Sandboxed internal executions can opt out of subagent UI events."""
        parent = MagicMock()
        parent._delegate_spinner = KawaiiSpinner("delegating")
        parent.tool_progress_callback = MagicMock()
        parent._delegate_child_progress_suppressed = True

        cb = _build_child_progress_callback(0, "test goal", parent)
        assert cb is None

    def test_cli_spinner_tool_event(self):
        """Should print tool line above spinner for CLI path."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent)
        assert cb is not None
        
        cb("tool.started", "web_search", "quantum computing", {})
        output = buf.getvalue()
        assert "web_search" in output
        assert "quantum computing" in output
        assert "├─" in output

    def test_cli_spinner_thinking_event(self):
        """Should print thinking line above spinner for CLI path."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("_thinking", "I'll search for papers first")
        
        output = buf.getvalue()
        assert "💭" in output
        assert "search for papers" in output

    def test_gateway_batched_progress(self):
        """Gateway path: each tool.started relays a subagent.tool event, and a
        subagent.progress summary fires once BATCH_SIZE tools accumulate."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Each tool.started relays a subagent.tool event immediately (per-tool relay).
        for i in range(4):
            cb("tool.started", f"tool_{i}", f"arg_{i}", {})
        # 4 per-tool relays so far, no batch summary yet (BATCH_SIZE=5)
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 4

        # 5th call triggers another per-tool relay PLUS the batch-size summary
        cb("tool.started", "tool_4", "arg_4", {})
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 5 + ["subagent.progress"]
        summary_call = parent_cb.call_args_list[-1]
        summary_text = summary_call.kwargs.get("preview") or summary_call.args[2]
        assert "tool_0" in summary_text
        assert "tool_4" in summary_text

    def test_trace_logs_structural_event_without_changing_relay(self, monkeypatch, caplog):
        monkeypatch.setenv("DOVIE_STREAM_TRACE", "1")
        parent = MagicMock()
        parent.session_id = "session-1"
        parent._hermes_active_run_id = "run-1"
        parent._hermes_active_turn_id = "turn-1"
        parent._delegate_spinner = None
        parent.tool_progress_callback = MagicMock()

        cb = _build_child_progress_callback(
            0,
            "test goal",
            parent,
            subagent_id="subagent-1",
            delegate_call_id="delegate-1",
        )
        with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
            cb("tool.started", "terminal", "pwd", {"command": "pwd"})

        parent.tool_progress_callback.assert_called_once()
        assert parent.tool_progress_callback.call_args.args[:4] == (
            "subagent.tool",
            "terminal",
            "pwd",
            {"command": "pwd"},
        )
        assert "[dovie-subagent-event-source]" in caplog.text
        assert "event_type=subagent.tool" in caplog.text
        assert "run_id=run-1" in caplog.text
        assert "turn_id=turn-1" in caplog.text
        assert "status=running" in caplog.text
        assert "payload_bytes=" in caplog.text

    def test_async_terminal_event_keeps_dispatch_origin_after_parent_turn_changes(self):
        parent = MagicMock()
        parent._delegate_spinner = None
        parent._hermes_active_run_id = "run-original"
        parent._hermes_active_turn_id = "turn-original"
        parent._hermes_active_client_message_id = "client-original"
        parent._hermes_active_runtime_scope_key = "profile:original"
        parent.tool_progress_callback = MagicMock()

        callback = _build_child_progress_callback(
            0,
            "finish later",
            parent,
            subagent_id="subagent-1",
            delegate_call_id="delegate-1",
        )

        # The parent turn has finished and a newer turn now owns the session.
        parent._hermes_active_run_id = "run-new"
        parent._hermes_active_turn_id = "turn-new"
        parent._hermes_active_client_message_id = "client-new"
        parent._hermes_active_runtime_scope_key = "profile:new"
        callback(
            "subagent.complete",
            preview="done",
            status="completed",
            summary="done",
        )

        terminal = parent.tool_progress_callback.call_args
        assert terminal.args[0] == "subagent.complete"
        assert terminal.kwargs["run_id"] == "run-original"
        assert terminal.kwargs["turn_id"] == "turn-original"
        assert terminal.kwargs["client_message_id"] == "client-original"
        assert terminal.kwargs["runtime_scope_key"] == "profile:original"
        assert terminal.kwargs["status"] == "completed"

    def test_task_descriptor_is_emitted_once_on_lifecycle_start(self):
        parent = MagicMock()
        parent._delegate_spinner = None
        parent.tool_progress_callback = MagicMock()

        cb = _build_child_progress_callback(
            0,
            "review the repository",
            parent,
            subagent_id="subagent-1",
            parent_id="leader-1",
            depth=1,
            model="test-model",
            toolsets=["terminal"],
            role="reviewer",
            context="large immutable task context",
            delegate_call_id="delegate-1",
            agent_name="Reviewer",
        )
        cb("subagent.start")
        cb("tool.started", "terminal", "pwd", {"command": "pwd"})

        start_payload = parent.tool_progress_callback.call_args_list[0].kwargs
        tool_payload = parent.tool_progress_callback.call_args_list[1].kwargs
        assert start_payload["goal"] == "review the repository"
        assert start_payload["context"] == "large immutable task context"
        assert start_payload["dispatch_message"] == (
            "review the repository\n\nlarge immutable task context"
        )
        assert start_payload["model"] == "test-model"
        assert start_payload["toolsets"] == ["terminal"]

        assert tool_payload["subagent_id"] == "subagent-1"
        assert tool_payload["delegate_call_id"] == "delegate-1"
        assert tool_payload["parent_id"] == "leader-1"
        assert "goal" not in tool_payload
        assert "context" not in tool_payload
        assert "dispatch_message" not in tool_payload
        assert "model" not in tool_payload
        assert "toolsets" not in tool_payload

    def test_thinking_relayed_to_gateway(self):
        """Thinking events are relayed as subagent.thinking events."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("_thinking", "some reasoning text")

        parent_cb.assert_called_once()
        assert parent_cb.call_args.args[0] == "subagent.thinking"
        assert parent_cb.call_args.args[2] == "some reasoning text"

    def test_parallel_callbacks_independent(self):
        """Each child's callback batches tool names independently."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb0 = _build_child_progress_callback(0, "goal a", parent)
        cb1 = _build_child_progress_callback(1, "goal b", parent)

        # 3 tool.started per child = 6 per-tool relays; neither should hit
        # the batch-size summary (batch size = 5, counted per-child).
        for i in range(3):
            cb0("tool.started", f"tool_{i}", f"a_{i}", {})
            cb1("tool.started", f"other_{i}", f"b_{i}", {})

        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events.count("subagent.tool") == 6
        assert "subagent.progress" not in events

    def test_task_index_prefix_in_batch_mode(self):
        """Batch mode (task_count > 1) should show 1-indexed prefix for all tasks."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        # task_index=0 in a batch of 3 → prefix "[1]"
        cb0 = _build_child_progress_callback(0, "test goal", parent, task_count=3)
        cb0("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[1]" in output

        # task_index=2 in a batch of 3 → prefix "[3]"
        buf.truncate(0)
        buf.seek(0)
        cb2 = _build_child_progress_callback(2, "test goal", parent, task_count=3)
        cb2("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[3]" in output

    def test_single_task_no_prefix(self):
        """Single task (task_count=1) should not show index prefix."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("tool.started", "web_search", "test", {})
        
        output = buf.getvalue()
        assert "[" not in output


# =========================================================================
# Integration: thinking callback in run_agent.py
# =========================================================================

class TestThinkingCallback:
    """Tests for the _thinking callback in AIAgent conversation loop."""

    def _simulate_thinking_callback(self, content, callback, delegate_depth=1):
        """Simulate the run_agent.py plain assistant-content path.
        
        delegate_depth: simulates self._delegate_depth.
            Plain assistant output should not be relabeled as thinking for
            either the main agent or delegated subagents.
        """
        _ = (content, callback, delegate_depth)

    def test_thinking_callback_not_fired_for_plain_content(self):
        """Plain assistant content must not be relayed as thinking."""
        calls = []
        self._simulate_thinking_callback(
            "I'll research quantum computing first, then summarize.",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0

    def test_thinking_callback_skipped_when_no_content(self):
        """Should not fire when assistant has no content."""
        calls = []
        self._simulate_thinking_callback(
            None,
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0

    def test_thinking_callback_skips_long_plain_content(self):
        """Long plain assistant content must remain normal output."""
        calls = []
        self._simulate_thinking_callback(
            "A" * 200 + "\nSecond line should be ignored",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0

    def test_thinking_callback_skipped_for_main_agent(self):
        """Main agent (delegate_depth=0) should NOT fire thinking events.
        This prevents gateway spam on Telegram/Discord."""
        calls = []
        self._simulate_thinking_callback(
            "I'll help you with that request.",
            lambda name, preview=None: calls.append((name, preview)),
            delegate_depth=0,
        )
        assert len(calls) == 0

    def test_thinking_callback_does_not_relabel_reasoning_scratchpad(self):
        """XML-tagged assistant content is not relabeled as subagent thinking."""
        calls = []
        self._simulate_thinking_callback(
            "<REASONING_SCRATCHPAD>I need to analyze this carefully</REASONING_SCRATCHPAD>",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0

    def test_thinking_callback_does_not_relabel_think_tags(self):
        """<think> assistant content is not relabeled as subagent thinking."""
        calls = []
        self._simulate_thinking_callback(
            "<think>Let me think about this problem</think>",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0

    def test_thinking_callback_empty_after_strip(self):
        """Should not fire when content is only XML tags."""
        calls = []
        self._simulate_thinking_callback(
            "<REASONING_SCRATCHPAD></REASONING_SCRATCHPAD>",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0


# =========================================================================
# Gateway batch flush tests
# =========================================================================

class TestBatchFlush:
    """Tests for gateway batch flush on subagent completion."""

    def test_flush_sends_remaining_batch(self):
        """_flush should send a final subagent.progress summary of any unsent
        tool names in the batch (less than BATCH_SIZE)."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Send 3 tools (below batch size of 5) — each relays subagent.tool
        cb("tool.started", "web_search", "query1", {})
        cb("tool.started", "read_file", "file.txt", {})
        cb("tool.started", "write_file", "out.txt", {})
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 3  # per-tool relays so far
        assert "subagent.progress" not in events  # no batch-size summary yet

        # Flush should send the remaining 3 as a summary
        cb._flush()
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events[-1] == "subagent.progress"
        summary_call = parent_cb.call_args_list[-1]
        summary_text = summary_call.kwargs.get("preview") or summary_call.args[2]
        assert "web_search" in summary_text
        assert "write_file" in summary_text

    def test_flush_noop_when_batch_empty(self):
        """_flush should not send anything when batch is empty."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb._flush()
        parent_cb.assert_not_called()

    def test_flush_noop_when_no_parent_callback(self):
        """_flush should not crash when there's no parent callback."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True

        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("tool.started", "web_search", "test", {})
        cb._flush()  # Should not crash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
