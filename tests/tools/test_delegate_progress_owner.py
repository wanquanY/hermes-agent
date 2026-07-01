#!/usr/bin/env python3
"""Focused tests for delegate child progress ownership metadata."""

import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent


def _make_mock_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


class TestDelegateProgressOwner(unittest.TestCase):
    @patch("tools.delegate_tool._load_config", return_value={})
    def test_build_child_agent_marks_progress_owner_tool(self, mock_cfg):
        parent = _make_mock_parent()
        parent.tool_progress_callback = MagicMock()
        parent._delegate_child_output_tool_name = "test_agent_profile"

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="test draft",
                context=None,
                toolsets=[],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        progress_cb = MockAgent.call_args[1]["tool_progress_callback"]
        self.assertIsNotNone(progress_cb)

        progress_cb("tool.started", "terminal", "pwd", {"command": "pwd"})

        args, kwargs = parent.tool_progress_callback.call_args
        self.assertEqual(args[:4], ("subagent.tool", "terminal", "pwd", {"command": "pwd"}))
        self.assertEqual(kwargs["delegation_tool_name"], "test_agent_profile")


if __name__ == "__main__":
    unittest.main()
