"""Tests for plugin-owned Skill and MCP capability assets."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli.plugin_assets import (
    PluginAssetRegistry,
    parse_asset_declarations,
)
from hermes_cli.plugins import PluginManager


def _write_capability_plugin(root: Path, *, name: str = "design") -> Path:
    plugin = root / "productivity" / name
    skill = plugin / "skills" / name / "SKILL.md"
    mcp = plugin / "mcp" / f"{name}-desktop" / "manifest.yaml"
    skill.parent.mkdir(parents=True)
    mcp.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: Use {name} safely.\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    mcp.write_text(
        yaml.safe_dump(
            {
                "manifest_version": 1,
                "name": f"{name}-desktop",
                "description": f"Use {name} desktop.",
                "source": "https://example.com",
                "transport": {"type": "http", "url": "http://127.0.0.1:9876/mcp"},
                "auth": {"type": "none"},
            }
        ),
        encoding="utf-8",
    )
    (plugin / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "kind": "capability",
                "skills": [
                    {
                        "name": name,
                        "path": f"skills/{name}/SKILL.md",
                        "description": f"Use {name} safely.",
                    }
                ],
                "mcp_catalog": [
                    {"path": f"mcp/{name}-desktop/manifest.yaml"}
                ],
            }
        ),
        encoding="utf-8",
    )
    return plugin


def test_manifest_asset_paths_must_be_relative_and_confined():
    with pytest.raises(ValueError, match="inside the plugin directory"):
        parse_asset_declarations(
            {"skills": [{"name": "bad", "path": "../outside/SKILL.md"}]}
        )
    with pytest.raises(ValueError, match="inside the plugin directory"):
        parse_asset_declarations(
            {"mcp_catalog": [{"path": "/tmp/manifest.yaml"}]}
        )


def test_registry_rejects_skill_symlink_escape(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("# Outside\n", encoding="utf-8")
    linked = plugin / "SKILL.md"
    linked.symlink_to(outside)

    registry = PluginAssetRegistry()
    with pytest.raises(ValueError, match="must stay inside"):
        registry.register_skill(
            plugin_name="demo",
            plugin_key="demo",
            plugin_root=plugin,
            name="demo",
            path=linked,
        )


def test_bundled_declarative_capability_loads_without_python(
    tmp_path, monkeypatch
):
    bundled = tmp_path / "plugins"
    plugin = _write_capability_plugin(bundled, name="design")
    monkeypatch.setattr("hermes_cli.plugins.get_bundled_plugins_dir", lambda: bundled)

    manager = PluginManager()
    manager.discover_and_load()

    loaded = manager._plugins["productivity/design"]
    assert loaded.enabled is True
    assert loaded.module is None
    assert loaded.skills_registered == ["design:design"]
    assert loaded.mcp_catalog_registered == [
        str((plugin / "mcp" / "design-desktop" / "manifest.yaml").resolve())
    ]
    assert manager.find_plugin_skill("design:design") == (
        plugin / "skills" / "design" / "SKILL.md"
    ).resolve()


def test_plugin_catalog_manifest_is_parsed_by_shared_catalog(
    tmp_path, monkeypatch
):
    bundled = tmp_path / "plugins"
    _write_capability_plugin(bundled, name="design")
    monkeypatch.setattr("hermes_cli.plugins.get_bundled_plugins_dir", lambda: bundled)

    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(
        "hermes_cli.mcp_catalog._plugin_catalog_manifests",
        manager.list_mcp_catalog_manifests,
    )
    empty_catalog = tmp_path / "optional-mcps"
    empty_catalog.mkdir()
    monkeypatch.setenv("HERMES_OPTIONAL_MCPS", str(empty_catalog))

    from hermes_cli.mcp_catalog import list_catalog

    entries = list_catalog()
    assert [entry.name for entry in entries] == ["design-desktop"]


def test_repository_figma_is_one_plugin_owned_capability(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    manager = PluginManager()
    monkeypatch.setattr(
        "hermes_cli.plugins.get_bundled_plugins_dir", lambda: repo_root / "plugins"
    )

    manager.discover_and_load()

    loaded = manager._plugins["productivity/figma"]
    assert loaded.enabled is True
    assert loaded.skills_registered == ["figma:figma"]
    assert len(loaded.mcp_catalog_registered) == 1
    assert not (repo_root / "skills" / "productivity" / "figma" / "SKILL.md").exists()
    assert not (
        repo_root / "optional-mcps" / "figma-desktop" / "manifest.yaml"
    ).exists()
