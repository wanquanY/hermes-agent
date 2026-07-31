from __future__ import annotations

import zipfile
from unittest.mock import MagicMock


def test_inspect_installed_skill_reads_only_local_package(monkeypatch, tmp_path):
    home = tmp_path / "home"
    skill_dir = home / "skills" / "brand" / "frontis-vi"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: frontis-vi\n"
            "description: Brand rules\n"
            "metadata:\n"
            "  hermes:\n"
            "    tags: [brand, visual]\n"
            "---\n"
            "# Frontis\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "references" / "palette.md").write_text("# Palette\n", encoding="utf-8")
    (skill_dir / "preview.bin").write_bytes(b"\x00\xff")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", home / "skills")

    from tools.skill_package_lifecycle import inspect_installed_skill

    result = inspect_installed_skill("frontis-vi")

    assert result["name"] == "frontis-vi"
    assert result["description"] == "Brand rules"
    assert result["category"] == "brand"
    assert result["tags"] == ["brand", "visual"]
    assert [file["path"] for file in result["files"]] == [
        "SKILL.md",
        "preview.bin",
        "references/palette.md",
    ]
    assert result["files"][1]["is_binary"] is True


def test_inspect_installed_skill_does_not_fall_back_to_remote_search(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", home / "skills")

    from tools.skill_package_lifecycle import inspect_installed_skill

    try:
        inspect_installed_skill("missing-skill")
    except RuntimeError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected missing local skill to fail")


def test_inspect_installed_skill_supports_plugin_package_index(monkeypatch, tmp_path):
    skill_dir = tmp_path / "plugins" / "lark-cli" / "skills" / "lark-cli"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: lark-cli\ndescription: Lark CLI\n---\n# Lark CLI\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tools.skills_tool._find_all_skills",
        lambda *, skip_disabled: [{
            "name": "lark-cli:lark-cli",
            "description": "Lark CLI",
            "category": "plugins/lark-cli",
            "skill_dir": str(skill_dir),
            "plugin": "lark-cli",
        }] if skip_disabled else [],
    )

    from tools.skill_package_lifecycle import inspect_installed_skill

    result = inspect_installed_skill("lark-cli:lark-cli")

    assert result["source"] == "lark-cli"
    assert result["source_type"] == "plugin"
    assert result["identifier"] == "lark-cli:lark-cli"
    assert result["files"][0]["path"] == "SKILL.md"


def test_inspect_installed_skill_prefers_install_path_without_global_index(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "home"
    skill_dir = home / "skills" / "brand" / "frontis-vi"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: frontis-vi\ndescription: Brand rules\n---\n# Frontis\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", home / "skills")
    global_index = MagicMock(side_effect=AssertionError("global index should not run"))
    monkeypatch.setattr("tools.skills_tool._find_all_skills", global_index)

    from tools.skill_package_lifecycle import inspect_installed_skill

    result = inspect_installed_skill("frontis-vi", "brand/frontis-vi")

    assert result["category"] == "brand"
    assert result["skill_dir"] == str(skill_dir)
    global_index.assert_not_called()


def test_import_skill_archive_to_home_installs_single_skill(monkeypatch, tmp_path):
    home = tmp_path / "home"
    archive = tmp_path / "demo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("demo-skill/SKILL.md", "---\nname: demo-skill\n---\n# Demo\n")
        zf.writestr("demo-skill/scripts/run.sh", "echo ok\n")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skill_manager_tool._security_scan_skill", lambda _path: None)
    monkeypatch.setattr("tools.skills_hub.append_audit_log", lambda *args, **kwargs: None)

    from tools.skill_package_lifecycle import import_skill_archive_to_home

    result = import_skill_archive_to_home(archive, category="productivity")

    target = home / "skills" / "productivity" / "demo-skill"
    assert result["name"] == "demo-skill"
    assert result["installPath"] == "productivity/demo-skill"
    assert (target / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (target / "scripts" / "run.sh").read_text(encoding="utf-8") == "echo ok\n"


def test_import_skill_archive_rejects_multiple_skills(monkeypatch, tmp_path):
    archive = tmp_path / "multi.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("one/SKILL.md", "# One\n")
        zf.writestr("two/SKILL.md", "# Two\n")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    from tools.skill_package_lifecycle import import_skill_archive_to_home

    try:
        import_skill_archive_to_home(archive)
    except RuntimeError as exc:
        assert "multiple skills" in str(exc)
    else:
        raise AssertionError("expected multiple-skill archive to be rejected")
