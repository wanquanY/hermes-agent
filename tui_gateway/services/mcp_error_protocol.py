"""Stable, product-safe MCP connection failure protocol.

MCP transports are implemented with asyncio/anyio task groups.  Transport
failures therefore frequently arrive wrapped in ``ExceptionGroup`` objects.
Those wrappers are useful in diagnostics, but they are not a meaningful API
contract for gateway clients.  This module owns the boundary between raw
transport exceptions and the stable error codes exposed by the gateway.
"""

from __future__ import annotations

import errno
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse


_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(authorization|token|api[-_ ]?key|secret|password)\s*[:=]\s*[^\s,;]+"
)


def _leaf_exceptions(error: BaseException) -> Iterable[BaseException]:
    nested = getattr(error, "exceptions", None)
    if nested:
        for child in nested:
            if isinstance(child, BaseException):
                yield from _leaf_exceptions(child)
        return
    yield error


def _error_text(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        messages: list[str] = []
        for leaf in _leaf_exceptions(error):
            text = str(leaf).strip() or leaf.__class__.__name__
            if text not in messages:
                messages.append(text)
        rendered = "; ".join(messages[:3])
    else:
        rendered = str(error or "").strip()
    return _CREDENTIAL_PATTERN.sub(r"\1=[REDACTED]", rendered)


def _is_local_http_server(server: dict[str, Any]) -> bool:
    url = str(server.get("url") or "").strip()
    if not url:
        return False
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _contains_errno(error: BaseException, codes: set[int]) -> bool:
    for leaf in _leaf_exceptions(error):
        if getattr(leaf, "errno", None) in codes:
            return True
        for related in (getattr(leaf, "__cause__", None), getattr(leaf, "__context__", None)):
            if isinstance(related, BaseException) and _contains_errno(related, codes):
                return True
    return False


def normalize_mcp_failure(
    error: BaseException | str,
    server: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return a stable error code, state, public message, and diagnostic.

    ``message`` is safe to render in a GUI. ``diagnostic`` preserves the
    sanitized leaf cause for logs and diagnostics surfaces only.
    """

    config = server or {}
    diagnostic = _error_text(error)
    lowered = diagnostic.lower()
    exception = error if isinstance(error, BaseException) else None

    missing_executable = isinstance(exception, FileNotFoundError) or (
        "no such file or directory" in lowered and not config.get("url")
    )
    if missing_executable:
        return {
            "code": "executable_missing",
            "state": "configuration_error",
            "message": "The MCP server executable could not be found",
            "diagnostic": diagnostic,
        }

    authorization_failure = any(
        marker in lowered
        for marker in ("401 unauthorized", "401 client error", "authentication required")
    )
    if authorization_failure:
        return {
            "code": "authorization_required",
            "state": "awaiting_auth",
            "message": "Authorization is required before this MCP server can be used",
            "diagnostic": diagnostic,
        }

    timed_out = isinstance(exception, TimeoutError) or "timed out" in lowered or "timeout" in lowered
    refused = (
        (exception is not None and _contains_errno(exception, {errno.ECONNREFUSED}))
        or any(
            marker in lowered
            for marker in (
                "connection refused",
                "failed to connect",
                "all connection attempts failed",
                "connecterror",
            )
        )
    )

    if _is_local_http_server(config) and (refused or timed_out or diagnostic):
        return {
            "code": "external_runtime_unavailable",
            "state": "awaiting_external_runtime",
            "message": "The local MCP server is not running or is not accepting connections",
            "diagnostic": diagnostic,
        }
    if timed_out:
        return {
            "code": "connection_timeout",
            "state": "unreachable",
            "message": "The MCP server did not respond before the connection timed out",
            "diagnostic": diagnostic,
        }
    if refused:
        return {
            "code": "connection_unreachable",
            "state": "unreachable",
            "message": "The MCP server could not be reached",
            "diagnostic": diagnostic,
        }
    return {
        "code": "connection_failed",
        "state": "unreachable",
        "message": "The MCP connection failed",
        "diagnostic": diagnostic,
    }


def mcp_probe_failure_result(
    name: str,
    server: dict[str, Any],
    error: BaseException | str,
) -> dict[str, Any]:
    failure = normalize_mcp_failure(error, server)
    return {
        "ok": False,
        "connected": False,
        "state": failure["state"],
        "name": name,
        "tools": [],
        "tool_count": 0,
        "prompts": 0,
        "resources": 0,
        "expected_tools": [],
        "error_code": failure["code"],
        "message": failure["message"],
        "diagnostic": failure["diagnostic"],
    }
