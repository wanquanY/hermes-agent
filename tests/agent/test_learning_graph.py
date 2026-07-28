from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent.learning_graph import LearningGraphService, SkillNode, build_edges, density_stats


def _seed_home(home: Path, *, memory: str, skill: str) -> None:
    memories = home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(memory, encoding="utf-8")
    skill_dir = home / "skills" / skill
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ncategory: dev\n---\n# {skill}\n",
        encoding="utf-8",
    )
    (home / "skills" / ".usage.json").write_text(
        json.dumps(
            {
                skill: {
                    "created_by": "agent",
                    "created_at": "2026-07-01T12:00:00+00:00",
                    "use_count": 1,
                }
            }
        ),
        encoding="utf-8",
    )


def test_edges_only_connect_existing_nodes():
    nodes = {
        "a": SkillNode("a", "x", related=["b", "ghost"]),
        "b": SkillNode("b", "x", related=["a"]),
        "c": SkillNode("c", "y"),
    }
    assert build_edges(nodes) == [("a", "b")]
    assert density_stats(nodes, build_edges(nodes))["linked_nodes"] == 2


def test_graph_has_typed_stable_nodes_and_integral_edges(tmp_path: Path):
    home = tmp_path / "profile"
    _seed_home(home, memory="Use pytest for hermes-debug", skill="hermes-debug")

    graph = LearningGraphService(home).build()

    ids = {node["id"] for node in graph["nodes"]}
    assert any(node_id.startswith("memory:") and ":memory:" in node_id for node_id in ids)
    assert any(node_id.endswith(":hermes-debug") for node_id in ids)
    assert all(edge["source"] in ids and edge["target"] in ids for edge in graph["edges"])
    assert [item["nodeId"] for item in graph["timeline"]] == [
        node["id"]
        for node in sorted(
            graph["nodes"],
            key=lambda node: (
                int(node.get("timestamp") or 0),
                0 if node.get("kind") == "memory" else 1,
                node["id"],
            ),
        )
    ]
    assert graph["stats"]["memory_nodes"] == 1
    assert graph["stats"]["learned_skills"] == 1


def test_memory_node_id_survives_unrelated_insertion(tmp_path: Path):
    home = tmp_path / "profile"
    _seed_home(home, memory="alpha\n§\nbeta", skill="learned")
    first = LearningGraphService(home).build()
    beta_id = next(card["id"] for card in first["memory"] if card["body"] == "beta")

    (home / "memories" / "MEMORY.md").write_text(
        "new entry\n§\nalpha\n§\nbeta", encoding="utf-8"
    )
    second = LearningGraphService(home).build()

    assert beta_id == next(card["id"] for card in second["memory"] if card["body"] == "beta")


def test_malformed_frontmatter_metadata_does_not_crash(tmp_path: Path):
    home = tmp_path / "profile"
    _seed_home(home, memory="note", skill="bad-skill")
    (home / "skills" / "bad-skill" / "SKILL.md").write_text(
        '---\nname: bad-skill\nmetadata: not-a-dict\ndescription: "oops\n---\n',
        encoding="utf-8",
    )

    graph = LearningGraphService(home).build()

    assert any(node["id"].endswith(":bad-skill") for node in graph["nodes"])


def test_profile_homes_are_isolated_under_concurrent_builds(tmp_path: Path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    _seed_home(home_a, memory="only alpha", skill="alpha-skill")
    _seed_home(home_b, memory="only beta", skill="beta-skill")

    with ThreadPoolExecutor(max_workers=2) as pool:
        graph_a, graph_b = list(
            pool.map(lambda home: LearningGraphService(home).build(), [home_a, home_b])
        )

    ids_a = {node["id"] for node in graph_a["nodes"]}
    ids_b = {node["id"] for node in graph_b["nodes"]}
    assert any(value.endswith(":alpha-skill") for value in ids_a)
    assert not any(value.endswith(":beta-skill") for value in ids_a)
    assert any(value.endswith(":beta-skill") for value in ids_b)
    assert not any(value.endswith(":alpha-skill") for value in ids_b)
