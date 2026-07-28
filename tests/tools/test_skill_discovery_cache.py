"""Regression tests for profile/plugin-safe Skill discovery caching."""

import os
from unittest.mock import patch


def _write_skill(path, description: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: {description}\n---\nBody.\n",
        encoding="utf-8",
    )


def test_unchanged_skill_metadata_is_parsed_once(tmp_path, monkeypatch):
    from tools import skills_tool

    _write_skill(tmp_path / "demo", "first")
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)

    with (
        patch("agent.skill_utils.get_external_skills_dirs", return_value=[]),
        patch("hermes_cli.plugins.discover_plugins"),
        patch(
            "hermes_cli.plugins.get_plugin_manager"
        ) as get_plugin_manager,
        patch.object(
            skills_tool,
            "_parse_frontmatter",
            wraps=skills_tool._parse_frontmatter,
        ) as parse,
    ):
        get_plugin_manager.return_value.list_plugin_skill_entries.return_value = []
        first = skills_tool._find_all_skills()
        second = skills_tool._find_all_skills()

    assert first == second
    assert parse.call_count == 1


def test_editing_existing_skill_invalidates_metadata_cache(tmp_path, monkeypatch):
    from tools import skills_tool

    skill_dir = tmp_path / "demo"
    _write_skill(skill_dir, "first")
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)

    with (
        patch("agent.skill_utils.get_external_skills_dirs", return_value=[]),
        patch("hermes_cli.plugins.discover_plugins"),
        patch(
            "hermes_cli.plugins.get_plugin_manager"
        ) as get_plugin_manager,
    ):
        get_plugin_manager.return_value.list_plugin_skill_entries.return_value = []
        first = skills_tool._find_all_skills()

        skill_file = skill_dir / "SKILL.md"
        _write_skill(skill_dir, "second description")
        stat = skill_file.stat()
        os.utime(skill_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

        second = skills_tool._find_all_skills()

    assert first[0]["description"] == "first"
    assert second[0]["description"] == "second description"


def test_cached_results_are_not_mutable_aliases(tmp_path, monkeypatch):
    from tools import skills_tool

    _write_skill(tmp_path / "demo", "stable")
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)

    with (
        patch("agent.skill_utils.get_external_skills_dirs", return_value=[]),
        patch("hermes_cli.plugins.discover_plugins"),
        patch(
            "hermes_cli.plugins.get_plugin_manager"
        ) as get_plugin_manager,
    ):
        get_plugin_manager.return_value.list_plugin_skill_entries.return_value = []
        first = skills_tool._find_all_skills()
        first[0]["description"] = "mutated"
        second = skills_tool._find_all_skills()

    assert second[0]["description"] == "stable"
