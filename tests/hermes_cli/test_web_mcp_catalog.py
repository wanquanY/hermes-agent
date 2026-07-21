"""Dashboard-facing MCP catalog contract tests."""

import pytest


@pytest.fixture()
def client(monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    monkeypatch.delenv("HERMES_OPTIONAL_MCPS", raising=False)

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def test_dashboard_catalog_exposes_figma_desktop_read_first_entry(client):
    response = client.get("/api/mcp/catalog")

    assert response.status_code == 200
    payload = response.json()
    figma = next(
        entry for entry in payload["entries"] if entry["name"] == "figma-desktop"
    )
    assert {key: figma[key] for key in (
        "name",
        "description",
        "source",
        "transport",
        "auth_type",
        "required_env",
        "needs_install",
        "installed",
        "enabled",
    )} == {
        "name": "figma-desktop",
        "description": "Read Figma design context from the local Figma Desktop MCP server.",
        "source": (
            "https://developers.figma.com/docs/figma-mcp-server/"
            "desktop-server-installation/"
        ),
        "transport": "http",
        "auth_type": "none",
        "required_env": [],
        "needs_install": False,
        "installed": False,
        "enabled": False,
    }
    assert figma["url"] == "http://127.0.0.1:3845/mcp"
    assert figma["command"] is None
    assert figma["args"] == []
    assert figma["install_url"] is None
    assert figma["install_ref"] is None
    assert figma["bootstrap"] == []
    assert figma["default_enabled"][:3] == [
        "get_design_context",
        "get_metadata",
        "get_screenshot",
    ]
    assert "read-first tool subset" in figma["post_install"]
