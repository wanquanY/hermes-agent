#!/usr/bin/env python3
"""Hermes zero-debt phase verdict.

P0 intentionally validates the execution contract, not the final debt-free
state. Later phases can promote current warnings into blocking failures.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs" / "hermes_zero_debt_manifest.md"
EXECUTION_PLAN = REPO_ROOT / "docs" / "hermes_zero_debt_execution_plan.md"
VERDICT_SCRIPT = REPO_ROOT / "scripts" / "zero_debt" / "verdict.py"
GATE_TEST = REPO_ROOT / "tests" / "observability" / "test_zero_debt_gates.py"


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "message": self.message}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _contains_all(text: str, tokens: list[str]) -> bool:
    return all(token in text for token in tokens)


def _required_file_checks() -> list[Check]:
    required = [
        MANIFEST,
        EXECUTION_PLAN,
        VERDICT_SCRIPT,
        GATE_TEST,
    ]
    return [
        Check(
            id=f"file_exists:{path.relative_to(REPO_ROOT).as_posix()}",
            ok=path.exists(),
            message="exists" if path.exists() else "missing",
        )
        for path in required
    ]


def _manifest_checks() -> list[Check]:
    text = _read(MANIFEST)
    sections = [
        "## Frontend V3.1 Freeze",
        "## Keep List",
        "## Kill List",
        "## Rebuild List",
        "## Production Grep Gates",
        "## Tests And Docs Allowlist",
        "## Wire Boundary Allowlist",
        "## Phase Sign-Off Contract",
    ]
    checks = [
        Check(
            id="manifest:required_sections",
            ok=_contains_all(text, sections),
            message="all required sections present",
        ),
        Check(
            id="manifest:wire_boundary_explicit",
            ok=(
                "`hermes_agent/gateway/pipeline.py`" in text
                and "stored_session_id" in text
                and "stable_session_id" in text
                and "runtime_session_id" in text
            ),
            message="wire alias allowlist is explicit",
        ),
        Check(
            id="manifest:production_gates_defined",
            ok=_contains_all(
                text,
                [
                    "`no_sessiondb_production`",
                    "`no_legacy_identity_alias_internal`",
                    "`no_method_modules`",
                    "`no_dovie_overrides`",
                    "`event_ledger_single_writer`",
                    "`run_state_single_writer`",
                ],
            ),
            message="production grep gates are named",
        ),
    ]
    return checks


def _execution_plan_checks() -> list[Check]:
    text = _read(EXECUTION_PLAN)
    phase_tokens = [f"## P{i} " for i in range(7)]
    return [
        Check(
            id="plan:p0_to_p6_present",
            ok=_contains_all(text, phase_tokens),
            message="P0-P6 are defined",
        ),
        Check(
            id="plan:vertical_slice_rule",
            ok="dispatch -> domain/repository -> SQLite -> wire response" in text,
            message="vertical slice E2E rule is present",
        ),
        Check(
            id="plan:two_signoff_files",
            ok=(
                "zero_debt_phase_pX_verdict.json" in text
                and "zero_debt_phase_pX_human_signoff.md" in text
            ),
            message="machine and user sign-off files are required",
        ),
    ]


def _working_state_warnings() -> list[str]:
    warnings: list[str] = []
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch and branch != "feat/hermes-zero-debt":
        warnings.append(
            f"current branch is {branch!r}; destructive phases must run on 'feat/hermes-zero-debt'"
        )
    status = _git(["status", "--short"])
    if status:
        warnings.append("worktree is dirty; P1+ requires an explicit baseline or clean handoff")
    return warnings


def build_p0_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_manifest_checks(),
        *_execution_plan_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P0",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": _working_state_warnings(),
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p0_human_signoff.md",
    }


def build_verdict(phase: str) -> dict[str, Any]:
    normalized = str(phase or "").upper()
    if normalized != "P0":
        return {
            "phase": normalized,
            "status": "fail",
            "checks": [
                {
                    "id": "phase:supported",
                    "ok": False,
                    "message": "only P0 verdict is implemented; extend this script before later phase sign-off",
                }
            ],
            "warnings": [],
        }
    return build_p0_verdict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes zero-debt phase verdict")
    parser.add_argument("--phase", default="P0", help="Phase id, currently P0")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    verdict = build_verdict(args.phase)
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{verdict['phase']} {verdict['status']}")
        for check in verdict["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"[{marker}] {check['id']}: {check['message']}")
        for warning in verdict.get("warnings", []):
            print(f"[warn] {warning}")
    return 0 if verdict["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
