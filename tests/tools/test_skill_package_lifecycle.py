from __future__ import annotations

import zipfile


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
