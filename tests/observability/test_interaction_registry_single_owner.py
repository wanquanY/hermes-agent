from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_ALLOWED_DIRECT_INTERNAL_REFERENCES = {
    "tui_gateway/services/interaction_registry.py",
    "hermes_agent/domain/interaction.py",
    "hermes_agent/domain/canonical_event.py",
    "hermes_agent/domain/event_ledger.py",
    "hermes_state_runs.py",
}

_EXCLUDED_DIRS = {
    ".venv",
    "__pycache__",
    ".import_linter_cache",
    "tests",
    "docs",
}


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if set(rel.parts) & _EXCLUDED_DIRS:
            continue
        files.append(path)
    return files


def _contains_direct_interaction_write(source: str) -> bool:
    # The ownership contract only targets the enum name and canonical internal
    # event prefix. Avoid constructing ASTs for the overwhelming majority of
    # production files that cannot possibly match; this gate runs alongside
    # the entire repository suite and must stay deterministic under load.
    if (
        "InternalRunEventType" not in source
        and "_internal.interaction." not in source
    ):
        return False
    tree = ast.parse(source)
    nodes = tuple(ast.walk(tree))
    if any(
        (isinstance(node, ast.Name) and node.id == "InternalRunEventType")
        or (isinstance(node, ast.Attribute) and node.attr == "InternalRunEventType")
        for node in nodes
    ):
        return True
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        function_name = ""
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            function_name = node.func.attr
        if function_name not in {"append_event", "append_run_event"}:
            continue
        if any(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value.startswith("_internal.interaction.")
            for value in ast.walk(node)
        ):
            return True
    return False


def test_interaction_internal_run_event_writes_have_single_owner() -> None:
    offenders: list[str] = []
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _ALLOWED_DIRECT_INTERNAL_REFERENCES:
            continue
        text = path.read_text(encoding="utf-8")
        if _contains_direct_interaction_write(text):
            offenders.append(rel)

    if offenders:
        raise AssertionError(
            "Interaction lifecycle persistence must go through "
            "tui_gateway/services/interaction_registry.py; direct internal "
            "run_event ownership found in:\n  " + "\n  ".join(sorted(offenders))
        )
