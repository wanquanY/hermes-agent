from __future__ import annotations

import json

import tools.doxie_skill_categories as doxie_categories
import tools.skill_manager_tool as skill_manager


def _skill_content(name: str = "reading-helper") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Helps users with reading tasks.\n"
        "---\n\n"
        "# Reading Helper\n\n"
        "Use this skill for reading workflows.\n"
    )


def _configure_doxie_runtime(monkeypatch, tmp_path, categories):
    skills_dir = tmp_path / "skills"
    monkeypatch.setenv("DOXIE_HERMES_RUNTIME_MODE", "desktop")
    monkeypatch.setattr(skill_manager, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_manager, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(skill_manager, "_security_scan_skill", lambda _path: None)
    monkeypatch.setattr(
        doxie_categories,
        "list_doxie_skill_categories",
        lambda *, force_refresh=False: list(categories),
    )
    return skills_dir


def _result(payload: str) -> dict:
    return json.loads(payload)


def test_skill_manage_create_requires_doxie_category(monkeypatch, tmp_path):
    _configure_doxie_runtime(
        monkeypatch,
        tmp_path,
        [{"slug": "reading", "name": "阅读", "sort_order": 1, "enabled": True}],
    )

    result = _result(skill_manager.skill_manage(
        action="create",
        name="reading-helper",
        content=_skill_content(),
    ))

    assert result["success"] is False
    assert "category is required in Doxie runtimes" in result["error"]


def test_skill_manage_create_rejects_category_outside_doxie_catalog(monkeypatch, tmp_path):
    _configure_doxie_runtime(
        monkeypatch,
        tmp_path,
        [{"slug": "reading", "name": "阅读", "sort_order": 1, "enabled": True}],
    )

    result = _result(skill_manager.skill_manage(
        action="create",
        name="reading-helper",
        content=_skill_content(),
        category="sales",
    ))

    assert result["success"] is False
    assert "Invalid Doxie skill category 'sales'" in result["error"]


def test_skill_manage_create_accepts_enabled_doxie_category(monkeypatch, tmp_path):
    skills_dir = _configure_doxie_runtime(
        monkeypatch,
        tmp_path,
        [
            {"slug": "reading", "name": "阅读", "sort_order": 1, "enabled": True},
            {"slug": "sales", "name": "销售", "sort_order": 2, "enabled": True},
        ],
    )

    result = _result(skill_manager.skill_manage(
        action="create",
        name="reading-helper",
        content=_skill_content(),
        category="reading",
    ))

    assert result["success"] is True
    assert result["category"] == "reading"
    assert result["path"] == "reading/reading-helper"
    assert (skills_dir / "reading" / "reading-helper" / "SKILL.md").read_text(encoding="utf-8") == _skill_content()
