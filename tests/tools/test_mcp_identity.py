from tools.mcp_identity import (
    canonical_mcp_tool_name,
    from_anthropic_oauth_wire_name,
    is_canonical_mcp_tool_name,
    legacy_mcp_tool_name,
    resolve_legacy_mcp_tool_name,
    split_canonical_mcp_tool_name,
    to_anthropic_oauth_wire_name,
)


def test_canonical_name_has_unambiguous_double_delimiters():
    name = canonical_mcp_tool_name("my__server", "read__file")
    assert name == "mcp__my_server__read_file"
    assert split_canonical_mcp_tool_name(name) == ("my_server", "read_file")
    assert is_canonical_mcp_tool_name(name)


def test_legacy_name_resolves_only_against_one_known_canonical_entry():
    canonical = canonical_mcp_tool_name("filesystem", "read_file")
    assert resolve_legacy_mcp_tool_name(
        "mcp_filesystem_read_file", [canonical]
    ) == canonical


def test_legacy_ambiguity_fails_closed():
    candidates = [
        canonical_mcp_tool_name("a_b", "tool"),
        canonical_mcp_tool_name("a", "b_tool"),
    ]
    assert legacy_mcp_tool_name("a_b", "tool") == "mcp_a_b_tool"
    assert resolve_legacy_mcp_tool_name("mcp_a_b_tool", candidates) is None


def test_non_mcp_name_never_migrates():
    assert resolve_legacy_mcp_tool_name("terminal", ["mcp__x__y"]) is None


def test_anthropic_oauth_wire_round_trip_prefers_canonical_registry_name():
    canonical = canonical_mcp_tool_name("linear", "get_issue")
    known = ["terminal", canonical]
    assert to_anthropic_oauth_wire_name("terminal") == "mcp__terminal"
    assert to_anthropic_oauth_wire_name(canonical) == canonical
    assert from_anthropic_oauth_wire_name("mcp__terminal", known) == "terminal"
    assert from_anthropic_oauth_wire_name(canonical, known) == canonical


def test_anthropic_oauth_replayed_legacy_name_migrates_to_canonical():
    canonical = canonical_mcp_tool_name("linear", "get_issue")
    assert to_anthropic_oauth_wire_name("mcp_linear_get_issue") == "mcp__linear_get_issue"
    assert from_anthropic_oauth_wire_name(
        "mcp__linear_get_issue", [canonical]
    ) == canonical


def test_registry_is_the_single_legacy_read_migration_boundary():
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    canonical = canonical_mcp_tool_name("filesystem", "read_file")
    registry.register(
        name=canonical,
        toolset="mcp-filesystem",
        schema={"name": canonical, "description": "read", "parameters": {}},
        handler=lambda args: args["path"],
    )

    assert registry.resolve_name("mcp_filesystem_read_file") == canonical
    assert registry.dispatch("mcp_filesystem_read_file", {"path": "ok"}) == "ok"
    definitions = registry.get_definitions({"mcp_filesystem_read_file"})
    assert definitions[0]["function"]["name"] == canonical
    assert "mcp_filesystem_read_file" not in registry.get_all_tool_names()


def test_registry_legacy_ambiguity_does_not_dispatch():
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    for server, tool in (("a_b", "tool"), ("a", "b_tool")):
        name = canonical_mcp_tool_name(server, tool)
        registry.register(
            name=name,
            toolset=f"mcp-{server}",
            schema={"name": name, "description": "x", "parameters": {}},
            handler=lambda args: "unexpected",
        )
    assert registry.resolve_name("mcp_a_b_tool") is None
    assert "Unknown tool" in registry.dispatch("mcp_a_b_tool", {})


def test_sanitized_server_collision_fails_closed_instead_of_overwriting():
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from tools.mcp_tool import (
        MCPServerTask,
        _mcp_tool_server_names,
        _register_server_tools,
    )
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    tool = SimpleNamespace(
        name="ping",
        description="ping",
        inputSchema={"type": "object", "properties": {}},
    )
    first = MCPServerTask("my-server")
    first.session = MagicMock()
    first._tools = [tool]
    second = MCPServerTask("my_server")
    second.session = MagicMock()
    second._tools = [tool]
    canonical = canonical_mcp_tool_name("my-server", "ping")

    try:
        with patch("tools.registry.registry", registry):
            assert canonical in _register_server_tools(
                "my-server", first, {"command": "first"}
            )
            second_names = _register_server_tools(
                "my_server", second, {"command": "second"}
            )
        assert canonical not in second_names
        assert _mcp_tool_server_names[canonical] == "my-server"
        assert registry.get_toolset_for_tool(canonical) == "mcp-my-server"
    finally:
        for name in list(_mcp_tool_server_names):
            if name.startswith("mcp__my_server__"):
                _mcp_tool_server_names.pop(name, None)
