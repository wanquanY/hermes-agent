from __future__ import annotations

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


def test_interaction_internal_run_event_writes_have_single_owner() -> None:
    offenders: list[str] = []
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _ALLOWED_DIRECT_INTERNAL_REFERENCES:
            continue
        text = path.read_text(encoding="utf-8")
        if "_internal.interaction." not in text:
            continue
        if "append_run_event" in text or "InternalRunEventType" in text:
            offenders.append(rel)

    if offenders:
        raise AssertionError(
            "Interaction lifecycle persistence must go through "
            "tui_gateway/services/interaction_registry.py; direct internal "
            "run_event ownership found in:\n  " + "\n  ".join(sorted(offenders))
        )
