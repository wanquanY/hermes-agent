"""spec §J3 — single EventLedger append entry (whole-repo reality check).

Only ``hermes_agent/domain/event_ledger.py`` may issue ``INSERT / UPDATE /
DELETE`` statements against ``run_events``. Everything else routes through
``EventLedger``.

**Prior version scanned only ``hermes_agent/`` — that scope was too narrow
and produced a false green (audit 2026-07-07, docs/v3_audit_report.md
§四 伪绿 1).** The production writers live at repo root
(``hermes_state_runs.py:1676`` and ``:2704``), outside the ``hermes_agent/``
tree. This file now scans the WHOLE repo and requires zero non-ledger
writers. New physical mutations must be added as EventLedger methods and
covered by domain tests.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


_MUTATION_RE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+(?:IGNORE|REPLACE|ROLLBACK|ABORT|FAIL))?\s+INTO"
    r"|UPDATE"
    r"|DELETE\s+FROM)"
    r"\s+run_events\b",
    re.IGNORECASE,
)


# Legitimate writers — the canonical ledger + structural / one-off backfill
# migrations. Paths relative to REPO_ROOT.
_LEDGER_WRITERS = {
    "hermes_agent/domain/event_ledger.py",
}

# Migrations legitimately shape the schema and may need to backfill rows;
# every migration file is one-off, not runtime.
_MIGRATION_ROOTS = {"hermes_agent/storage/migrations"}

# Test files never count.
_EXCLUDED_DIRS = {"tests", "__pycache__", ".venv", ".import_linter_cache"}


def _iter_python_files_whole_repo():
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & _EXCLUDED_DIRS:
            continue
        yield path


def _iter_python_files_v3():
    for path in (REPO_ROOT / "hermes_agent").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _string_literals(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
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


def _is_migration(rel: str) -> bool:
    return any(rel.startswith(root + "/") for root in _MIGRATION_ROOTS)


def _find_shadow_writers(files) -> list[tuple[str, str]]:
    offenders: list[tuple[str, str]] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _LEDGER_WRITERS:
            continue
        if _is_migration(rel):
            continue
        for literal in _string_literals(path):
            if _MUTATION_RE.search(literal):
                snippet = literal.strip().replace("\n", " ")[:120]
                offenders.append((rel, snippet))
                break  # one hit per file is enough for the report
    return offenders


# ---------------------------------------------------------------------------


def test_j3_event_ledger_writer_exists():
    """Sanity — event_ledger.py must contain the canonical INSERT."""
    src = (REPO_ROOT / "hermes_agent" / "domain" / "event_ledger.py").read_text(encoding="utf-8")
    assert _MUTATION_RE.search(src), (
        "event_ledger.py has no INSERT INTO run_events — canonical writer lost"
    )


def test_j3_v3_tree_is_clean():
    """spec §J3 within the v3 tree (``hermes_agent/`` only) — every
    ``INSERT/UPDATE/DELETE run_events`` outside the ledger + migrations is
    a J3 violation inside v3.
    """
    offenders = _find_shadow_writers(_iter_python_files_v3())
    if offenders:
        formatted = "\n".join(f"  {rel}\n    {snippet!r}" for rel, snippet in offenders)
        raise AssertionError(
            "spec §J3 violated inside v3 tree — non-ledger writers:\n" + formatted
        )


def test_j3_whole_repo_has_no_shadow_run_event_writers():
    """spec §J3 — whole-repo scan.

    ``run_events`` is the canonical ledger. Outside EventLedger and
    migrations, code may decide what to append/update/delete, but the
    physical mutation must route through EventLedger.
    """
    offenders = _find_shadow_writers(_iter_python_files_whole_repo())
    if offenders:
        formatted = "\n".join(f"  {rel}\n    {snippet!r}" for rel, snippet in offenders)
        raise AssertionError("spec §J3 violated — non-ledger writers:\n" + formatted)
