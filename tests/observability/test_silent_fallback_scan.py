"""spec §J11 + §I —— 静默降级 (silent fallback) 全仓扫描.

Silent fallback is a subtle variant of silent-swallow: a caller wraps a
new-path call in ``try / except`` and, on any exception, silently
reassigns to ``None`` or ``False`` and continues down the legacy path.
This defeats spec §J4 (RunStateMachine single entry) and hides real
production failures.

Audit 2026-07-07 (docs/v3_audit_report.md §四 伪绿 2, 优先级 1 #3) called
out ``tui_gateway/services/run_control.py:2216, :2224`` exactly this
pattern for ``terminate_run``.

This scan targets the production anti-pattern that hid the Phase C/D
transition failure and adjacent renamed variants::

    except <anything>:
        atomic_result = None
        terminal_result = None
        # legacy publish continues

If found, the caller is falling back to legacy terminal persistence
silently. Generic silent-swallow cleanup is covered by the separate
silent_swallow tests; this file guards the run terminal fallback class.
"""

from __future__ import annotations

import ast
from pathlib import Path

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


def _body_is_terminal_silent_fallback(body: list[ast.stmt]) -> bool:
    """True if a handler resets terminal atomic state and keeps going."""
    if not body:
        return False
    suspicious_names = {
        "atomic_result",
        "terminal_result",
        "termination_result",
        "terminate_result",
        "run_state_result",
        "run_status_result",
        "state_machine_result",
    }
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            if not (
                isinstance(stmt.value, ast.Constant)
                and stmt.value.value is None
            ):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id in suspicious_names:
                    return True
    return False


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
            if not _body_is_terminal_silent_fallback(handler.body):
                continue
            exc_type = ast.unparse(handler.type) if handler.type else "bare"
            hits.append((handler.lineno, f"except {exc_type}: {ast.unparse(handler.body[0])}"))
    return hits


# ---------------------------------------------------------------------------


def test_terminal_fallback_scanner_detects_synthetic_hit():
    """Meta — the scanner catches the anti-pattern it's supposed to catch."""
    for src in (
        """
try:
    foo()
except Exception:
    atomic_result = None
""",
        """
try:
    foo()
except Exception:
    terminal_result = None
""",
    ):
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                assert _body_is_terminal_silent_fallback(node.handlers[0].body)


def test_no_terminal_silent_fallback_outside_v3():
    """Whole-repo scan (excluding v3 tree): terminal fallback must fail
    loudly or use an explicit degraded state, never silently route to legacy.
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
