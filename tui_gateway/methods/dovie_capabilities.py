"""Gateway adapter for Dovie-owned capability reconciliation."""

from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals

from dovie_extension.capability_policy import (
    filter_managed_dovie_mcp_servers,
    hermes_builtin_mcp_names,
    is_managed_dovie_runtime,
    reconcile_dovie_capability_ownership,
    reconcile_managed_dovie_runtime,
)

_server = bind_server_globals(globals())


@method("dovie.capabilities.reconcile")
def _reconcile(rid: object, params: dict) -> dict:
    del params
    try:
        from dovie_extension.product_plugins import product_plugin_statuses

        result = reconcile_dovie_capability_ownership()
        result["product_plugins"] = product_plugin_statuses()
        return _ok(rid, result)
    except Exception as exc:
        return _err(rid, 5041, str(exc))


__all__ = [
    "reconcile_dovie_capability_ownership",
    "reconcile_managed_dovie_runtime",
    "filter_managed_dovie_mcp_servers",
    "hermes_builtin_mcp_names",
    "is_managed_dovie_runtime",
]
