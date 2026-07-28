"""spec §J11 whole-repo silent-swallow reality check.

Prior scanner (``tests/observability/test_silent_swallow_lint.py``) only
inspected ``hermes_agent/`` — legacy code (hermes_state.py,
tui_gateway/, gateway/, hermes_team_mission/, ...) sits outside that
scope and has decades of ``except X: pass`` accretion.

Audit 2026-07-07 (docs/v3_audit_report.md 二.Phase I) called for a
whole-repo scan + CI gate. This test provides the whole-repo scan; the
``.github/workflows/hermes-v3-gates.yml`` workflow provides the CI gate.

The test is ``xfail(strict=False)`` — the *count* of silent swallows in
the legacy tree is the signal. When Phase I finishes retiring the
legacy trees, the test flips to xpassed and can be un-x-failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_agent.observability.silent_swallow_lint import scan_paths


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Scan every directory that ships source outside vendored third-party trees.
# We exclude .venv / __pycache__ / build outputs / .import_linter_cache.
_LEGACY_TREES = [
    REPO_ROOT / "gateway",
    REPO_ROOT / "tui_gateway",
    REPO_ROOT / "hermes_team_mission",
    REPO_ROOT / "dovie_extension",
    REPO_ROOT / "hermes_cli",
    REPO_ROOT / "acp_adapter",
    REPO_ROOT / "cron",
    REPO_ROOT / "plugins",
    REPO_ROOT / "providers",
]


@pytest.mark.xfail(
    strict=False,
    reason=(
        "spec §12 Phase I not yet finished — legacy trees (gateway/, "
        "tui_gateway/, hermes_state*.py, ...) still contain silent-swallow "
        "sites. This xfail is the reality signal; when Phase I lands (or the "
        "legacy trees are retired by Phase D/G/J), it flips to xpassed."
    ),
)
def test_no_silent_swallow_across_legacy_trees():
    roots = [p for p in _LEGACY_TREES if p.exists()]
    findings = scan_paths(roots)
    if findings:
        head = "\n".join(
            f"  {f.file}:{f.line}  except {f.exception_type}: pass"
            for f in findings[:20]
        )
        extra = f"\n  ... and {len(findings) - 20} more" if len(findings) > 20 else ""
        raise AssertionError(
            f"silent swallow across legacy trees ({len(findings)} sites):\n"
            + head
            + extra
        )
