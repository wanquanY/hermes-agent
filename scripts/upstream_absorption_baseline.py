#!/usr/bin/env python3
"""Generate reproducible Git evidence for a manual upstream absorption cycle."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


RUNTIME_CORE = re.compile(
    r"^(?:agent/|gateway/|tools/|hermes_agent/|hermes_cli/|tui_gateway/|cron/|"
    r"run_agent\.py$|model_tools\.py$|hermes_state\.py$)"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _changed_files(repo: Path, base: str, ref: str) -> set[str]:
    output = _git(repo, "diff", "--name-only", f"{base}..{ref}")
    return {line for line in output.splitlines() if line}


def generate(repo: Path, output_dir: Path, local_ref: str, upstream_ref: str) -> dict[str, object]:
    local_sha = _git(repo, "rev-parse", local_ref)
    upstream_sha = _git(repo, "rev-parse", upstream_ref)
    merge_base = _git(repo, "merge-base", local_sha, upstream_sha)
    left, right = _git(repo, "rev-list", "--left-right", "--count", f"{local_sha}...{upstream_sha}").split()

    local_files = _changed_files(repo, merge_base, local_sha)
    upstream_files = _changed_files(repo, merge_base, upstream_sha)
    overlap = sorted(local_files & upstream_files)
    runtime_overlap = [path for path in overlap if RUNTIME_CORE.search(path)]

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "overlap-files.txt").write_text("\n".join(overlap) + "\n", encoding="utf-8")
    (output_dir / "runtime-core-overlap-files.txt").write_text(
        "\n".join(runtime_overlap) + "\n", encoding="utf-8"
    )

    evidence: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": str(repo.resolve()),
        "local": {"ref": local_ref, "sha": local_sha, "unique_commits": int(left)},
        "upstream": {"ref": upstream_ref, "sha": upstream_sha, "unique_commits": int(right)},
        "merge_base": merge_base,
        "changed_files": {
            "local": len(local_files),
            "upstream": len(upstream_files),
            "overlap": len(overlap),
            "runtime_core_overlap": len(runtime_overlap),
        },
        "runtime_core_pattern": RUNTIME_CORE.pattern,
    }
    (output_dir / "baseline.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--local-ref", default="HEAD")
    parser.add_argument("--upstream-ref", default="upstream/main")
    args = parser.parse_args()
    evidence = generate(args.repo.resolve(), args.output_dir, args.local_ref, args.upstream_ref)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
