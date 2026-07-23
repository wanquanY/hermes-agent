"""Profile-owned MCP and plugin capability lifecycle orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from typing import Any

from tui_gateway.services.capability_operations import capability_operations
from tui_gateway.services.mcp_error_protocol import (
    mcp_probe_failure_result,
    normalize_mcp_failure,
)

logger = logging.getLogger(__name__)


def bootstrap_profile_mcp_runtime() -> list[str]:
    """Materialize profile MCP tools inside the executable worker registry.

    The control plane owns configuration and health probes, but it cannot
    register tools for an isolated run worker: Python registries are
    process-local. Worker readiness therefore includes MCP discovery before
    the first tool-schema snapshot is built.
    """

    from model_tools import invalidate_tool_definitions_cache
    from tools.mcp_tool import discover_mcp_tools

    tool_names = list(discover_mcp_tools() or [])
    invalidate_tool_definitions_cache()
    return tool_names


def mcp_server_row(name: str, config: dict, runtime: dict | None = None) -> dict:
    transport = "http" if config.get("url") else "stdio" if config.get("command") else "unknown"
    tools = config.get("tools") if isinstance(config.get("tools"), dict) else {}
    dovie_owner = config.get("dovie") if isinstance(config.get("dovie"), dict) else {}
    owner_type = str(dovie_owner.get("owner_type") or "").strip()
    owner_id = str(dovie_owner.get("owner_id") or "").strip()
    if owner_type in {"plugin", "mcp"} and owner_id:
        origin = f"dovie_{owner_type}"
    else:
        # Legacy Hermes catalog installs predate ownership metadata. Derive
        # their origin at the read boundary so Dovie can retire the Hermes
        # product catalog without hiding or deleting unrelated custom MCPs.
        from dovie_extension.capability_policy import hermes_builtin_mcp_names

        origin = (
            "hermes_builtin"
            if name in hermes_builtin_mcp_names()
            else "custom"
        )
    runtime = runtime or {}
    raw_error = str(runtime.get("error") or runtime.get("last_error") or "").strip()
    failure = normalize_mcp_failure(raw_error, config) if raw_error else None
    return {
        "name": name,
        "origin": origin,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "owner_version": str(dovie_owner.get("manifest_version") or "").strip(),
        "transport": transport,
        "url": str(config.get("url") or ""),
        "command": str(config.get("command") or ""),
        "args": [str(item) for item in config.get("args") or []],
        "auth_type": str(config.get("auth") or ("header" if config.get("headers") else "none")),
        "enabled": config.get("enabled", True) is not False,
        "env_keys": sorted(str(key) for key in (config.get("env") or {}).keys()),
        "tool_filter_configured": "include" in tools,
        "enabled_tools": [str(item) for item in tools.get("include") or []],
        "connected": bool(runtime.get("connected", False)),
        "runtime_state": str(
            (failure or {}).get("state")
            or runtime.get("status")
            or runtime.get("state")
            or "not_started"
        ),
        "discovered_tool_count": int(runtime.get("tools") or 0),
        "last_error_code": str((failure or {}).get("code") or ""),
        "last_error": str((failure or {}).get("message") or ""),
        "last_error_detail": str((failure or {}).get("diagnostic") or ""),
    }


def probe_mcp_capabilities(name: str, server: dict) -> dict:
    """Probe transport and capability health without conflating it with config state."""
    from hermes_cli import mcp_catalog
    from hermes_cli.mcp_config import _probe_single_server

    details: dict = {}
    try:
        tools = _probe_single_server(name, server, details=details)
    except BaseException as exc:
        return mcp_probe_failure_result(name, server, exc)
    entry = mcp_catalog.get_entry(name)
    expected_tools = list(entry.tools.default_enabled or []) if entry else []
    tool_rows = [
        {"name": tool_name, "description": description}
        for tool_name, description in tools
    ]
    prompts = int(details.get("prompts") or 0)
    resources = int(details.get("resources") or 0)
    usable = bool(tool_rows or prompts or resources)
    state = "ready" if usable else "connected_empty"
    return {
        "ok": usable,
        "connected": True,
        "state": state,
        "name": name,
        "tools": tool_rows,
        "tool_count": len(tool_rows),
        "prompts": prompts,
        "resources": resources,
        "expected_tools": expected_tools,
        "message": (
            "Connection established and capabilities discovered"
            if usable
            else "Connection established but the server exposed no tools, prompts, or resources"
        ),
    }


def reload_profile_mcp_runtime(
    sessions: Mapping[str, Any],
    enabled_toolsets_loader: Callable[[], list[str] | None],
) -> list[str]:
    from tools.mcp_tool import (
        discover_mcp_tools,
        refresh_agent_mcp_tools,
        shutdown_mcp_servers,
    )

    shutdown_mcp_servers()
    discover_mcp_tools()
    # One profile worker can own several open conversations. Refresh every
    # cached agent snapshot so the installed tools become visible immediately.
    enabled_toolsets = enabled_toolsets_loader()
    refreshed_session_ids: list[str] = []
    for session_id, session in list(sessions.items()):
        agent = session.get("agent") if isinstance(session, dict) else None
        if agent is None:
            continue
        try:
            refresh_agent_mcp_tools(
                agent,
                enabled_override=enabled_toolsets,
                quiet_mode=True,
            )
            refreshed_session_ids.append(str(session_id))
        except Exception:
            logger.warning(
                "Failed to refresh cached agent tools after capability operation",
                exc_info=True,
            )
    return refreshed_session_ids


def _oauth_context(update):
    from tools.mcp_oauth import force_interactive_oauth, observe_oauth_authorization

    def authorization_ready(url: str) -> None:
        update(
            status="running",
            phase="awaiting_auth",
            progress=42,
            message="Complete authorization in your browser",
            details={"authorization_url": url},
        )

    class OAuthContext:
        def __enter__(self):
            self._interactive = force_interactive_oauth()
            self._interactive.__enter__()
            self._observer = observe_oauth_authorization(
                authorization_ready,
                external_browser=True,
            )
            self._observer.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            self._observer.__exit__(exc_type, exc, tb)
            self._interactive.__exit__(exc_type, exc, tb)

    return OAuthContext()


def _unready_result(result: dict, *, phase: str, message: str) -> dict:
    return {
        **result,
        "operation_status": "degraded",
        "operation_phase": phase,
        "operation_message": message,
    }


def start_capability_operation(
    params: dict,
    *,
    emit: Callable[[dict], None],
    sessions: Mapping[str, Any],
    enabled_toolsets_loader: Callable[[], list[str] | None],
) -> dict:
    from hermes_cli import mcp_catalog
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.mcp_config import _get_mcp_servers

    action = str(params.get("action") or "").strip().lower()
    target = str(params.get("name") or params.get("identifier") or "").strip()
    if not action or not target:
        raise ValueError("capability operation action and target are required")

    def reload_runtime() -> None:
        reload_profile_mcp_runtime(sessions, enabled_toolsets_loader)

    if action == "mcp.install":
        entry = mcp_catalog.get_entry(target)
        if entry is None:
            from hermes_cli.mcp_config import _MCP_PRESETS, _save_mcp_server

            preset = _MCP_PRESETS.get(target)
            if preset is None:
                raise ValueError(f"unknown MCP catalog entry: {target}")

            def runner(update):
                update(phase="configuring", progress=35, message="Saving MCP preset")
                if not _save_mcp_server(target, dict(preset)):
                    raise RuntimeError("MCP preset configuration rejected")
                update(phase="discovering", progress=62, message="Discovering MCP capabilities")
                result = probe_mcp_capabilities(target, dict(preset))
                update(phase="reloading", progress=90, message="Reloading MCP runtime")
                reload_runtime()
                if result.get("ok"):
                    return result
                return _unready_result(
                    result,
                    phase=str(result.get("state") or "unreachable"),
                    message=str(
                        result.get("message") or "MCP configured but capabilities are not ready"
                    ),
                )
        else:
            supplied_env = params.get("env") if isinstance(params.get("env"), dict) else {}
            for key, value in supplied_env.items():
                if str(value):
                    save_env_value(str(key), str(value))
            missing = [
                item.name
                for item in entry.auth.env
                if item.required and not get_env_value(item.name)
            ]
            if missing:
                raise ValueError(f"missing required environment: {', '.join(missing)}")

            def runner(update):
                auth = _oauth_context(update) if entry.auth.type == "oauth" else nullcontext()
                with auth:
                    result = mcp_catalog.install_entry(
                        entry,
                        enable=bool(params.get("enabled", True)),
                        progress=update,
                    )
                    if entry.auth.type != "oauth" or result.get("probe_status") in {"ready", "empty"}:
                        update(phase="reloading", progress=90, message="Reloading MCP runtime")
                        reload_runtime()
                probe_status = str(result.get("probe_status") or "")
                if probe_status == "ready":
                    return result
                if entry.auth.type == "oauth" and probe_status == "unreachable":
                    return _unready_result(
                        result,
                        phase="awaiting_auth",
                        message="Authorization is still required",
                    )
                if str(entry.transport.url or "").startswith(("http://127.0.0.1", "http://localhost")):
                    return _unready_result(
                        result,
                        phase="awaiting_external_runtime",
                        message="Start the external desktop MCP server and test again",
                    )
                return _unready_result(
                    result,
                    phase="connected_empty" if probe_status == "empty" else "unreachable",
                    message="MCP configured but capabilities are not ready",
                )
    elif action == "mcp.authorize":
        server = _get_mcp_servers().get(target)
        if not server:
            raise ValueError(f"MCP server not found: {target}")
        if str(server.get("auth") or "") != "oauth":
            raise ValueError(f"MCP server does not use OAuth: {target}")

        def runner(update):
            with _oauth_context(update):
                update(phase="awaiting_auth", progress=30, message="Starting browser authorization")
                result = probe_mcp_capabilities(target, server)
                update(phase="reloading", progress=90, message="Reloading MCP runtime")
                reload_runtime()
                if not result.get("ok"):
                    return _unready_result(
                        result,
                        phase=str(result.get("state") or "connected_empty"),
                        message=str(
                            result.get("message")
                            or "Authorization completed but no capabilities were discovered"
                        ),
                    )
                return result
    elif action == "plugin.install":
        identifier = target

        def runner(update):
            from hermes_cli.plugins_cmd import dashboard_install_plugin

            update(phase="resolving", progress=10, message="Resolving plugin source")
            update(phase="installing", progress=35, message="Downloading and installing plugin")
            result = dashboard_install_plugin(
                identifier,
                force=bool(params.get("force", False)),
                enable=bool(params.get("enable", False)),
            )
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "plugin operation failed"))
            result.pop("after_install_path", None)
            update(phase="refreshing", progress=90, message="Refreshing plugin inventory")
            return result
    else:
        raise ValueError(f"unsupported capability operation: {action}")

    return capability_operations.start(
        kind=action,
        target=target,
        runner=runner,
        emit=emit,
        details={
            "runtime_scope_key": str(
                params.get("runtime_scope_key") or params.get("runtimeScopeKey") or ""
            ),
        },
    )
