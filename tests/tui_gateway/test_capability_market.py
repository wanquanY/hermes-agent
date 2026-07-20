import types
from unittest.mock import patch

from tui_gateway import server


def test_plugins_list_returns_every_discovered_plugin_with_canonical_key(tmp_path):
    plugin_dir = tmp_path / "plugins" / "firecrawl"
    plugin_dir.mkdir(parents=True)
    discovered = [
        (
            "firecrawl",
            "1.2.3",
            "Web extraction",
            "bundled",
            str(plugin_dir),
            "browser/firecrawl",
        )
    ]
    manifest = {
        "label": "Firecrawl",
        "hooks": ["before_tool"],
        "provides_tools": ["firecrawl_scrape"],
        "requires_env": [{"name": "FIRECRAWL_API_KEY", "secret": True}],
    }
    with (
        patch("hermes_cli.plugins_cmd._discover_all_plugins", return_value=discovered),
        patch(
            "hermes_cli.plugins_cmd._get_enabled_set",
            return_value={"browser/firecrawl"},
        ),
        patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set()),
        patch("hermes_cli.plugins_cmd._read_manifest", return_value=manifest),
        patch(
            "hermes_cli.plugins_cmd._missing_requires_env_names",
            return_value=["FIRECRAWL_API_KEY"],
        ),
        patch("hermes_cli.config.get_hermes_home", return_value=tmp_path),
    ):
        response = server.handle_request({
            "id": "1",
            "method": "plugins.list",
            "params": {},
        })

    plugin = response["result"]["plugins"][0]
    assert plugin["name"] == "browser/firecrawl"
    assert plugin["manifest_name"] == "firecrawl"
    assert plugin["display_name"] == "Firecrawl"
    assert plugin["runtime_status"] == "enabled"
    assert plugin["hooks"] == ["before_tool"]
    assert plugin["provides_tools"] == ["firecrawl_scrape"]


def test_mcp_catalog_list_combines_catalog_and_builtin_presets():
    entry = types.SimpleNamespace(
        name="linear",
        description="Linear MCP",
        source="Hermes catalog",
        transport=types.SimpleNamespace(
            type="http",
            command=None,
            args=[],
            url="https://mcp.linear.app",
        ),
        auth=types.SimpleNamespace(
            type="oauth",
            env=[
                types.SimpleNamespace(
                    name="LINEAR_TOKEN",
                    prompt="Linear token",
                    required=True,
                    secret=True,
                    default="",
                )
            ],
        ),
        install=None,
        tools=types.SimpleNamespace(default_enabled=[]),
        post_install="",
    )
    with (
        patch("hermes_cli.mcp_catalog.list_catalog", return_value=[entry]),
        patch("hermes_cli.mcp_catalog.catalog_diagnostics", return_value=[]),
        patch("hermes_cli.mcp_config._get_mcp_servers", return_value={}),
        patch.dict(
            "hermes_cli.mcp_config._MCP_PRESETS",
            {"codex": {"command": "codex", "args": ["mcp-server"]}},
            clear=True,
        ),
        patch("hermes_cli.config.get_env_value", return_value=""),
    ):
        response = server.handle_request({
            "id": "1",
            "method": "mcp.catalog.list",
            "params": {},
        })

    rows = {row["name"]: row for row in response["result"]["entries"]}
    assert set(rows) == {"linear", "codex"}
    assert rows["linear"]["missing_env"] == ["LINEAR_TOKEN"]
    assert rows["codex"]["kind"] == "preset"


def test_mcp_manage_persists_explicit_tool_permissions():
    config = {
        "mcp_servers": {
            "figma-desktop": {
                "url": "http://127.0.0.1:3845/mcp",
                "tools": {"exclude": ["write_design"]},
            }
        }
    }
    with (
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.save_config") as save_config,
    ):
        response = server.handle_request({
            "id": "1",
            "method": "mcp.manage",
            "params": {
                "action": "set_tools",
                "name": "figma-desktop",
                "tools": ["get_metadata", "get_screenshot", "get_metadata"],
            },
        })

    assert response["result"]["enabled_tools"] == ["get_metadata", "get_screenshot"]
    tools = config["mcp_servers"]["figma-desktop"]["tools"]
    assert tools == {"include": ["get_metadata", "get_screenshot"]}
    save_config.assert_called_once_with(config)
