from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import _build_child_agent


def test_delegate_marks_child_agent_as_internal_execution() -> None:
    parent = _make_mock_parent()
    parent._session_db = MagicMock()

    with patch("run_agent.AIAgent") as agent_type:
        agent_type.return_value = MagicMock()
        _build_child_agent(
            task_index=0,
            goal="inspect repository",
            context=None,
            toolsets=[],
            model=None,
            max_iterations=10,
            parent_agent=parent,
            task_count=1,
        )

    assert agent_type.call_args.kwargs["session_kind"] == "execution"
    assert agent_type.call_args.kwargs["conversation_kind"] == "internal"
