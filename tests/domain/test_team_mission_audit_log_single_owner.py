"""Team Mission audit projection physical access guard.

``team_mission_events`` is a legacy audit/read-model projection, not a second
canonical event ledger. Runtime code must access it through
``TeamMissionAuditLog`` so Phase E can later replace the backing projection
without chasing raw SQL across gateway/runtime modules.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_TEAM_MISSION_EVENTS_SQL_RE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+(?:IGNORE|REPLACE|ROLLBACK|ABORT|FAIL))?\s+INTO"
    r"|UPDATE"
    r"|DELETE\s+FROM"
    r"|FROM)"
    r"\s+team_mission_events\b",
    re.IGNORECASE,
)

_OWNERS = {
    "hermes_agent/application/team_mission_audit_log.py",
}

_MIGRATION_OR_SCHEMA_ROOTS = {
    "hermes_agent/composition/migrations",
}

_SCHEMA_MAINTENANCE_FILES = {
    "hermes_team_mission/state/schema.py",
}

_EXCLUDED_DIRS = {"tests", "__pycache__", ".venv", ".import_linter_cache"}


def _iter_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & _EXCLUDED_DIRS:
            continue
        yield path


def _string_literals(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            literals.append(node.value)
    return literals


def _is_allowed(rel: str) -> bool:
    if rel in _OWNERS or rel in _SCHEMA_MAINTENANCE_FILES:
        return True
    return any(rel.startswith(root + "/") for root in _MIGRATION_OR_SCHEMA_ROOTS)


def test_team_mission_events_physical_access_is_centralized():
    offenders: list[tuple[str, str]] = []
    for path in _iter_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if _is_allowed(rel):
            continue
        for literal in _string_literals(path):
            if _TEAM_MISSION_EVENTS_SQL_RE.search(literal):
                offenders.append((rel, literal.strip().replace("\n", " ")[:120]))
                break
    if offenders:
        formatted = "\n".join(f"  {rel}\n    {snippet!r}" for rel, snippet in offenders)
        raise AssertionError(
            "team_mission_events physical SQL must route through TeamMissionAuditLog:\n"
            + formatted
        )
