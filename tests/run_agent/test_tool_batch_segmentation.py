"""Ordered mixed tool-batch segmentation regression tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.tool_dispatch_helpers import (
    _plan_tool_batch_segments,
    _should_parallelize_tool_batch,
)
from agent.tool_executor import execute_tool_calls_segmented


def _call(tool_name: str, **arguments):
    return SimpleNamespace(
        id=f"call-{tool_name}-{id(arguments)}",
        function=SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(arguments),
        ),
    )


def _segment_names(plan):
    return [
        (kind, [call.function.name for call in calls])
        for kind, calls in plan
    ]


def test_mixed_batch_preserves_barrier_order_and_recovers_safe_runs():
    calls = [
        _call("read_file", path="a.py"),
        _call("web_search", query="one"),
        _call("terminal", command="make deploy"),
        _call("search_files", query="owner"),
        _call("skill_view", name="test"),
    ]

    assert _segment_names(_plan_tool_batch_segments(calls)) == [
        ("parallel", ["read_file", "web_search"]),
        ("sequential", ["terminal"]),
        ("parallel", ["search_files", "skill_view"]),
    ]


def test_unknown_and_mcp_effects_fail_closed_to_sequential(monkeypatch):
    monkeypatch.setattr(
        "agent.tool_dispatch_helpers._is_mcp_tool_parallel_safe",
        lambda _name: True,
    )
    calls = [
        _call("read_file", path="a.py"),
        _call("mcp__payments__charge", amount=10),
        _call("future_plugin", value=1),
        _call("read_file", path="b.py"),
    ]

    assert _segment_names(_plan_tool_batch_segments(calls)) == [
        ("sequential", [
            "read_file",
            "mcp__payments__charge",
            "future_plugin",
            "read_file",
        ]),
    ]


def test_independent_file_mutations_can_share_a_parallel_segment(tmp_path):
    calls = [
        _call("write_file", path=str(tmp_path / "a.py")),
        _call("patch", path=str(tmp_path / "b.py")),
    ]

    assert _segment_names(_plan_tool_batch_segments(calls)) == [
        ("parallel", ["write_file", "patch"]),
    ]


def test_overlapping_file_effects_are_serialized(tmp_path):
    root = tmp_path / "tree"
    calls = [
        _call("write_file", path=str(root)),
        _call("patch", path=str(root / "child.py")),
    ]

    assert _segment_names(_plan_tool_batch_segments(calls)) == [
        ("sequential", ["write_file", "patch"]),
    ]


def test_malformed_arguments_become_a_barrier():
    malformed = SimpleNamespace(
        id="bad",
        function=SimpleNamespace(name="read_file", arguments="{"),
    )
    calls = [_call("read_file", path="a"), malformed, _call("read_file", path="b")]

    assert _segment_names(_plan_tool_batch_segments(calls)) == [
        ("sequential", ["read_file", "read_file", "read_file"]),
    ]


def test_homogeneous_parallel_view_remains_compatible():
    safe = [_call("read_file", path="a"), _call("web_search", query="q")]
    unsafe = [_call("read_file", path="a"), _call("terminal", command="date")]

    assert _should_parallelize_tool_batch(safe) is True
    assert _should_parallelize_tool_batch(unsafe) is False


def test_segmented_executor_finalizes_once_for_whole_turn():
    calls = [
        _call("read_file", path="a"),
        _call("web_search", query="q"),
        _call("terminal", command="date"),
        _call("search_files", query="x"),
        _call("skill_view", name="s"),
    ]
    assistant = SimpleNamespace(tool_calls=calls)
    messages = []
    agent = MagicMock()
    agent.context_compressor.context_length = 100_000
    order = []

    def concurrent(_agent, segment, output, *_args, finalize=True, **_kwargs):
        assert finalize is False
        names = [call.function.name for call in segment.tool_calls]
        order.append(("parallel", names))
        output.extend({"role": "tool", "content": name} for name in names)

    def sequential(_agent, segment, output, *_args, finalize=True, **_kwargs):
        assert finalize is False
        names = [call.function.name for call in segment.tool_calls]
        order.append(("sequential", names))
        output.extend({"role": "tool", "content": name} for name in names)

    with (
        patch("agent.tool_executor.execute_tool_calls_concurrent", side_effect=concurrent),
        patch("agent.tool_executor.execute_tool_calls_sequential", side_effect=sequential),
        patch("agent.tool_executor.enforce_turn_budget") as budget,
        patch("agent.tool_executor.get_active_env", return_value=None),
    ):
        execute_tool_calls_segmented(agent, assistant, messages, "task")

    assert order == [
        ("parallel", ["read_file", "web_search"]),
        ("sequential", ["terminal"]),
        ("parallel", ["search_files", "skill_view"]),
    ]
    assert [message["content"] for message in messages] == [
        "read_file",
        "web_search",
        "terminal",
        "search_files",
        "skill_view",
    ]
    budget.assert_called_once()
    agent._apply_pending_steer_to_tool_results.assert_called_once_with(messages, 5)
