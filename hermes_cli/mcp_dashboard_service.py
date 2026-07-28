"""Dashboard-owned orchestration for hosted MCP OAuth flows.

Protocol mechanics remain in the MCP SDK and ``tools.mcp_oauth``.  This
module owns the process-local flow registry, profile isolation and rollback
needed by the web control plane.  HTTP route concerns stay in ``web_server``.
"""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse, urlunparse

if TYPE_CHECKING:
    from starlette.requests import Request
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow


FLOW_TTL_SECONDS = 15 * 60
MAX_PENDING_FLOWS = 8
flows: dict[str, "DashboardOAuthFlow"] = {}
flows_lock = threading.Lock()
_transactions: dict[tuple[str, str], threading.Lock] = {}
_transactions_lock = threading.Lock()


def collect_expired_flows() -> None:
    cutoff = time.time() - FLOW_TTL_SECONDS
    with flows_lock:
        stale = [
            flow_id
            for flow_id, flow in flows.items()
            if getattr(flow, "created_at", 0) < cutoff
        ]
        for flow_id in stale:
            flows.pop(flow_id, None)


def callback_url_from_base(base_url: str, server_name: str) -> str:
    suffix = quote(server_name, safe="")
    return f"{base_url.rstrip('/')}/api/mcp/oauth/callback/{suffix}"


def callback_url(request: "Request", server_name: str) -> str:
    """Build the externally reachable callback URL for a dashboard flow."""
    from hermes_cli.dashboard_auth.prefix import prefix_from_request, resolve_public_url

    suffix = f"/api/mcp/oauth/callback/{quote(server_name, safe='')}"
    public_url = resolve_public_url()
    if public_url:
        return f"{public_url}{suffix}"
    base = urlparse(str(request.base_url))
    prefix = prefix_from_request(request)
    return urlunparse(
        base._replace(path=f"{prefix}{suffix}", params="", query="", fragment="")
    )


def _transaction(flow: "DashboardOAuthFlow") -> threading.Lock:
    key = (flow.hermes_home, flow.server_name)
    with _transactions_lock:
        return _transactions.setdefault(key, threading.Lock())


def run_oauth(flow: "DashboardOAuthFlow", cfg: dict[str, Any]) -> None:
    """Run one MCP probe with dashboard redirect/callback handlers.

    Token state and the live manager entry are snapshotted before forcing an
    interactive exchange, then restored if any part of authorization fails.
    """
    from hermes_cli.mcp_config import (
        _oauth_tokens_present,
        _probe_single_server,
        _save_mcp_server,
    )

    try:
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools.mcp_dashboard_oauth import dashboard_oauth_flow
        from tools.mcp_oauth import HermesTokenStorage, force_interactive_oauth
        from tools.mcp_oauth_manager import get_manager

        home_token = set_hermes_home_override(flow.hermes_home)
        secret_token = set_secret_scope(
            build_profile_secret_scope(Path(flow.hermes_home))
        )
        try:
            with _transaction(flow), force_interactive_oauth(), dashboard_oauth_flow(
                flow
            ):
                manager = get_manager()
                storage = HermesTokenStorage(flow.server_name)
                backup = storage.snapshot()
                previous_entry = None
                try:
                    previous_entry = manager.remove(
                        flow.server_name,
                        hermes_home=flow.hermes_home,
                    )
                    tools = _probe_single_server(
                        flow.server_name,
                        cfg,
                        connect_timeout=max(
                            float(cfg.get("connect_timeout", 0) or 0), 315
                        ),
                    )
                    if not _oauth_tokens_present(flow.server_name):
                        raise RuntimeError(
                            "The server responded, but no OAuth token was obtained — "
                            "this provider may require a manually-registered OAuth client."
                        )
                    _save_mcp_server(flow.server_name, cfg)
                    flow.tools = [
                        {"name": name, "description": description}
                        for name, description in tools
                    ]
                    flow.mark_approved()
                    if flow.reconnect_live:
                        from tools.mcp_tool import reconnect_mcp_server

                        reconnect_mcp_server(flow.server_name)
                except Exception:
                    storage.restore(backup, only_if_absent=True)
                    manager.restore_entry(
                        flow.server_name,
                        previous_entry,
                        hermes_home=flow.hermes_home,
                    )
                    raise
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if "403" in message and ("regist" in lowered or "forbidden" in lowered):
            message = (
                f"'{flow.server_name}' only allows pre-approved OAuth clients — it "
                "rejected client registration (403), so no browser flow can start. "
                "Add a pre-registered OAuth client to this server or use its stdio "
                "or API-key transport instead."
            )
        flow.mark_error(message)
    finally:
        flow.mark_worker_done()


def new_flow_id() -> str:
    return secrets.token_urlsafe(24)
