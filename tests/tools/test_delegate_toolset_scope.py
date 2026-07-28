"""Tests for delegate_tool toolset scoping.

Verifies that subagents cannot gain tools that the parent does not have.
The LLM controls the `toolsets` parameter — without intersection with the
parent's enabled_toolsets, it can escalate privileges by requesting
arbitrary toolsets.
"""

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from tools.delegate_tool import _strip_blocked_tools
from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS
from tools.delegate_tool_access import resolve_child_tool_access


class TestToolsetIntersection:
    """Subagent toolsets must be a subset of parent's enabled_toolsets."""

    def test_requested_toolsets_intersected_with_parent(self):
        """LLM requests toolsets parent doesn't have — extras are dropped."""
        parent = SimpleNamespace(enabled_toolsets=["terminal", "file"])

        # Simulate the intersection logic from _build_child_agent
        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["terminal", "file", "web", "browser", "rl"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert sorted(scoped) == ["file", "terminal"]
        assert "web" not in scoped
        assert "browser" not in scoped
        assert "rl" not in scoped

    def test_all_requested_toolsets_available_on_parent(self):
        """LLM requests subset of parent tools — all pass through."""
        parent = SimpleNamespace(enabled_toolsets=["terminal", "file", "web", "browser"])

        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["terminal", "web"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert sorted(scoped) == ["terminal", "web"]

    def test_no_toolsets_requested_inherits_parent(self):
        """When toolsets is None/empty, child inherits parent's set."""
        parent_toolsets = ["terminal", "file", "web"]
        child = _strip_blocked_tools(parent_toolsets)
        assert "terminal" in child
        assert "file" in child
        assert "web" in child

    def test_strip_blocked_removes_delegation(self):
        """Blocked toolsets (delegation, clarify, etc.) are always removed."""
        child = _strip_blocked_tools(["terminal", "delegation", "clarify", "memory"])
        assert "delegation" not in child
        assert "clarify" not in child
        assert "memory" not in child
        assert "terminal" in child

    def test_empty_intersection_yields_empty_toolsets(self):
        """If parent has no overlap with requested, child gets nothing extra."""
        parent = SimpleNamespace(enabled_toolsets=["terminal"])

        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["web", "browser"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert scoped == []

    def test_resolver_never_grants_tools_missing_from_parent(self):
        parent = SimpleNamespace(
            enabled_toolsets=["terminal"],
            valid_tool_names={"terminal", "process"},
        )

        child_toolsets, child_tool_names = resolve_child_tool_access(
            parent,
            ["web", "browser", "delegate_task"],
            role="orchestrator",
            blocked_tools=DELEGATE_BLOCKED_TOOLS,
            default_toolsets=["terminal", "file", "web"],
        )

        assert child_toolsets == []
        assert child_tool_names == []

    def test_orchestrator_keeps_delegation_only_when_parent_has_it(self):
        parent = SimpleNamespace(
            enabled_toolsets=["terminal", "delegation"],
            valid_tool_names={"terminal", "process", "delegate_task"},
        )

        child_toolsets, child_tool_names = resolve_child_tool_access(
            parent,
            ["terminal", "delegation"],
            role="orchestrator",
            blocked_tools=DELEGATE_BLOCKED_TOOLS,
            default_toolsets=["terminal", "file", "web"],
        )

        assert "delegate_task" in child_tool_names
        assert "delegation" in child_toolsets
