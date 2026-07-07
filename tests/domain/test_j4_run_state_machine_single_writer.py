"""spec §J4 — RunStateMachine single terminal writer (whole-repo reality check).

Only ``hermes_agent/domain/run_terminator.py`` may issue ``UPDATE runs``
statements that mutate ``status`` or terminal columns. Everything else
routes through ``terminate_run``.

Audit 2026-07-07 (docs/v3_audit_report.md §四 伪绿 2) identified **6
direct ``UPDATE runs`` bypasses** in legacy ``hermes_state_runs.py`` plus
a silent fallback in ``tui_gateway/services/run_control.py``.

Same xfail pattern as ``test_j3_event_ledger_single_writer.py``: whole
repo scan; xfail is the "reality signal" until Phase D switch collapses
the legacy row writers into ``terminate_run`` (spec §12 Phase D).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


_UPDATE_RUNS_RE = re.compile(
    r"\bUPDATE\s+runs\b",
    re.IGNORECASE,
)


# The canonical writers. `run_terminator.py` owns the terminal transitions;
# migrations legitimately reshape the `runs` schema.
_TERMINATOR_WRITERS = {
    "hermes_agent/domain/run_terminator.py",
}
_MIGRATION_ROOTS = {"hermes_agent/storage/migrations"}
_EXCLUDED_DIRS = {"tests", "__pycache__", ".venv", ".import_linter_cache"}


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
    ``run_terminator.py`` + migrations is a J4 violation inside v3.
    """
    offenders = _find_shadow_writers(_iter_v3_tree())
    if offenders:
        formatted = "\n".join(f"  {rel}\n    {snippet!r}" for rel, snippet in offenders)
        raise AssertionError(
            "spec §J4 violated inside v3 tree — non-terminator writers:\n"
            + formatted
        )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "spec §12 Phase D switch not yet landed — legacy hermes_state_runs.py "
        "and tui_gateway/services/run_control.py still write runs.status "
        "directly. This xfail flips to xpassed when Phase D collapses the "
        "legacy row writers into terminate_run (see docs/v3_audit_report.md "
        "§四 伪绿 2)."
    ),
)
def test_j4_whole_repo_shadow_writers_gone():
    """spec §J4 REALITY CHECK — whole-repo scan for `UPDATE runs`.

    Failing here is the truth: legacy code still bypasses run_terminator's
    single-entry invariant. Fix by collapsing legacy writers, not by
    narrowing the scan.
    """
    offenders = _find_shadow_writers(_iter_whole_repo())
    if offenders:
        formatted = "\n".join(f"  {rel}\n    {snippet!r}" for rel, snippet in offenders)
        raise AssertionError(
            f"spec §J4 whole-repo shadow writers still present ({len(offenders)} files):\n"
            + formatted
        )
