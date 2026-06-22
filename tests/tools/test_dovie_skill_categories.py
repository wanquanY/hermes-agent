from __future__ import annotations

import json
import threading

import tools.dovie_skill_categories as dovie_categories
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


def _configure_dovie_runtime(monkeypatch, tmp_path, categories):
    skills_dir = tmp_path / "skills"
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_MODE", "desktop")
    monkeypatch.setattr(skill_manager, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_manager, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(skill_manager, "_security_scan_skill", lambda _path: None)
    monkeypatch.setattr(
        dovie_categories,
        "list_dovie_skill_categories",
        lambda *, force_refresh=False: list(categories),
    )
    return skills_dir


def _result(payload: str) -> dict:
    return json.loads(payload)


def test_skill_manage_create_requires_dovie_category(monkeypatch, tmp_path):
    _configure_dovie_runtime(
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
    assert "category is required in Dovie runtimes" in result["error"]


def test_skill_manage_create_rejects_category_outside_dovie_catalog(monkeypatch, tmp_path):
    _configure_dovie_runtime(
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
    assert "Invalid Dovie skill category 'sales'" in result["error"]


def test_skill_manage_create_accepts_enabled_dovie_category(monkeypatch, tmp_path):
    skills_dir = _configure_dovie_runtime(
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


def test_fetch_categories_returns_on_interrupt(monkeypatch):
    monkeypatch.setenv("DOVIE_SKILL_CATEGORIES_URL", "https://dovie.example/categories")
    monkeypatch.setenv("DOVIE_LLM_RUNTIME_TOKEN", "runtime-token")
    started = threading.Event()
    release = threading.Event()
    result_holder = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"data": []}'

    def fake_urlopen(_request, *, timeout):
        started.set()
        release.wait(timeout=5)
        return FakeResponse()

    monkeypatch.setattr(dovie_categories.urllib.request, "urlopen", fake_urlopen)

    def run_fetch():
        try:
            dovie_categories._fetch_categories()
        except InterruptedError as exc:
            result_holder["error"] = str(exc)

    worker = threading.Thread(target=run_fetch)
    worker.start()
    assert started.wait(timeout=1)

    from tools.interrupt import set_interrupt

    set_interrupt(True, thread_id=worker.ident)
    worker.join(timeout=1)
    release.set()
    set_interrupt(False, thread_id=worker.ident)

    assert not worker.is_alive()
    assert result_holder["error"] == "Dovie skill category request interrupted."
