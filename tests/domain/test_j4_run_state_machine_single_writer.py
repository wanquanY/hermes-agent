"""spec §J4 — RunStateMachine single terminal writer (whole-repo reality check).

Only ``hermes_agent/domain/run_terminator.py`` may issue ``UPDATE runs``
statements that mutate terminal columns. ``RunRepoImpl`` owns non-terminal
``runs`` materialized-view fields such as metadata and last_seq.

Audit 2026-07-07 (docs/v3_audit_report.md §四 伪绿 2) identified **6
direct ``UPDATE runs`` bypasses** in legacy ``hermes_state_runs.py`` plus
a silent fallback in ``tui_gateway/services/run_control.py``.

The whole-repo scan keeps an explicit inventory of authorised writers.
New writers fail immediately.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


_UPDATE_RUNS_RE = re.compile(
    r"\bUPDATE\s+runs\b",
    re.IGNORECASE,
)
_TERMINAL_COLUMN_RE = re.compile(
    r"\b(?:terminal_seq|terminal_degraded|terminal_cause)\b",
    re.IGNORECASE,
)


# The canonical writers. `run_terminator.py` owns the terminal transitions;
# migrations legitimately reshape the `runs` schema.
_TERMINATOR_WRITERS = {
    "hermes_agent/domain/run_terminator.py",
}
_RUN_MATERIALIZED_VIEW_WRITERS = {
    "hermes_agent/repositories/run_repo.py",
}
_MIGRATION_ROOTS = {"hermes_agent/storage/migrations"}
# ``scripts/`` is CI/audit tooling (``scripts/zero_debt/verdict.py`` is
# itself a shadow-writer scanner containing the pattern strings) — not
# production runtime.
_EXCLUDED_DIRS = {"tests", "__pycache__", ".venv", ".import_linter_cache", "scripts"}


def _iter_whole_repo():
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & _EXCLUDED_DIRS:
            continue
        yield path


def _iter_v3_tree():
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


def _find_shadow_writers(files) -> list[tuple[str, str]]:
    offenders: list[tuple[str, str]] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _TERMINATOR_WRITERS:
            continue
        if rel in _RUN_MATERIALIZED_VIEW_WRITERS:
            continue
        if any(rel.startswith(root + "/") for root in _MIGRATION_ROOTS):
            continue
        for literal in _string_literals(path):
            if _UPDATE_RUNS_RE.search(literal):
                snippet = literal.strip().replace("\n", " ")[:120]
                offenders.append((rel, snippet))
                break
    return offenders


# ---------------------------------------------------------------------------


def test_j4_run_terminator_writer_exists():
    """Sanity — run_terminator.py must contain the canonical UPDATE runs."""
    src = (
        REPO_ROOT / "hermes_agent" / "domain" / "run_terminator.py"
    ).read_text(encoding="utf-8")
    assert _UPDATE_RUNS_RE.search(src), (
        "run_terminator.py has no UPDATE runs — canonical writer lost"
    )


def test_j4_v3_tree_is_clean():
    """spec §J4 within v3 tree only — every ``UPDATE runs`` outside
    ``run_terminator.py`` / ``RunRepoImpl`` + migrations is a J4 violation
    inside v3.
    """
    offenders = _find_shadow_writers(_iter_v3_tree())
    if offenders:
        formatted = "\n".join(f"  {rel}\n    {snippet!r}" for rel, snippet in offenders)
        raise AssertionError(
            "spec §J4 violated inside v3 tree — non-terminator writers:\n"
            + formatted
        )


def test_j4_terminal_columns_only_written_by_run_terminator():
    offenders: list[tuple[str, str]] = []
    for path in _iter_whole_repo():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _TERMINATOR_WRITERS:
            continue
        if any(rel.startswith(root + "/") for root in _MIGRATION_ROOTS):
            continue
        for literal in _string_literals(path):
            if _UPDATE_RUNS_RE.search(literal) and _TERMINAL_COLUMN_RE.search(literal):
                snippet = literal.strip().replace("\n", " ")[:120]
                offenders.append((rel, snippet))
                break
    assert not offenders, (
        "terminal runs columns must only be written by run_terminator.py:\n"
        + "\n".join(f"  {rel}\n    {snippet!r}" for rel, snippet in offenders)
    )


_AUTHORIZED_RUNS_WRITERS: set[str] = set()


def test_j4_whole_repo_shadow_writer_inventory_is_explicit():
    """spec §J4 REALITY CHECK — whole-repo scan for `UPDATE runs`.

    There should be no legacy writer left here. The remaining authorised
    writer is RunRepoImpl, which owns non-terminal materialized-view fields.
    """
    offenders = _find_shadow_writers(_iter_whole_repo())
    offender_paths = {rel for rel, _snippet in offenders}
    assert offender_paths == _AUTHORIZED_RUNS_WRITERS
