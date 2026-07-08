"""Anti-relocation gate — no god-object class may survive by moving into
the blessed source trees (spec §12 P2/P4/P5).

Audit 2026-07-08/09 established the failure mode this gate closes:

  P2 nearly closed with the 6787-line ``SessionDB`` god-object RELOCATED
  into ``hermes_agent/storage/state_store.py`` and renamed
  ``HermesStateStore`` — the manifest's ``SessionDB``/``hermes_state``
  grep gates were evaded by the rename+move. codex then added
  P2-specific gates (``state_store_decomposed``, ``hermes_state_store_no_methods``).

  The identical loophole is still open downstream:
    * P4 gates only check "old worker services have no production imports"
      — a verbatim relocation of ``tui_gateway/services/worker_*.py`` into
      ``hermes_agent/orchestration/`` passes.
    * P5 gates only check "``gateway/`` is gone" + "no ``^from gateway``"
      — relocating ``gateway/run.py`` (18277 lines / GatewayRunner **209
      methods**) into ``hermes_gateway/run.py`` passes while the monolith
      survives under a new name.

This gate uses **method count per class** — a behavioral, name- and
path-independent god-object signal. A class with an extreme number of
methods is a god-object regardless of what it is called or where it
lives, so a relocated monolith trips it immediately.

Why method count (not line count): channel adapters are legitimately
large files (``feishu.py`` 1972 lines) — the manifest's Rebuild List
explicitly accepts adapter-file size as separate, deferred debt. But
their largest CLASS has only 55 methods. The true god-objects have
199 (``HermesStateStore``) and 209 (``GatewayRunner``). Method count
cleanly separates them; line count does not. Data-plane monoliths that
are large-but-few-methods (``state_mixins/runs.py``) are already covered
by ``state_store_decomposed`` + the §4.6 single-owner guard.

Calibration (2026-07-09): largest legitimate class = 55 methods
(``feishu`` adapter, ``SessionRepoImpl`` 54). Threshold 80 sits well
above legit code and well below the god-objects.

Marked ``xfail(strict=False)`` — currently trips on ``HermesStateStore``
(199 methods, the known P2 god-object still being decomposed). Flips to
xpassed when P2 finishes decomposing it AND no P4/P5 relocation
reintroduces a monolith. Fix by decomposing, never by raising the
threshold.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Largest legit class = 55 methods; god-objects = 199 / 209.
MAX_CLASS_METHODS = 80

# The trees a god-object must never sneak into. gateway/ and tui_gateway/
# are legacy (being retired) — this gate guards only the trees meant to
# stay clean. hermes_gateway/ is pre-included in case P4/P5 create it as
# gateway/run.py's new home.
_BLESSED_TREES = ("hermes_agent", "channels", "hermes_gateway")

_EXCLUDE_PREFIXES = ("hermes_agent/storage/migrations/",)


def _iter_blessed_files():
    for tree in _BLESSED_TREES:
        root = REPO_ROOT / tree
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if any(rel.startswith(p) for p in _EXCLUDE_PREFIXES):
                continue
            yield path, rel


def _god_object_classes() -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for path, rel in _iter_blessed_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = sum(
                    1
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
                if methods > MAX_CLASS_METHODS:
                    out.append((rel, node.name, methods))
    return sorted(out, key=lambda x: -x[2])


# ---------------------------------------------------------------------------


def test_threshold_does_not_flag_legit_classes():
    """Sanity — the largest legit class (SessionRepoImpl, 54 methods) sits
    under the threshold, so any offender is unambiguously a god-object.
    """
    session_repo = REPO_ROOT / "hermes_agent" / "repositories" / "session_repo.py"
    tree = ast.parse(session_repo.read_text(encoding="utf-8"))
    max_methods = max(
        (
            sum(
                1
                for c in n.body
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
        ),
        default=0,
    )
    assert max_methods <= MAX_CLASS_METHODS, (
        f"SessionRepoImpl has {max_methods} methods, over threshold "
        f"{MAX_CLASS_METHODS} — recalibrate; a legit repo must not trip "
        f"the god-object gate"
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Anti-relocation reality signal. Currently trips on "
        "HermesStateStore (199 methods in hermes_agent/storage/state_store.py) "
        "— the known P2 god-object still being decomposed. Flips to xpassed "
        "when P2 decomposes it and no P4/P5 relocation reintroduces a "
        "monolith (e.g. GatewayRunner, 209 methods, moved from gateway/run.py "
        "into hermes_gateway/). Fix by decomposing, never by raising the "
        "threshold."
    ),
)
def test_no_god_object_class_in_blessed_trees():
    """spec §12 anti-relocation gate — no class in hermes_agent/ /
    channels/ / hermes_gateway/ may exceed the god-object method-count
    threshold.

    Name- and path-independent: catches the P2-style fake-out (rename+move
    a monolith into the clean tree), closing the loophole the P4/P5 exit
    gates leave open.
    """
    offenders = _god_object_classes()
    if offenders:
        raise AssertionError(
            f"god-object class(es) in blessed trees "
            f"(> {MAX_CLASS_METHODS} methods):\n"
            + "\n".join(f"  {rel}::{cls}: {m} methods" for rel, cls, m in offenders)
        )
