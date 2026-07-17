from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_cli import journey


def test_journey_json_uses_canonical_graph(monkeypatch, capsys, tmp_path: Path):
    payload = {
        "nodes": [{"id": "memory:scope:memory:rev:0", "kind": "memory"}],
        "edges": [],
        "timeline": [],
        "stats": {"memory_nodes": 1, "learned_skills": 0},
    }
    monkeypatch.setattr(journey, "_payload", lambda: payload)

    assert journey._show(argparse.Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_journey_list_prints_node_ids(monkeypatch, capsys):
    monkeypatch.setattr(
        journey,
        "_payload",
        lambda: {
            "nodes": [
                {
                    "id": "skill:scope:debug",
                    "kind": "skill",
                    "label": "debug",
                    "timestamp": 1,
                }
            ]
        },
    )

    assert journey._list(argparse.Namespace(json=False)) == 0
    assert "skill:scope:debug" in capsys.readouterr().out


def test_journey_parser_registers_mutations():
    parser = argparse.ArgumentParser()
    journey.register_cli(parser)

    assert parser.parse_args(["--json"]).func is journey._show
    assert parser.parse_args(["list"]).func is journey._list
    assert parser.parse_args(["edit", "node", "--content", "new"]).func is journey._edit
    assert parser.parse_args(["delete", "node", "--yes"]).func is journey._delete
