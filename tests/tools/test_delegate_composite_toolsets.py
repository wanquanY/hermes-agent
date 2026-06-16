"""Tests for composite toolset expansion in delegate_task intersection."""

import unittest
from types import SimpleNamespace

from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS
from tools.delegate_tool_access import (
    _expand_parent_toolsets,
    resolve_child_tool_access,
)


class TestExpandParentToolsets(unittest.TestCase):
    """Verify _expand_parent_toolsets recognises individual toolsets within composites."""

    def test_composite_hermes_cli_expands_web(self):
        """hermes-cli includes web_search/web_extract → 'web' should be in expansion."""
        expanded = _expand_parent_toolsets({"hermes-cli"})
        self.assertIn("web", expanded)
        self.assertIn("terminal", expanded)
        self.assertIn("browser", expanded)
        # Original composite is preserved
        self.assertIn("hermes-cli", expanded)

    def test_individual_toolset_unchanged(self):
        """When parent already uses individual toolsets, expansion keeps them."""
        expanded = _expand_parent_toolsets({"web", "terminal"})
        self.assertIn("web", expanded)
        self.assertIn("terminal", expanded)

    def test_empty_parent_toolsets(self):
        expanded = _expand_parent_toolsets(set())
        self.assertEqual(expanded, set())

    def test_unknown_toolset_passthrough(self):
        """Unknown toolset names pass through without error."""
        expanded = _expand_parent_toolsets({"nonexistent-toolset-xyz"})
        self.assertIn("nonexistent-toolset-xyz", expanded)

    def test_intersection_with_expanded_composite(self):
        """End-to-end: requesting ['web'] from parent with ['hermes-cli'] yields ['web']."""
        parent_toolsets = {"hermes-cli"}
        expanded = _expand_parent_toolsets(parent_toolsets)
        toolsets = ["web"]
        child_toolsets = [t for t in toolsets if t in expanded]
        self.assertEqual(child_toolsets, ["web"])

    def test_child_access_allows_subset_from_composite_parent(self):
        """delegate_task access resolution allows a concrete subset of a composite parent."""
        parent = SimpleNamespace(
            enabled_toolsets=["hermes-cli"],
            valid_tool_names={"web_search", "web_extract", "terminal", "process"},
        )

        child_toolsets, child_tool_names = resolve_child_tool_access(
            parent,
            ["web"],
            role="leaf",
            blocked_tools=DELEGATE_BLOCKED_TOOLS,
            default_toolsets=["terminal", "file", "web"],
        )

        self.assertEqual(child_toolsets, ["web"])
        self.assertEqual(child_tool_names, ["web_extract", "web_search"])


if __name__ == "__main__":
    unittest.main()
