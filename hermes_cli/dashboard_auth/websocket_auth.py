"""Dashboard WebSocket transport authentication and boundary policy.

HTTP middleware does not run for WebSocket upgrades. This module keeps the
equivalent credential, peer, Host, and Origin rules in one transport-focused
place so every dashboard socket uses the same security model.
"""

from __future__ import annotations

import hmac
import os
import urllib.parse
from typing import Any, Callable, Mapping

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})


def _state_value(state: Any, name: str, default: Any = None) -> Any:
    return getattr(state, name, default)


def client_reason(ws: Any, state: Any) -> str | None:
    """Return a peer-IP rejection reason, or ``None`` when allowed."""
    if bool(_state_value(state, "auth_required", False)):
        return None
    bound_host = str(_state_value(state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in LOOPBACK_HOSTS:
        return None
    client_host = ws.client.host if getattr(ws, "client", None) else ""
    if not client_host:
        return f"missing_or_empty_peer bound={bound_host or '?'}"
    if client_host in LOOPBACK_HOSTS:
        return None
    return f"peer_not_loopback peer={client_host} bound={bound_host or '?'}"


def host_origin_reason(
    ws: Any,
    state: Any,
    *,
    is_accepted_host: Callable[[str, str], bool],
) -> str | None:
    """Return a WebSocket Host/Origin rejection reason, if any."""
    bound_host = _state_value(state, "bound_host", None)
    if not bound_host:
        return None

    host_header = ws.headers.get("host", "")
    if not is_accepted_host(host_header, bound_host):
        return f"host_mismatch host={host_header or '?'} bound={bound_host}"

    origin = ws.headers.get("origin", "")
    if not origin:
        return None
    parsed = urllib.parse.urlparse(origin)
    # Packaged desktop origins (file://, null, app://) cannot be forged by a
    # cross-site web page. The already-validated WS credential remains the
    # authentication boundary for these non-web origins.
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc or not is_accepted_host(parsed.netloc, bound_host):
        return f"origin_mismatch origin={origin} bound={bound_host}"
    return None


def request_reason(
    ws: Any,
    state: Any,
    *,
    is_accepted_host: Callable[[str, str], bool],
) -> str | None:
    """Return the first transport-boundary rejection reason."""
    return host_origin_reason(
        ws,
        state,
        is_accepted_host=is_accepted_host,
    ) or client_reason(ws, state)


def auth_reason(
    ws: Any,
    state: Any,
    *,
    session_token: str,
) -> tuple[str | None, str]:
    """Validate the active WS credential and identify its credential shape."""
    if bool(_state_value(state, "auth_required", False)):
        from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
        from hermes_cli.dashboard_auth.ws_tickets import (
            TicketInvalid,
            consume_internal_credential,
            consume_ticket,
        )

        internal = ws.query_params.get("internal", "")
        if internal:
            try:
                consume_internal_credential(internal)
                return None, "internal"
            except TicketInvalid as exc:
                audit_log(
                    AuditEvent.WS_TICKET_REJECTED,
                    reason=f"internal: {exc}",
                    ip=(ws.client.host if getattr(ws, "client", None) else ""),
                    path=ws.url.path,
                )
                return "internal_invalid", "internal"

        ticket = ws.query_params.get("ticket", "")
        if not ticket:
            return "no_credential", "none"
        try:
            consume_ticket(ticket)
            return None, "ticket"
        except TicketInvalid as exc:
            audit_log(
                AuditEvent.WS_TICKET_REJECTED,
                reason=str(exc),
                ip=(ws.client.host if getattr(ws, "client", None) else ""),
                path=ws.url.path,
            )
            return "ticket_invalid", "ticket"

    token = ws.query_params.get("token", "")
    if not token:
        return "no_credential", "none"
    if hmac.compare_digest(token.encode(), session_token.encode()):
        return None, "token"
    return "token_mismatch", "token"


def _client_ws_host(state: Any) -> str | None:
    explicit = os.environ.get("HERMES_DASHBOARD_WS_HOST", "").strip()
    if explicit:
        return explicit
    host = _state_value(state, "bound_host", None)
    if not host:
        return None
    return "127.0.0.1" if host in WILDCARD_HOSTS else str(host)


def build_server_ws_url(
    path: str,
    state: Any,
    *,
    session_token: str,
    query: Mapping[str, str] | None = None,
) -> str | None:
    """Build an authenticated URL for a server-spawned WebSocket client."""
    host = _client_ws_host(state)
    port = _state_value(state, "bound_port", None)
    if not host or not port:
        return None

    netloc = (
        f"[{host}]:{port}"
        if ":" in host and not host.startswith("[")
        else f"{host}:{port}"
    )
    params: dict[str, str]
    if bool(_state_value(state, "auth_required", False)):
        from hermes_cli.dashboard_auth.ws_tickets import internal_ws_credential

        params = {"internal": internal_ws_credential()}
    else:
        params = {"token": session_token}
    params.update(query or {})
    return f"ws://{netloc}{path}?{urllib.parse.urlencode(params)}"
