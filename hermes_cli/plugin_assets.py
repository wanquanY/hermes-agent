"""Declarative Skill and MCP assets owned by Hermes plugins.

This module is intentionally independent from :mod:`hermes_cli.plugins` so
asset validation and lookup do not add more responsibilities to the plugin
loader.  Directory plugins may declare read-only assets in ``plugin.yaml``::

    skills:
      - name: figma
        path: skills/figma/SKILL.md
        description: Use Figma MCP for design inspection and design-to-code.
    mcp_catalog:
      - path: mcp/figma-desktop/manifest.yaml

The plugin manager decides whether a plugin is enabled.  Only assets from an
enabled plugin are registered here.  Every asset path is resolved inside the
owning plugin directory, including symlink resolution, so a manifest cannot
publish arbitrary host files through ``skill_view`` or the MCP catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


@dataclass(frozen=True)
class SkillAssetDeclaration:
    """A validated, relative Skill asset declaration from ``plugin.yaml``."""

    name: str
    path: str
    description: str = ""


@dataclass(frozen=True)
class McpCatalogAssetDeclaration:
    """A validated, relative MCP catalog declaration from ``plugin.yaml``."""

    path: str


@dataclass(frozen=True)
class RegisteredPluginSkill:
    """Runtime metadata for a Skill exposed by an enabled plugin."""

    qualified_name: str
    plugin_name: str
    plugin_key: str
    bare_name: str
    path: Path
    description: str = ""


@dataclass(frozen=True)
class RegisteredMcpCatalogManifest:
    """Runtime metadata for an MCP manifest exposed by an enabled plugin."""

    plugin_name: str
    plugin_key: str
    path: Path


def _relative_asset_path(value: Any, *, field_name: str) -> str:
    """Validate and normalize a portable path from a plugin manifest."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty relative path")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must stay inside the plugin directory: {value!r}")
    return path.as_posix()


def parse_asset_declarations(
    data: dict[str, Any],
) -> tuple[tuple[SkillAssetDeclaration, ...], tuple[McpCatalogAssetDeclaration, ...]]:
    """Parse the declarative asset blocks from a plugin manifest mapping."""

    raw_skills = data.get("skills", [])
    if not isinstance(raw_skills, list):
        raise ValueError("plugin skills must be a list of mappings")

    skill_names: set[str] = set()
    skills: list[SkillAssetDeclaration] = []
    for index, raw in enumerate(raw_skills):
        if not isinstance(raw, dict):
            raise ValueError(f"plugin skills[{index}] must be a mapping")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"plugin skills[{index}].name must be a non-empty string")
        if ":" in name:
            raise ValueError(f"plugin skill name must not contain ':': {name!r}")
        if name in skill_names:
            raise ValueError(f"duplicate plugin skill declaration: {name}")
        skill_names.add(name)
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"plugin skills[{index}].description must be a string")
        skills.append(
            SkillAssetDeclaration(
                name=name,
                path=_relative_asset_path(
                    raw.get("path"), field_name=f"plugin skills[{index}].path"
                ),
                description=description.strip(),
            )
        )

    raw_mcp = data.get("mcp_catalog", [])
    if not isinstance(raw_mcp, list):
        raise ValueError("plugin mcp_catalog must be a list of mappings")

    mcp_paths: set[str] = set()
    mcp_catalog: list[McpCatalogAssetDeclaration] = []
    for index, raw in enumerate(raw_mcp):
        if not isinstance(raw, dict):
            raise ValueError(f"plugin mcp_catalog[{index}] must be a mapping")
        path = _relative_asset_path(
            raw.get("path"), field_name=f"plugin mcp_catalog[{index}].path"
        )
        if path in mcp_paths:
            raise ValueError(f"duplicate plugin MCP catalog declaration: {path}")
        mcp_paths.add(path)
        mcp_catalog.append(McpCatalogAssetDeclaration(path=path))

    return tuple(skills), tuple(mcp_catalog)


def _resolve_owned_file(plugin_root: Path, relative_path: str, *, label: str) -> Path:
    """Resolve an asset and prove it remains inside its owning plugin root."""

    root = plugin_root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes plugin directory: {relative_path!r}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"{label} not found at {candidate}")
    return candidate


class PluginAssetRegistry:
    """Process-local registry of assets from currently enabled plugins."""

    def __init__(self) -> None:
        self._skills: dict[str, RegisteredPluginSkill] = {}
        self._mcp_manifests: dict[Path, RegisteredMcpCatalogManifest] = {}

    def clear(self) -> None:
        self._skills.clear()
        self._mcp_manifests.clear()

    def register_skill(
        self,
        *,
        plugin_name: str,
        plugin_key: str,
        plugin_root: Path | None,
        name: str,
        path: Path,
        description: str = "",
    ) -> RegisteredPluginSkill:
        """Register a Skill, enforcing namespace and plugin ownership rules."""

        from agent.skill_utils import _NAMESPACE_RE

        if ":" in name:
            raise ValueError(
                f"Skill name '{name}' must not contain ':' "
                f"(the namespace is derived from '{plugin_name}')."
            )
        if not name or not _NAMESPACE_RE.match(name):
            raise ValueError(f"Invalid skill name '{name}'. Must match [a-zA-Z0-9_-]+.")

        resolved = path.resolve()
        if plugin_root is not None:
            root = plugin_root.resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"Plugin skill '{name}' must stay inside {root}: {resolved}"
                ) from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"SKILL.md not found at {resolved}")
        if resolved.name != "SKILL.md":
            raise ValueError(f"Plugin skill '{name}' must point to a SKILL.md file")

        qualified = f"{plugin_name}:{name}"
        existing = self._skills.get(qualified)
        if existing is not None and existing.plugin_key != plugin_key:
            raise ValueError(
                f"Plugin skill '{qualified}' is already owned by {existing.plugin_key}"
            )
        entry = RegisteredPluginSkill(
            qualified_name=qualified,
            plugin_name=plugin_name,
            plugin_key=plugin_key,
            bare_name=name,
            path=resolved,
            description=description,
        )
        self._skills[qualified] = entry
        return entry

    def register_manifest_assets(
        self,
        *,
        plugin_name: str,
        plugin_key: str,
        plugin_root: Path,
        skills: Iterable[SkillAssetDeclaration],
        mcp_catalog: Iterable[McpCatalogAssetDeclaration],
    ) -> None:
        """Register all declarations after their owning plugin has loaded."""

        staged_skills: list[tuple[SkillAssetDeclaration, Path]] = []
        staged_mcp: list[Path] = []
        for declaration in skills:
            path = _resolve_owned_file(
                plugin_root, declaration.path, label=f"Plugin skill '{declaration.name}'"
            )
            staged_skills.append((declaration, path))
        for declaration in mcp_catalog:
            path = _resolve_owned_file(
                plugin_root, declaration.path, label="Plugin MCP catalog manifest"
            )
            if path.name not in {"manifest.yaml", "manifest.yml"}:
                raise ValueError(f"Plugin MCP catalog asset must be a manifest YAML: {path}")
            staged_mcp.append(path)

        # All file validation happens before mutation, so a broken declaration
        # cannot leave a partially registered asset set.
        for declaration, path in staged_skills:
            self.register_skill(
                plugin_name=plugin_name,
                plugin_key=plugin_key,
                plugin_root=plugin_root,
                name=declaration.name,
                path=path,
                description=declaration.description,
            )
        for path in staged_mcp:
            self._mcp_manifests[path] = RegisteredMcpCatalogManifest(
                plugin_name=plugin_name,
                plugin_key=plugin_key,
                path=path,
            )

    def find_skill(self, qualified_name: str) -> Path | None:
        entry = self._skills.get(qualified_name)
        return entry.path if entry else None

    def skill_entries(self, plugin_name: str | None = None) -> list[RegisteredPluginSkill]:
        entries = self._skills.values()
        if plugin_name is not None:
            entries = (entry for entry in entries if entry.plugin_name == plugin_name)
        return sorted(entries, key=lambda entry: entry.qualified_name)

    def remove_skill(self, qualified_name: str) -> None:
        self._skills.pop(qualified_name, None)

    def remove_plugin(self, plugin_key: str) -> None:
        """Remove every asset owned by one plugin after a failed load."""

        self._skills = {
            name: entry
            for name, entry in self._skills.items()
            if entry.plugin_key != plugin_key
        }
        self._mcp_manifests = {
            path: entry
            for path, entry in self._mcp_manifests.items()
            if entry.plugin_key != plugin_key
        }

    def mcp_manifests(self) -> list[RegisteredMcpCatalogManifest]:
        return sorted(self._mcp_manifests.values(), key=lambda entry: str(entry.path))
