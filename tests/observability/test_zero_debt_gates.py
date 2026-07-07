from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO_ROOT / "docs" / "hermes_zero_debt_manifest.md"
EXECUTION_PLAN = REPO_ROOT / "docs" / "hermes_zero_debt_execution_plan.md"
VERDICT = REPO_ROOT / "scripts" / "zero_debt" / "verdict.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_zero_debt_p0_documents_exist() -> None:
    assert MANIFEST.exists()
    assert EXECUTION_PLAN.exists()
    assert VERDICT.exists()


def test_manifest_defines_required_allowlists_and_gates() -> None:
    text = _read(MANIFEST)
    for token in (
        "## Frontend V3.1 Freeze",
        "## Keep List",
        "## Kill List",
        "## Rebuild List",
        "## Production Grep Gates",
        "## Tests And Docs Allowlist",
        "## Wire Boundary Allowlist",
        "`hermes_agent/gateway/pipeline.py`",
        "`no_sessiondb_production`",
        "`no_legacy_identity_alias_internal`",
        "`event_ledger_single_writer`",
        "`run_state_single_writer`",
    ):
        assert token in text


def test_execution_plan_requires_vertical_slices_and_two_signoffs() -> None:
    text = _read(EXECUTION_PLAN)
    for token in (
        "dispatch -> domain/repository -> SQLite -> wire response",
        "docs/audits/zero_debt_phase_pX_verdict.json",
        "docs/audits/zero_debt_phase_pX_human_signoff.md",
    ):
        assert token in text
    for phase in range(7):
        assert f"## P{phase} " in text


def test_p0_verdict_json_passes_contract_checks() -> None:
    output = subprocess.check_output(
        [sys.executable, str(VERDICT), "--phase", "P0", "--json"],
        cwd=REPO_ROOT,
        text=True,
    )
    verdict = json.loads(output)
    assert verdict["phase"] == "P0"
    assert verdict["status"] == "pass"
    assert verdict["checks"]
    assert all(check["ok"] for check in verdict["checks"])
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p0_human_signoff.md"
    )
