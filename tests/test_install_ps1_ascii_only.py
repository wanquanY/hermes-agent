from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def test_install_ps1_is_pure_ascii() -> None:
    raw = INSTALL_PS1.read_bytes()
    offender_lines: list[int] = []
    line_number = 1
    for byte in raw:
        if byte == 0x0A:
            line_number += 1
        elif byte >= 0x80:
            offender_lines.append(line_number)

    assert not offender_lines, (
        "scripts/install.ps1 must remain pure ASCII for BOM-less Windows "
        "PowerShell 5.1 parsing; offending lines: "
        f"{sorted(set(offender_lines))}"
    )
