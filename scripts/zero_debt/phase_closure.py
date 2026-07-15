#!/usr/bin/env python3
"""Hermes zero-debt phase closure gate.

`verdict.py` is the machine gate for static ownership checks. This script is
the phase-closure gate: it requires the machine verdict to pass and the human
sign-off file to contain an explicit approval instead of a pending template.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from verdict import REPO_ROOT, build_verdict


_PENDING_MARKERS = (
    "`pending`",
    "`pending_user_signoff`",
    "Decision: `pending`",
    "签收状态：`pending`",
    "待用户真机确认",
    "待填写",
    "TODO",
)


def _next_phase(phase: str) -> str:
    normalized = phase.upper()
    if normalized.startswith("P") and normalized[1:].isdigit():
        return f"P{int(normalized[1:]) + 1}"
    return "next phase"


def _human_signoff_result(phase: str, signoff_path: Path) -> dict[str, Any]:
    try:
        relative = signoff_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        relative = signoff_path.as_posix()
    if not signoff_path.exists():
        return {
            "id": "human_signoff:exists",
            "ok": False,
            "message": f"{relative} is missing",
        }

    text = signoff_path.read_text(encoding="utf-8")
    pending = [marker for marker in _PENDING_MARKERS if marker in text]
    if pending:
        return {
            "id": "human_signoff:not_pending",
            "ok": False,
            "message": f"{relative} still contains pending markers: {', '.join(pending)}",
        }

    next_phase = _next_phase(phase)
    approval_tokens = [
        f"{phase.upper()} 真机验收通过",
        f"进入 {next_phase}",
    ]
    missing = [token for token in approval_tokens if token not in text]
    if missing:
        return {
            "id": "human_signoff:explicit_approval",
            "ok": False,
            "message": f"{relative} missing explicit approval tokens: {', '.join(missing)}",
        }

    return {
        "id": "human_signoff:explicit_approval",
        "ok": True,
        "message": f"{relative} explicitly approves entering {next_phase}",
    }


def build_closure(phase: str) -> dict[str, Any]:
    normalized = phase.upper()
    machine = build_verdict(normalized)
    checks: list[dict[str, Any]] = [
        {
            "id": "machine_verdict:pass",
            "ok": machine.get("status") == "pass",
            "message": f"machine verdict status is {machine.get('status')}",
        }
    ]

    signoff = machine.get("next_required_human_signoff")
    if signoff:
        checks.append(_human_signoff_result(normalized, REPO_ROOT / str(signoff)))
    else:
        checks.append(
            {
                "id": "human_signoff:path_known",
                "ok": False,
                "message": "machine verdict did not declare next_required_human_signoff",
            }
        )

    failed = [check for check in checks if not check["ok"]]
    return {
        "phase": normalized,
        "status": "fail" if failed else "pass",
        "checks": checks,
        "machine_verdict": {
            "phase": machine.get("phase"),
            "status": machine.get("status"),
            "check_count": len(machine.get("checks", [])),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes zero-debt phase closure gate")
    parser.add_argument("--phase", required=True, help="Phase id, for example P1")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    closure = build_closure(args.phase)
    if args.json:
        print(json.dumps(closure, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{closure['phase']} {closure['status']}")
        for check in closure["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"[{marker}] {check['id']}: {check['message']}")
    return 0 if closure["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
