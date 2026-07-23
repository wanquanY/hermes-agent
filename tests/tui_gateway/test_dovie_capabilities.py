from __future__ import annotations

from dovie_extension import capability_policy
from dovie_extension.capability_policy import (
    filter_managed_dovie_mcp_servers,
    reconcile_dovie_capability_ownership,
)


def test_reconcile_removes_only_hermes_owned_capabilities(monkeypatch) -> None:
    from hermes_cli import config as config_module

    config = {
        "plugins": {
            "enabled": ["productivity/figma", "user/my-plugin"],
            "disabled": [],
        },
        "mcp_servers": {
            "figma-desktop": {"url": "http://127.0.0.1:3845/mcp"},
            "private-docs": {"command": "private-docs-mcp"},
            "dovie-figma": {
                "url": "https://mcp.figma.com/mcp",
                "dovie": {
                    "owner_type": "plugin",
                    "owner_id": "figma",
                    "manifest_version": "1.0.0",
                },
            },
        },
    }
    saved: list[dict] = []
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(config_module, "save_config", lambda value: saved.append(value))
    monkeypatch.setattr(
        capability_policy,
        "_bundled_hermes_capability_ids",
        lambda: ({"productivity/figma", "figma"}, {"figma-desktop"}),
    )
    monkeypatch.setattr(
        capability_policy,
        "_core_hermes_mcp_names",
        lambda: set(),
    )

    result = reconcile_dovie_capability_ownership()

    assert result == {
        "ok": True,
        "changed": True,
        "removed_hermes_plugins": ["productivity/figma"],
        "removed_hermes_mcp": ["figma-desktop"],
    }
    assert config["plugins"]["enabled"] == ["user/my-plugin"]
    assert set(config["mcp_servers"]) == {"private-docs", "dovie-figma"}
    assert saved == [config]


def test_reconcile_is_idempotent(monkeypatch) -> None:
    from hermes_cli import config as config_module

    config = {
        "plugins": {"enabled": ["user/my-plugin"]},
        "mcp_servers": {"private-docs": {"command": "private-docs-mcp"}},
    }
    saved: list[dict] = []
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(config_module, "save_config", lambda value: saved.append(value))
    monkeypatch.setattr(
        capability_policy,
        "_bundled_hermes_capability_ids",
        lambda: (set(), set()),
    )
    monkeypatch.setattr(
        capability_policy,
        "_core_hermes_mcp_names",
        lambda: set(),
    )

    result = reconcile_dovie_capability_ownership()

    assert result["changed"] is False
    assert result["removed_hermes_plugins"] == []
    assert result["removed_hermes_mcp"] == []
    assert saved == []


def test_profile_prepare_reconciles_before_worker_start(monkeypatch) -> None:
    from tui_gateway import server

    monkeypatch.setattr(
        capability_policy,
        "reconcile_dovie_capability_ownership",
        lambda: {
            "ok": True,
            "changed": True,
            "removed_hermes_plugins": ["productivity/figma"],
            "removed_hermes_mcp": ["figma-desktop"],
        },
    )

    response = server.handle_request({
        "id": "prepare-1",
        "method": "profile.prepare_runtime",
        "params": {"agent_profile_id": "profile-1"},
    })

    assert response["result"]["prepared"] is True
    assert response["result"]["runtime_scope_key"] == "profile:profile-1"
    assert response["result"]["capability_reconciliation"]["changed"] is True


def test_spawn_boundary_filters_hermes_builtins_but_preserves_dovie_owned(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DOVIE_MANAGED_HERMES_GATEWAY", "1")
    monkeypatch.setattr(
        capability_policy,
        "hermes_builtin_mcp_names",
        lambda: {"figma-desktop", "linear"},
    )
    servers = {
        "figma-desktop": {"url": "http://127.0.0.1:3845/mcp"},
        "linear": {
            "url": "https://mcp.example.test/linear",
            "dovie": {
                "owner_type": "mcp",
                "owner_id": "linear",
            },
        },
        "private-docs": {"command": "private-docs-mcp"},
    }

    assert filter_managed_dovie_mcp_servers(servers) == {
        "linear": servers["linear"],
        "private-docs": servers["private-docs"],
    }


def test_spawn_boundary_does_not_change_standalone_hermes(monkeypatch) -> None:
    monkeypatch.delenv("DOVIE_HERMES_CONTROL_HOME", raising=False)
    monkeypatch.delenv("DOVIE_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("DOVIE_MANAGED_HERMES_GATEWAY", raising=False)
    servers = {"figma-desktop": {"url": "http://127.0.0.1:3845/mcp"}}

    assert filter_managed_dovie_mcp_servers(servers) is servers


def test_server_projection_uses_the_same_product_ownership_policy(monkeypatch) -> None:
    from tui_gateway.services.capability_runtime import mcp_server_row

    monkeypatch.setattr(
        capability_policy,
        "hermes_builtin_mcp_names",
        lambda: {"figma-desktop"},
    )

    assert mcp_server_row("figma-desktop", {})["origin"] == "hermes_builtin"
    assert mcp_server_row("user-plugin-mcp", {})["origin"] == "custom"
    assert mcp_server_row(
        "dovie-figma",
        {"dovie": {"owner_type": "plugin", "owner_id": "figma"}},
    )["origin"] == "dovie_plugin"


def test_server_projection_exposes_stable_local_runtime_failure() -> None:
    from tui_gateway.services.capability_runtime import mcp_server_row

    row = mcp_server_row(
        "dovie-figma-desktop",
        {"url": "http://127.0.0.1:3845/mcp"},
        {
            "status": "failed",
            "error": "ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)",
        },
    )

    assert row["runtime_state"] == "awaiting_external_runtime"
    assert row["last_error_code"] == "external_runtime_unavailable"
    assert row["last_error"] == (
        "The local MCP server is not running or is not accepting connections"
    )
    assert "ExceptionGroup" not in row["last_error"]
    assert "ExceptionGroup" in row["last_error_detail"]


def test_probe_returns_structured_failure_instead_of_task_group(monkeypatch) -> None:
    from hermes_cli import mcp_config
    from tui_gateway.services.capability_runtime import probe_mcp_capabilities

    def fail_probe(*args, **kwargs):
        del args, kwargs
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ConnectionRefusedError(61, "Connection refused")],
        )

    monkeypatch.setattr(mcp_config, "_probe_single_server", fail_probe)

    result = probe_mcp_capabilities(
        "dovie-figma-desktop",
        {"url": "http://127.0.0.1:3845/mcp"},
    )

    assert result["ok"] is False
    assert result["state"] == "awaiting_external_runtime"
    assert result["error_code"] == "external_runtime_unavailable"
    assert "TaskGroup" not in result["message"]
    assert "Connection refused" in result["diagnostic"]


def test_profile_mcp_reload_refreshes_every_cached_conversation(monkeypatch) -> None:
    from tools import mcp_tool
    from tui_gateway.services.capability_runtime import reload_profile_mcp_runtime

    first_agent = object()
    second_agent = object()
    calls: list[tuple] = []
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: calls.append(("shutdown",)))
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda: calls.append(("discover",)))
    monkeypatch.setattr(
        mcp_tool,
        "refresh_agent_mcp_tools",
        lambda agent, **kwargs: calls.append(("refresh", agent, kwargs)),
    )

    refreshed = reload_profile_mcp_runtime(
        {
            "conversation-1": {"agent": first_agent},
            "conversation-2": {"agent": second_agent},
            "not-built": {},
        },
        lambda: ["mcp", "web"],
    )

    assert refreshed == ["conversation-1", "conversation-2"]
    assert calls[0:2] == [("shutdown",), ("discover",)]
    assert calls[2:] == [
        (
            "refresh",
            first_agent,
            {"enabled_override": ["mcp", "web"], "quiet_mode": True},
        ),
        (
            "refresh",
            second_agent,
            {"enabled_override": ["mcp", "web"], "quiet_mode": True},
        ),
    ]


def test_profile_worker_mcp_bootstrap_discovers_before_invalidating_schema_cache(
    monkeypatch,
) -> None:
    import model_tools
    from tools import mcp_tool
    from tui_gateway.services.capability_runtime import bootstrap_profile_mcp_runtime

    calls: list[str] = []
    monkeypatch.setattr(
        mcp_tool,
        "discover_mcp_tools",
        lambda: calls.append("discover") or ["mcp__figma__get_design_context"],
    )
    monkeypatch.setattr(
        model_tools,
        "invalidate_tool_definitions_cache",
        lambda: calls.append("invalidate"),
    )

    assert bootstrap_profile_mcp_runtime() == ["mcp__figma__get_design_context"]
    assert calls == ["discover", "invalidate"]
