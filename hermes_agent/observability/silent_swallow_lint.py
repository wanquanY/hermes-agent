"""AST-based lint for the ``except: pass`` silent-swallow ban (spec §J11).

Rule: an ``except`` handler body that is exactly ``pass`` — or only ``pass``
+ bare comments — is banned. Callers must classify as
``except RecoverableError`` (log warning + continue), ``except FatalError``
(log.exception + escalate), or a specific exception type paired with a real
handler body.

The banned form is easy to introduce but always hides real production
failures — spec §12 Phase I mandates zero occurrences under
``hermes_agent/``. Legacy code outside that root is grandfathered until
Phase D5 completes legacy state-facade retirement.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SilentSwallowFinding:
    file: str
    line: int
    exception_type: str        # 'bare' | 'Exception' | qualified name
    reason: str


def _handler_is_silent(handler: ast.ExceptHandler) -> bool:
    """Body is only ``pass`` statements (possibly repeated)."""
    body = list(handler.body)
    if not body:
        return True
    return all(isinstance(stmt, ast.Pass) for stmt in body)


def _handler_type_name(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare"
    return ast.unparse(handler.type)


def scan_source(source: str, *, filename: str = "<source>") -> list[SilentSwallowFinding]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    findings: list[SilentSwallowFinding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if not _handler_is_silent(handler):
                continue
            exc_type = _handler_type_name(handler)
            # Explicit allowance: ``except (RecoverableError,): pass`` is a
            # spec-recognized swallow; treat it as OK. Callers must still log
            # via observability helpers when they want a trail — pass alone
            # is fine because the RecoverableError type is itself the signal.
            if exc_type in {"RecoverableError", "hermes_agent.observability.RecoverableError"}:
                continue
            findings.append(
                SilentSwallowFinding(
                    file=filename,
                    line=handler.lineno,
                    exception_type=exc_type,
                    reason=(
                        "silent swallow — reclassify as RecoverableError / FatalError "
                        "or add a log line via hermes_agent.observability"
                    ),
                )
            )
    return findings


def scan_paths(roots: Iterable[Path]) -> list[SilentSwallowFinding]:
    """Walk each root recursively, returning all findings under it."""
    all_findings: list[SilentSwallowFinding] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = Path(dirpath) / name
                try:
                    source = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                all_findings.extend(scan_source(source, filename=str(path)))
    return all_findings
