#!/usr/bin/env python3
"""Whole-tree aggregate table writer scanner for Hermes zero-debt gates."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
V3_PKG = REPO_ROOT / "hermes_agent"
MIGRATION_PREFIX = "hermes_agent/storage/migrations/"


# Aggregate table -> files authorized to physically write it.
TABLE_OWNERS: dict[str, set[str]] = {
    "sessions": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    "session_index": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    "session_branches": {"hermes_agent/repositories/session_repo.py"},
    "session_handoffs": {"hermes_agent/repositories/session_repo.py"},
    "session_lineage": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    "session_branch_requests": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    "messages": {"hermes_agent/repositories/message_repo.py"},
    "runs": {
        "hermes_agent/domain/run_terminator.py",
        "hermes_agent/repositories/run_repo.py",
    },
    "run_events": {"hermes_agent/domain/event_ledger.py"},
    "seq_counter": {"hermes_agent/domain/seq_allocator.py"},
    "agent_profiles": {"hermes_agent/repositories/agent_profile_repo.py"},
    "agent_profile_versions": {"hermes_agent/repositories/agent_profile_repo.py"},
    "agent_profile_growth_summary": {
        "hermes_agent/repositories/agent_profile_repo.py"
    },
    "team_missions": {"hermes_agent/repositories/team_mission_repo.py"},
    "team_mission_nodes": {"hermes_agent/repositories/team_mission_repo.py"},
    "team_mission_edges": {"hermes_agent/repositories/team_mission_repo.py"},
    "team_mission_run_bindings": {"hermes_agent/repositories/team_mission_repo.py"},
    "activities": {"hermes_agent/repositories/team_mission_repo.py"},
    "activity_commands": {"hermes_agent/repositories/team_mission_repo.py"},
}


def write_re(table: str) -> re.Pattern[str]:
    return re.compile(
        r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+"
        + table
        + r"\b",
        re.IGNORECASE,
    )


def iter_v3_files() -> Iterable[Path]:
    for path in V3_PKG.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def string_literals(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            out.append(node.value)
    return out


def find_shadow_table_writers() -> list[tuple[str, str, str]]:
    compiled = {table: write_re(table) for table in TABLE_OWNERS}
    offenders: list[tuple[str, str, str]] = []
    for path in iter_v3_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(MIGRATION_PREFIX):
            continue
        literals = string_literals(path)
        for table, owners in TABLE_OWNERS.items():
            if rel in owners:
                continue
            pattern = compiled[table]
            for literal in literals:
                if pattern.search(literal):
                    snippet = literal.strip().replace("\n", " ")[:90]
                    offenders.append((rel, table, snippet))
                    break
    return offenders


def format_shadow_table_writers(
    offenders: list[tuple[str, str, str]],
    *,
    limit: int | None = None,
) -> str:
    by_file: dict[str, list[str]] = {}
    for rel, table, snippet in offenders:
        by_file.setdefault(rel, []).append(f"{table}: {snippet!r}")
    lines: list[str] = []
    for rel, rows in by_file.items():
        lines.append(f"  {rel}")
        lines.extend(f"    {row}" for row in rows)
        if limit is not None and len(lines) >= limit:
            lines.append("  ...")
            break
    return "\n".join(lines)
