"""Dovie-owned capability policy for Hermes-backed desktop runtimes.

Hermes remains the execution substrate, but Dovie owns the product Plugin and
MCP catalogs exposed by its desktop application.  This module is deliberately
independent from the Gateway method registry so config migration, Plugin
loading, and MCP spawning all enforce the same ownership boundary.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any


def is_managed_dovie_runtime() -> bool:
    """Return whether the current process is owned by Dovie Desktop."""
    return bool(
        str(os.getenv("DOVIE_HERMES_CONTROL_HOME") or "").strip()
        or str(os.getenv("DOVIE_PROCESS_ROLE") or "").strip() == "hermes-worker"
        or str(os.getenv("DOVIE_MANAGED_HERMES_GATEWAY") or "").strip() == "1"
    )


@lru_cache(maxsize=1)
def _bundled_hermes_capability_ids() -> tuple[set[str], set[str]]:
    """Return canonical bundled Plugin tokens and their declared MCP names."""
    from hermes_cli.mcp_catalog import _parse_manifest
    from hermes_cli.plugins_cmd import _discover_all_plugins, _read_manifest

    plugin_tokens: set[str] = set()
    plugin_mcp_names: set[str] = set()
    for name, _version, _description, source, directory, canonical_key in (
        _discover_all_plugins()
    ):
        if str(source).strip() != "bundled":
            continue
        plugin_tokens.update(
            token
            for token in (str(name).strip(), str(canonical_key).strip())
            if token
        )
        plugin_root = Path(directory)
        manifest = _read_manifest(plugin_root) if plugin_root.is_dir() else {}
        for declaration in manifest.get("mcp_catalog") or []:
            relative = (
                str(declaration.get("path") or "").strip()
                if isinstance(declaration, dict)
                else str(declaration).strip()
            )
            if not relative:
                continue
            try:
                manifest_path = (plugin_root / relative).resolve()
                manifest_path.relative_to(plugin_root.resolve())
                plugin_mcp_names.add(_parse_manifest(manifest_path).name)
            except Exception:
                # One malformed bundled declaration must not make the policy
                # forget all other identities and accidentally load them.
                continue
    return plugin_tokens, plugin_mcp_names


@lru_cache(maxsize=1)
def _core_hermes_mcp_names() -> set[str]:
    """Return only manifests shipped in Hermes' own optional MCP directory.

    ``mcp_catalog.list_catalog()`` is intentionally not used here because it
    also merges MCP declarations from enabled user Plugins. Those entries are
    user-owned and must survive Dovie's Hermes-builtin retirement.
    """
    from hermes_cli import mcp_catalog

    root = mcp_catalog._catalog_root()
    if not root.is_dir():
        return set()
    names: set[str] = set()
    for manifest_path in (
        child / "manifest.yaml"
        for child in sorted(root.iterdir())
        if (child / "manifest.yaml").is_file()
    ):
        try:
            names.add(mcp_catalog._parse_manifest(manifest_path).name)
        except Exception:
            continue
    return names


def hermes_builtin_mcp_names(
    bundled_plugin_mcp_names: set[str] | None = None,
) -> set[str]:
    """Return every core, preset, or bundled-Plugin MCP catalog identity."""
    from hermes_cli.mcp_config import _MCP_PRESETS

    plugin_mcp_names = bundled_plugin_mcp_names
    if plugin_mcp_names is None:
        _plugin_tokens, plugin_mcp_names = _bundled_hermes_capability_ids()
    names = set(_core_hermes_mcp_names())
    names.update(_MCP_PRESETS)
    names.update(plugin_mcp_names)
    return names


def _dovie_owned(config: object) -> bool:
    if not isinstance(config, dict):
        return False
    owner = config.get("dovie")
    return bool(
        isinstance(owner, dict)
        and str(owner.get("owner_type") or "").strip() in {"plugin", "mcp"}
        and str(owner.get("owner_id") or "").strip()
    )


def reconcile_dovie_capability_ownership() -> dict[str, Any]:
    """Retire Hermes product entries from the active Dovie profile.

    The migration is idempotent and ownership-aware: bundled Hermes Plugins
    and catalog MCPs are removed, while user Plugins, custom MCPs, and
    explicitly Dovie-owned bindings are preserved.
    """
    from hermes_cli.config import load_config, save_config

    config = load_config()
    changed = False

    plugin_tokens, plugin_mcp_names = _bundled_hermes_capability_ids()
    plugins = config.get("plugins")
    removed_plugins: list[str] = []
    if isinstance(plugins, dict):
        enabled = plugins.get("enabled")
        if isinstance(enabled, list):
            next_enabled = [
                value for value in enabled if str(value).strip() not in plugin_tokens
            ]
            removed_plugins = sorted(
                {str(value).strip() for value in enabled}
                - {str(value).strip() for value in next_enabled}
            )
            if next_enabled != enabled:
                plugins["enabled"] = next_enabled
                changed = True

    catalog_names = hermes_builtin_mcp_names(plugin_mcp_names)
    servers = config.get("mcp_servers")
    removed_mcp: list[str] = []
    if isinstance(servers, dict):
        for name in list(servers):
            if name not in catalog_names or _dovie_owned(servers.get(name)):
                continue
            removed_mcp.append(str(name))
            del servers[name]
            changed = True
        if not servers:
            config.pop("mcp_servers", None)

    if changed:
        save_config(config)

    return {
        "ok": True,
        "changed": changed,
        "removed_hermes_plugins": sorted(removed_plugins),
        "removed_hermes_mcp": sorted(removed_mcp),
    }


def reconcile_managed_dovie_runtime() -> dict[str, Any] | None:
    """Reconcile only inside a Dovie-managed gateway or profile worker."""
    if not is_managed_dovie_runtime():
        return None
    return reconcile_dovie_capability_ownership()


def filter_managed_dovie_mcp_servers(
    servers: dict[str, dict],
) -> dict[str, dict]:
    """Fail closed at the spawn boundary for retired Hermes catalog MCPs."""
    if not is_managed_dovie_runtime():
        return servers
    builtin_names = hermes_builtin_mcp_names()
    return {
        name: config
        for name, config in servers.items()
        if name not in builtin_names or _dovie_owned(config)
    }


__all__ = [
    "filter_managed_dovie_mcp_servers",
    "hermes_builtin_mcp_names",
    "is_managed_dovie_runtime",
    "reconcile_dovie_capability_ownership",
    "reconcile_managed_dovie_runtime",
]
