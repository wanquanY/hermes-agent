"""spec §J3 — single EventLedger append entry.

Only ``hermes_agent/domain/event_ledger.py`` may issue ``INSERT / UPDATE /
DELETE`` statements against ``run_events``. Migrations are allowed to
touch structural DDL (``ALTER TABLE``, ``CREATE INDEX``). Everything
else routes through ``EventLedger.append``.

If someone later adds a raw ``INSERT INTO run_events (...)`` outside the
ledger, this test flags it — spec §6.1 says run_events is the one ledger.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
V3_PKG = REPO_ROOT / "hermes_agent"


_MUTATION_RE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+(?:IGNORE|REPLACE|ROLLBACK|ABORT|FAIL))?\s+INTO"
    r"|UPDATE"
    r"|DELETE\s+FROM)"
    r"\s+run_events\b",
    re.IGNORECASE,
)


# Allowed writers (relative to REPO_ROOT).
_ALLOWED = {
    "hermes_agent/domain/event_ledger.py",
    # Migrations legitimately shape the schema; they use ALTER TABLE / CREATE
    # INDEX / DROP COLUMN which are structural, not row-mutation. But some
    # backfill migrations do issue INSERT/UPDATE — those are one-off schema
    # movements, not runtime writes, so we scope them here.
    "hermes_agent/storage/migrations/0042_interaction_events_persist.py",
    "hermes_agent/storage/migrations/0028_run_events_participant_id.py",
    "hermes_agent/storage/migrations/0034_tool_events_backfill.py",
}


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _string_literals(path: Path) -> list[str]:
    """Extract every non-docstring string literal from ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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


def test_j3_only_event_ledger_writes_run_events():
    offenders: list[tuple[str, str]] = []
    for path in _iter_python_files(V3_PKG):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        for literal in _string_literals(path):
            if _MUTATION_RE.search(literal):
                snippet = literal.strip().replace("\n", " ")[:120]
                offenders.append((rel, snippet))
    if offenders:
        formatted = "\n".join(
            f"  {rel}\n    {snippet!r}" for rel, snippet in offenders
        )
        raise AssertionError(
            "spec §J3 violated — non-ledger code writes run_events:\n"
            + formatted
        )


def test_j3_allowed_list_stays_narrow():
    """Sentinel — any future addition to ``_ALLOWED`` requires a spec §J3
    review. This assertion prints the current list so the reviewer can
    see growth over time.
    """
    # Current v3 ledger + 3 migrations. Fail if the list ever grows past 5.
    assert len(_ALLOWED) <= 5, (
        f"_ALLOWED grew past 5 entries — was a raw run_events writer added "
        f"outside the ledger? Current: {sorted(_ALLOWED)}"
    )


def test_j3_event_ledger_writer_exists():
    """Sanity — event_ledger.py must contain the canonical INSERT."""
    src = (V3_PKG / "domain" / "event_ledger.py").read_text(encoding="utf-8")
    assert _MUTATION_RE.search(src), (
        "event_ledger.py has no INSERT INTO run_events — did the ledger "
        "lose its canonical writer?"
    )
