"""spec §J11 + §I —— 静默降级 (silent fallback) 全仓扫描.

Silent fallback is a subtle variant of silent-swallow: a caller wraps a
new-path call in ``try / except`` and, on any exception, silently
reassigns to ``None`` or ``False`` and continues down the legacy path.
This defeats spec §J4 (RunStateMachine single entry) and hides real
production failures.

Audit 2026-07-07 (docs/v3_audit_report.md §四 伪绿 2, 优先级 1 #3) called
out ``tui_gateway/services/run_control.py:2216, :2224`` exactly this
pattern for ``terminate_run``.

This scan looks for the anti-pattern::

    except <anything>:
        <var> = None       # or = False
        # NO log, NO re-raise, NO taxonomy classification

If found outside ``hermes_agent/`` (the v3 tree already passes
silent_swallow_lint), the caller is falling back to legacy silently.

Marked ``xfail(strict=False)`` — failing IS the truth signal. When
Phase D switch retires the legacy path, the fallback disappears and
xfail flips to xpassed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_EXCLUDED_DIRS = {
    "tests",
    "__pycache__",
    ".venv",
    ".import_linter_cache",
    "hermes_agent",  # v3 tree is guarded by silent_swallow_lint
}


def _iter_whole_repo_ex_v3():
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & _EXCLUDED_DIRS:
            continue
        yield path


def _body_is_silent_fallback(body: list[ast.stmt]) -> bool:
    """True if the handler body is just ``var = None`` (or = False) with
    no logging, no re-raise, no taxonomy classification.
    """
    if not body:
        return False
    if len(body) > 2:
        return False
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            # Look for `X = None` or `X = False` — bare reassignment.
            if (
                isinstance(stmt.value, ast.Constant)
                and stmt.value.value in (None, False)
            ):
                continue
            return False
        if isinstance(stmt, ast.Pass):
            continue
        return False
    return True


def _scan_silent_fallbacks(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if not _body_is_silent_fallback(handler.body):
                continue
            exc_type = ast.unparse(handler.type) if handler.type else "bare"
            hits.append((handler.lineno, f"except {exc_type}: {ast.unparse(handler.body[0])}"))
    return hits


# ---------------------------------------------------------------------------


def test_silent_fallback_scanner_detects_synthetic_hit():
    """Meta — the scanner catches the anti-pattern it's supposed to catch."""
    src = """
try:
    foo()
except Exception:
    x = None
"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            assert _body_is_silent_fallback(node.handlers[0].body)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "spec §12 Phase D switch not yet landed — legacy tui_gateway wraps "
        "hermes_agent.domain.terminate_run() in try/except and silently "
        "resets to None on any error, falling back to the legacy path. "
        "This xfail flips to xpassed when Phase D removes the fallback "
        "(see docs/v3_audit_report.md §四 伪绿 2 + 优先级 1 #3)."
    ),
)
def test_no_silent_fallback_outside_v3():
    """Whole-repo scan (excluding v3 tree): every ``except: var = None``
    silent-fallback is a place where a caller pretends the failure never
    happened and continues down the legacy path.
    """
    offenders: list[tuple[str, int, str]] = []
    for path in _iter_whole_repo_ex_v3():
        for lineno, snippet in _scan_silent_fallbacks(path):
            offenders.append(
                (path.relative_to(REPO_ROOT).as_posix(), lineno, snippet)
            )
    if offenders:
        formatted = "\n".join(
            f"  {p}:{n}  {s}" for p, n, s in offenders[:40]
        )
        extra = f"\n  ... and {len(offenders) - 40} more" if len(offenders) > 40 else ""
        raise AssertionError(
            f"silent-fallback anti-pattern still present in {len(offenders)} sites:\n"
            + formatted + extra
        )
