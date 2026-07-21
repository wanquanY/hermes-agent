import ast
from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]


def _setup_data_tree_roots():
    tree = ast.parse((REPO_ROOT / "setup.py").read_text(encoding="utf-8"))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_data_file_tree"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def test_faster_whisper_is_not_a_base_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]

    assert not any(dep.startswith("faster-whisper") for dep in deps)

    voice_extra = data["project"]["optional-dependencies"]["voice"]
    assert any(dep.startswith("faster-whisper") for dep in voice_extra)


def test_manifest_includes_bundled_skills():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "graft skills" in manifest
    assert "graft optional-skills" in manifest


def test_mcp_catalog_is_in_sdist_and_every_entry_is_in_wheel():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "graft optional-mcps" in manifest
    assert "optional-mcps" in _setup_data_tree_roots()

    catalog_root = REPO_ROOT / "optional-mcps"
    assert list(catalog_root.glob("*/manifest.yaml")), (
        "the curated MCP catalog must not be empty"
    )


def test_plugin_capability_assets_are_in_sdist_and_wheel():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include plugins" in manifest
    assert "SKILL.md" in manifest
    assert "manifest.yaml" in manifest

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin_data = data["tool"]["setuptools"]["package-data"]["plugins"]
    assert "**/plugin.yaml" in plugin_data
    assert "**/SKILL.md" in plugin_data
    assert "**/manifest.yaml" in plugin_data

    figma = REPO_ROOT / "plugins/productivity/figma"
    assert (figma / "plugin.yaml").is_file()
    assert (figma / "skills/figma/SKILL.md").is_file()
    assert (figma / "mcp/figma-desktop/manifest.yaml").is_file()
