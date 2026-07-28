"""Phase L — mutation-verification harness (spec §12).

Each ``MutationSpec`` describes:
* the source file to mutate
* an exact ``find``/``replace`` pair (mutation)
* which test(s) the mutation MUST turn red
* which invariant tag it targets (J1-J11)

Running a mutation:
1. read + backup source
2. verify ``find`` matches exactly once
3. write mutated source
4. run pytest on ``expected_failing_tests`` — expect non-zero exit
5. restore original source (always, even on error)
6. assert step 4 was non-zero — the test suite proved the mutation broke it

The harness is deliberately conservative: any mismatch, syntax error, or
missing anchor is a hard fail so nobody ships without the guard actually
biting on their invariant.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


HERMES_AGENT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MutationSpec:
    """One mutation targeting one invariant (spec §J1-§J11)."""

    invariant: str                     # "J2", "J4", "J7", ... spec anchor
    source_path: str                   # repo-relative, e.g. "hermes_agent/domain/seq_allocator.py"
    find: str                          # exact string to replace (must be unique)
    replace: str                       # mutated substitute
    expected_failing_tests: tuple[str, ...]  # pytest node ids that MUST fail
    description: str = ""              # human-readable summary


@dataclass(frozen=True)
class MutationResult:
    spec: MutationSpec
    tests_red: bool                    # True = at least one expected test failed
    pytest_returncode: int
    stdout_tail: str
    stderr_tail: str


class MutationHarnessError(RuntimeError):
    """Raised when the harness cannot proceed (anchor not found, restore failed, ...)."""


def _resolve(path: str) -> Path:
    resolved = HERMES_AGENT_ROOT / path
    if not resolved.exists():
        raise MutationHarnessError(f"source path not found: {resolved}")
    return resolved


def _purge_bytecode_cache(source_path: Path) -> None:
    """Delete any .pyc file for ``source_path`` — the subprocess pytest run
    likely compiled the mutated source and cached it under ``__pycache__``.
    If we leave the stale bytecode behind, the parent pytest session (or a
    subsequent one) can import the mutated version even after we restore
    the .py file on disk.
    """
    cache_dir = source_path.parent / "__pycache__"
    if not cache_dir.exists():
        return
    for pyc in cache_dir.glob(f"{source_path.stem}.cpython-*.pyc"):
        try:
            pyc.unlink()
        except OSError as exc:
            # Best-effort cleanup — a failed unlink here just means the next
            # importer may pick up stale bytecode; leave breadcrumbs so a
            # confused test author can trace back to the real cause.
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "mutation harness could not purge %s: %s", pyc, exc
            )


def _run_pytest(nodes: tuple[str, ...]) -> tuple[int, str, str]:
    """Run pytest on the given nodes; return (returncode, stdout_tail, stderr_tail)."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-x",
        "--no-header",
        "--tb=short",
        *nodes,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(HERMES_AGENT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    out_tail = "\n".join(proc.stdout.splitlines()[-25:])
    err_tail = "\n".join(proc.stderr.splitlines()[-10:])
    return proc.returncode, out_tail, err_tail


def apply_mutation(spec: MutationSpec) -> MutationResult:
    """Run one mutation cycle and return the outcome.

    Always restores the source file — even when the harness itself raises.
    """
    path = _resolve(spec.source_path)
    original = path.read_text(encoding="utf-8")

    if spec.find not in original:
        raise MutationHarnessError(
            f"mutation anchor missing in {spec.source_path}: {spec.find!r}"
        )
    if original.count(spec.find) > 1:
        raise MutationHarnessError(
            f"mutation anchor is not unique in {spec.source_path}: "
            f"{spec.find!r} appears {original.count(spec.find)} times"
        )

    mutated = original.replace(spec.find, spec.replace)
    try:
        path.write_text(mutated, encoding="utf-8")
        rc, out_tail, err_tail = _run_pytest(spec.expected_failing_tests)
    finally:
        path.write_text(original, encoding="utf-8")
        _purge_bytecode_cache(path)

    return MutationResult(
        spec=spec,
        tests_red=rc != 0,
        pytest_returncode=rc,
        stdout_tail=out_tail,
        stderr_tail=err_tail,
    )
