#!/usr/bin/env python3
"""Summarize zero-debt phase state.

This is a read-only operator view. It deliberately separates machine verdict
from phase closure so a passing verdict cannot be mistaken for permission to
enter the next phase.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from phase_closure import build_closure
from verdict import build_verdict


def build_status(phases: list[str]) -> dict[str, Any]:
    phase_rows: list[dict[str, Any]] = []
    for raw_phase in phases:
        phase = str(raw_phase or "").upper()
        verdict = build_verdict(phase)
        closure = build_closure(phase)
        failed_verdict = [c for c in verdict.get("checks", []) if not c.get("ok")]
        failed_closure = [c for c in closure.get("checks", []) if not c.get("ok")]
        phase_rows.append(
            {
                "phase": phase,
                "machine_verdict": verdict.get("status"),
                "machine_failed_checks": [c.get("id") for c in failed_verdict],
                "closure": closure.get("status"),
                "closure_failed_checks": [c.get("id") for c in failed_closure],
                "next_required_human_signoff": verdict.get("next_required_human_signoff", ""),
            }
        )
    return {"phases": phase_rows}


def _render_text(status: dict[str, Any]) -> str:
    lines: list[str] = []
    for row in status["phases"]:
        lines.append(
            f"{row['phase']}: verdict={row['machine_verdict']} "
            f"closure={row['closure']} "
            f"human_signoff={row['next_required_human_signoff'] or '-'}"
        )
        if row["machine_failed_checks"]:
            lines.append("  verdict_failed=" + ", ".join(row["machine_failed_checks"]))
        if row["closure_failed_checks"]:
            lines.append("  closure_failed=" + ", ".join(row["closure_failed_checks"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize Hermes zero-debt phase status")
    parser.add_argument(
        "--phase",
        action="append",
        dest="phases",
        help="Phase to include. Can be repeated. Defaults to P1 and P2.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    status = build_status(args.phases or ["P1", "P2"])
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render_text(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
