from __future__ import annotations

import json
from pathlib import Path

from agent.learning_graph import LearningGraphService
from agent.learning_mutations import LearningMutationService
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_SKILL = """---
name: my-skill
description: A test skill.
---

# My Skill

Body.
"""


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "profile"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text(
        "alpha note\nline two\n§\nbeta note", encoding="utf-8"
    )
    (home / "memories" / "USER.md").write_text("profile note", encoding="utf-8")
    skill = home / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    (home / "skills" / ".usage.json").write_text(
        json.dumps({"my-skill": {"created_by": "agent", "use_count": 1}}),
        encoding="utf-8",
    )
    return home


def _node_id(home: Path, title: str) -> str:
    graph = LearningGraphService(home).build()
    return next(card["id"] for card in graph["memory"] if card["title"] == title)


def test_canonical_memory_detail_edit_delete(tmp_path: Path):
    home = _home(tmp_path)
    service = LearningMutationService(home)
    alpha_id = _node_id(home, "alpha note")

    assert service.detail(alpha_id)["content"].startswith("alpha note")
    edited = service.edit(alpha_id, "alpha rewritten")
    assert edited["ok"]
    assert "alpha rewritten" in (home / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert not service.detail(alpha_id)["ok"]

    rewritten_id = _node_id(home, "alpha rewritten")
    assert service.delete(rewritten_id)["ok"]
    assert "alpha rewritten" not in (home / "memories" / "MEMORY.md").read_text(encoding="utf-8")


def test_legacy_memory_index_is_read_compatible(tmp_path: Path):
    home = _home(tmp_path)
    service = LearningMutationService(home)

    assert service.detail("memory:memory:1")["content"] == "beta note"
    assert service.detail("memory:profile:2")["content"] == "profile note"


def test_unrelated_insertion_does_not_stale_canonical_id(tmp_path: Path):
    home = _home(tmp_path)
    service = LearningMutationService(home)
    beta_id = _node_id(home, "beta note")
    path = home / "memories" / "MEMORY.md"
    path.write_text("new\n§\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    assert service.detail(beta_id)["content"] == "beta note"


def test_memory_write_matches_memory_store_format(tmp_path: Path):
    from tools.memory_tool import ENTRY_DELIMITER, MemoryStore

    home = _home(tmp_path)
    node_id = _node_id(home, "alpha note")
    assert LearningMutationService(home).edit(node_id, "alpha rewritten")["ok"]
    path = home / "memories" / "MEMORY.md"
    entries = MemoryStore._read_file(path)
    assert path.read_text(encoding="utf-8") == ENTRY_DELIMITER.join(entries)


def test_duplicate_memory_occurrence_is_mutated_without_collapsing_siblings(
    tmp_path: Path,
):
    home = _home(tmp_path)
    path = home / "memories" / "MEMORY.md"
    path.write_text("same\n§\nsame\n§\nother", encoding="utf-8")
    graph = LearningGraphService(home).build()
    duplicate_ids = [card["id"] for card in graph["memory"] if card["body"] == "same"]

    result = LearningMutationService(home).edit(duplicate_ids[1], "second changed")

    assert result["ok"]
    assert path.read_text(encoding="utf-8") == "same\n§\nsecond changed\n§\nother"


def test_skill_edit_archive_and_pinned_guard(tmp_path: Path):
    home = _home(tmp_path)
    service = LearningMutationService(home)
    good = _SKILL.replace("A test skill.", "Updated description.")
    skill_id = next(
        node["id"]
        for node in LearningGraphService(home).build()["nodes"]
        if node.get("entityId") == "my-skill"
    )

    assert service.detail(skill_id)["ok"]
    assert service.edit(skill_id, good)["ok"]
    assert "Updated description." in (home / "skills" / "my-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    token = set_hermes_home_override(home)
    try:
        from tools import skill_usage

        skill_usage.set_pinned("my-skill", True)
    finally:
        reset_hermes_home_override(token)
    assert not service.delete(skill_id)["ok"]

    token = set_hermes_home_override(home)
    try:
        skill_usage.set_pinned("my-skill", False)
    finally:
        reset_hermes_home_override(token)
    assert service.delete(skill_id)["ok"]
    assert (home / "skills" / ".archive" / "my-skill" / "SKILL.md").exists()


def test_cross_profile_mutation_is_rejected(tmp_path: Path):
    home_a = _home(tmp_path / "a")
    home_b = _home(tmp_path / "b")
    alpha_id = _node_id(home_a, "alpha note")
    before = (home_a / "memories" / "MEMORY.md").read_text(encoding="utf-8")

    result = LearningMutationService(home_b).edit(alpha_id, "wrong profile")

    assert not result["ok"]
    assert (home_a / "memories" / "MEMORY.md").read_text(encoding="utf-8") == before
