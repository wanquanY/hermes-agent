"""Architecture debt gate for the gateway-owned production packages."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAX_PRODUCTION_FILE_LINES = 2_000

# Existing oversized modules are explicit migration debt. Their ceilings are
# pinned to the committed baseline, so they can shrink but never grow and no
# new oversized gateway module can be introduced.
LEGACY_LINE_CEILINGS = {
    "tui_gateway/services/run_control.py": 3_278,
}


def _production_python_files() -> list[Path]:
    return sorted(
        path
        for package in (ROOT / "hermes_agent", ROOT / "tui_gateway")
        for path in package.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_gateway_production_files_respect_line_count_boundary() -> None:
    offenders: list[str] = []
    observed_legacy_debt: set[str] = set()
    for path in _production_python_files():
        relative = path.relative_to(ROOT).as_posix()
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        ceiling = LEGACY_LINE_CEILINGS.get(relative, MAX_PRODUCTION_FILE_LINES)
        if relative in LEGACY_LINE_CEILINGS and line_count > MAX_PRODUCTION_FILE_LINES:
            observed_legacy_debt.add(relative)
        if line_count > ceiling:
            offenders.append(f"{relative}: {line_count} > {ceiling}")

    missing = sorted(set(LEGACY_LINE_CEILINGS) - observed_legacy_debt)
    assert not missing, f"remove resolved entries from LEGACY_LINE_CEILINGS: {missing}"
    assert not offenders, "gateway source-size policy violations:\n" + "\n".join(offenders)
