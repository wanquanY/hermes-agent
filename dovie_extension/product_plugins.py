"""Typed Dovie product lifecycle for explicitly allowlisted Hermes Plugins."""

from __future__ import annotations

from typing import Any

from dovie_extension.capability_policy import (
    managed_dovie_bundled_plugin_allowlist,
)

_PRODUCT_PLUGIN_IDS = frozenset({"lark-cli"})


def _require_product_plugin(plugin_id: object) -> str:
    normalized = str(plugin_id or "").strip()
    if (
        normalized not in _PRODUCT_PLUGIN_IDS
        or normalized not in managed_dovie_bundled_plugin_allowlist()
    ):
        raise ValueError(f"unsupported Dovie product Plugin: {normalized or '(empty)'}")
    return normalized


def _configured_toolset_state(toolset: str) -> tuple[bool, list[str]]:
    from hermes_cli.config import load_config

    config = load_config()
    platform_toolsets = config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        return False, []
    platforms = sorted(
        str(platform)
        for platform, values in platform_toolsets.items()
        if isinstance(values, list) and toolset in values
    )
    return bool(platforms), platforms


def product_plugin_status(
    plugin_id: object,
    *,
    verify_connection: bool = False,
) -> dict[str, Any]:
    """Project one product-owned Plugin without exposing the generic catalog."""
    name = _require_product_plugin(plugin_id)

    from hermes_cli.plugins_cmd import (
        _get_disabled_set,
        _get_enabled_set,
        _get_plugin_toolset_key,
        _plugin_exists,
    )

    installed = _plugin_exists(name)
    enabled = name in _get_enabled_set() and name not in _get_disabled_set()
    toolset = _get_plugin_toolset_key(name) if installed else None
    toolset_enabled, platforms = (
        _configured_toolset_state(toolset) if toolset else (False, [])
    )

    response: dict[str, Any] = {
        "ok": installed,
        "plugin_id": name,
        "installed": installed,
        # Product lifecycle is profile-scoped, while the control-plane Plugin
        # Manager is process-scoped. Actual code reload happens in the target
        # profile worker via reload.tools; do not project another profile's
        # process-global manager state here.
        "loaded": installed,
        "enabled": enabled and toolset_enabled,
        "toolset": toolset or "",
        "platforms": platforms,
        "runtime_state": (
            "not_installed"
            if not installed
            else "enabled"
            if enabled and toolset_enabled
            else "disabled"
        ),
        "last_error": "",
    }

    if name == "lark-cli":
        from plugins.lark_cli.runtime import LarkCliRuntime
        from plugins.lark_cli.service import LarkCliService

        runtime = LarkCliRuntime()
        probe = runtime.probe()
        response["cli"] = probe.to_dict()
        if not probe.available or not probe.compatible:
            response["ok"] = False
            response["runtime_state"] = "binary_unavailable"
            response["last_error"] = probe.reason
        else:
            health = LarkCliService(runtime=runtime).status(
                verify=verify_connection,
            )
            binding = health.get("binding")
            auth = health.get("auth")
            auth_payload = auth.get("result") if isinstance(auth, dict) else {}
            if (
                isinstance(auth_payload, dict)
                and isinstance(auth_payload.get("data"), dict)
            ):
                auth_payload = auth_payload["data"]
            response["connection"] = {
                "ready": bool(health.get("ok")),
                "binding_state": (
                    "bound"
                    if isinstance(binding, dict) and binding.get("ok")
                    else "not_bound"
                ),
                "identity": (
                    str(auth_payload.get("identity") or "")
                    if isinstance(auth_payload, dict)
                    else ""
                ),
                "verified": (
                    auth_payload.get("verified")
                    if isinstance(auth_payload, dict)
                    else None
                ),
                "next_action": str(health.get("next_action") or ""),
            }
    return response


def set_product_plugin_enabled(
    plugin_id: object,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Mutate only a Dovie-owned allowlisted Plugin activation."""
    name = _require_product_plugin(plugin_id)
    from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=bool(enabled))
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "Plugin activation failed"))
    return {
        **product_plugin_status(name),
        "changed": not bool(result.get("unchanged")),
    }


def product_plugin_statuses() -> list[dict[str, Any]]:
    """Return every product-authorized native Plugin status."""
    allowed = managed_dovie_bundled_plugin_allowlist()
    return [
        product_plugin_status(plugin_id)
        for plugin_id in sorted(_PRODUCT_PLUGIN_IDS & allowed)
    ]


__all__ = [
    "product_plugin_status",
    "product_plugin_statuses",
    "set_product_plugin_enabled",
]
