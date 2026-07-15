"""Static guard — the boundary between v3 ``hermes_agent/`` and the legacy
``gateway/`` folder must remain clean in both directions.

Rationale (spec §12 Phase J prep):
* User's guidance: "channel 相关的不要动" — legacy ``gateway/`` folder
  (including telegram / discord / whatsapp / signal / feishu / qqbot /
  homeassistant / slack / signal / dingtalk / sms / email / mattermost /
  wechat platform adapters) stays intact until explicit retirement is
  approved.
* v3 landing goal: the new ``hermes_agent/`` package is self-contained;
  no legacy import chains its way into the aggregate roots.
* If either direction starts coupling, Phase J retirement becomes
  entangled and this test catches it early.

This test is intentionally coarse — one grep per direction — so any
future breakage is immediately visible.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LEGACY_GATEWAY = REPO_ROOT / "gateway"
V3_PACKAGE = REPO_ROOT / "hermes_agent"


_V3_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+hermes_agent(?:\.|\s|$)")
_LEGACY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+gateway(?:\.|\s|$)")


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        # Skip byte-compiled caches and vendored code.
        if "__pycache__" in path.parts:
            continue
        yield path


# ---------------------------------------------------------------------------


def test_v3_hermes_agent_never_imports_legacy_gateway():
    """v3 ``hermes_agent/`` code must not import from the legacy ``gateway/``
    folder (which is under Phase J retirement pending).
    """
    if not V3_PACKAGE.exists():
        raise AssertionError(
            "hermes_agent/ must exist — Phase 0 landing failed?"
        )
    offenders: list[tuple[Path, int, str]] = []
    for path in _iter_python_files(V3_PACKAGE):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _LEGACY_IMPORT_RE.match(line):
                offenders.append((path, lineno, line.strip()))
    if offenders:
        formatted = "\n".join(
            f"  {p.relative_to(REPO_ROOT)}:{n}  {code}"
            for p, n, code in offenders
        )
        raise AssertionError(
            "v3 hermes_agent/ code imports legacy gateway/ — Phase J "
            "retirement would become entangled:\n" + formatted
        )


def test_legacy_gateway_never_imports_v3_hermes_agent():
    """Legacy ``gateway/`` code must not depend on v3 ``hermes_agent/``
    either. If it did, retiring the legacy folder would break v3 by
    accident during acceptance.
    """
    if not LEGACY_GATEWAY.exists():
        # Legacy folder already retired — nothing to check.
        return
    offenders: list[tuple[Path, int, str]] = []
    for path in _iter_python_files(LEGACY_GATEWAY):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _V3_IMPORT_RE.match(line):
                offenders.append((path, lineno, line.strip()))
    if offenders:
        formatted = "\n".join(
            f"  {p.relative_to(REPO_ROOT)}:{n}  {code}"
            for p, n, code in offenders
        )
        raise AssertionError(
            "legacy gateway/ code imports v3 hermes_agent/ — retiring "
            "the legacy folder would break v3:\n" + formatted
        )


def test_legacy_gateway_folder_still_present():
    """Sentinel — this test *encodes* the fact that Phase J retirement
    has not been executed. When user later says "OK retire it" and it's
    done, this assertion flips to ``not LEGACY_GATEWAY.exists()`` and
    Phase J is closed.
    """
    assert LEGACY_GATEWAY.exists() and LEGACY_GATEWAY.is_dir(), (
        "legacy gateway/ has been unexpectedly retired — if this was "
        "intentional, flip the assertion; otherwise investigate the "
        "missing folder"
    )
