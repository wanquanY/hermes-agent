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
      — relocating the legacy GatewayRunner (18277 lines / **209 methods**)
      into ``hermes_gateway/runner.py`` passes while the monolith survives
      under a new name.

This gate uses **method count per class** — a behavioral, name- and
path-independent god-object signal. A class with an extreme number of
methods is a god-object regardless of what it is called or where it
lives, so a relocated monolith trips it immediately.

MIXIN-RECOMPOSITION VECTOR (audit 2026-07-09): a subtler fake-out than
relocation defeats the per-class body count entirely. Shard the
209-method ``GatewayRunner`` across 46 ``*Mixin`` classes (each ~3
methods, each in its own <800-line file) and recompose them with multiple
inheritance::

    class GatewayRunner(GatewayApprovalCommandMixin, ...46 mixins...): ...

Every file is small, every CLASS body is under the threshold, ``grep``
sees dozens of tidy modules — but the *runtime object* still exposes ~214
distinct methods on one ``self``, all sharing state, none independently
testable (a mixin reaches into ``self.session_store._entries`` etc.
defined by its siblings). The source is sharded; the god-object is fully
intact. ``_composed_method_surface`` expands the local MRO to measure the
real per-instance method surface, so this recomposition trips the gate
the same way a raw monolith does. Fix by extracting real collaborators
that the runner *delegates to* (composition), not mixins it *is*
(inheritance).

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
# hermes_gateway/runner.py's new home.
_BLESSED_TREES = ("hermes_agent", "channels", "hermes_gateway")

_EXCLUDE_PREFIXES = ("hermes_agent/storage/migrations/",)


def test_retired_state_mixin_forwarding_package_has_no_source_modules() -> None:
    forwarding_dir = REPO_ROOT / "hermes_agent" / "storage" / "state_mixins"
    assert not list(forwarding_dir.glob("*.py"))


def test_unreferenced_top_level_state_facade_modules_are_absent() -> None:
    retired_modules = {
        "hermes_state_activities.py",
        "hermes_state_agent_profiles.py",
        "hermes_state_branch.py",
        "hermes_state_member_chat.py",
        "hermes_state_participants.py",
        "hermes_state_run_event_codec.py",
        "hermes_state_run_event_index.py",
        "hermes_state_run_event_reference.py",
        "hermes_state_runs.py",
        "hermes_state_runtime.py",
        "hermes_state_team_capabilities.py",
        "hermes_state_team_registry.py",
        "hermes_state_tool_events.py",
    }
    assert not {path.name for path in REPO_ROOT.iterdir()} & retired_modules


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


# --- Mixin-recomposition detector (composed per-instance method surface) ----
#
# The per-class body count above is blind to a monolith sharded across
# mixins and recomposed via multiple inheritance. This second detector
# resolves the *local* MRO — every base class defined in the trees we
# scan — and counts the DISTINCT method names an instance would expose.
# GatewayRunner(46 mixins) = ~214 distinct methods = still a god-object,
# no matter that each mixin body is tiny.
#
# The composition ROOT (``GatewayRunner``) currently still lives in the
# blessed ``hermes_gateway/`` tree after P5. We therefore index blessed trees
# and expand local MRO surfaces so a mixin-only textual split still counts as
# the same production god-object.


def _iter_composition_index_files():
    yield from _iter_blessed_files()
    legacy_root = REPO_ROOT / "gateway" / "run.py"
    if legacy_root.exists():
        yield legacy_root, legacy_root.relative_to(REPO_ROOT).as_posix()


def _local_class_index() -> dict[str, list[tuple[str, list[str], list[str]]]]:
    """Map simple class name -> list of (rel_path, method_names, base_names).

    Bases are recorded by simple name (``Name.id`` or ``Attribute.attr``)
    so local mixins resolve; external/library bases simply won't be found
    in the index and contribute nothing.
    """
    index: dict[str, list[tuple[str, list[str], list[str]]]] = {}
    for path, rel in _iter_composition_index_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            method_names = [
                c.name
                for c in node.body
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            base_names: list[str] = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    base_names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    base_names.append(b.attr)
            index.setdefault(node.name, []).append((rel, method_names, base_names))
    return index


def _composed_method_surface(
    class_name: str, index: dict, _seen: set[str] | None = None
) -> set[str]:
    """Distinct method names on an instance of ``class_name``, expanding
    local base classes (mixins) transitively. Unknown/external bases
    contribute nothing — we only measure sharding within the scanned trees.
    """
    if _seen is None:
        _seen = set()
    if class_name in _seen:
        return set()
    _seen.add(class_name)
    names: set[str] = set()
    for _rel, method_names, base_names in index.get(class_name, []):
        names.update(method_names)
        for base in base_names:
            names |= _composed_method_surface(base, index, _seen)
    return names


# Channel adapters legitimately inherit a large SHARED base
# (``BasePlatformAdapter``, ~82 methods) shared across ~20 platforms —
# that is ordinary template-method inheritance, not god-object sharding,
# and the manifest's Rebuild List explicitly accepts adapter size as
# separate deferred debt (and the user asked not to touch channel code).
# The composed arm therefore excludes ``channels/`` and targets exactly
# the live composed runtime objects this refactor is chartered to eliminate.
_COMPOSED_EXCLUDE_TREES = ("channels/",)

_COMPOSED_ALLOWLIST_CLASSES = frozenset()


def _composed_god_objects() -> list[tuple[str, str, int]]:
    """Classes that RECOMPOSE local mixins into a >threshold method surface.

    Filters to classes that inherit at least one locally-defined base, so
    plain single-class god-objects (already covered by
    ``_god_object_classes``) are not double-reported here. Channel adapters
    are excluded (shared-base inheritance = legit, out of scope).
    """
    index = _local_class_index()
    out: list[tuple[str, str, int]] = []
    for name, defs in index.items():
        rel = defs[0][0]
        if any(rel.startswith(t) for t in _COMPOSED_EXCLUDE_TREES):
            continue
        if name in _COMPOSED_ALLOWLIST_CLASSES:
            continue
        composes_local = any(
            any(b in index for b in base_names) for _rel, _m, base_names in defs
        )
        if not composes_local:
            continue
        surface = _composed_method_surface(name, index)
        if len(surface) > MAX_CLASS_METHODS:
            out.append((rel, name, len(surface)))
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
        "Anti-relocation reality signal. Flips to xpassed when no remaining "
        "runtime object exceeds the method threshold and no later relocation "
        "reintroduces a monolith. "
        "Fix by decomposing, never by raising the "
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


def test_composed_surface_detector_catches_a_synthetic_mixin_split():
    """Meta — the composed-surface detector must see through a mixin split.

    A monolith sharded into two tiny mixins and recomposed has a small
    per-class body but a full composed surface. This locks the detector so
    a future edit can't silently regress it back to body-only counting.
    """
    index = {
        "AMixin": [("a.py", [f"m{i}" for i in range(3)], [])],
        "BMixin": [("b.py", [f"n{i}" for i in range(3)], [])],
        "God": [("g.py", ["own"], ["AMixin", "BMixin"])],
    }
    surface = _composed_method_surface("God", index)
    assert len(surface) == 7, (
        f"composed surface should union own+mixin methods (expected 7, "
        f"got {len(surface)}) — detector regressed to body-only counting"
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Mixin-recomposition reality signal (audit 2026-07-09). Currently "
        "trips on GatewayRunner — 46 *Mixin classes recomposed via multiple "
        "inheritance into a ~214-method single runtime object. Each mixin "
        "body is tiny (passes the per-class gate) but the god-object is "
        "fully intact per-instance. Flips to xpassed when GatewayRunner is "
        "decomposed into real collaborators it DELEGATES to, not mixins it "
        "IS. Fix by composition, never by raising the threshold or splitting "
        "into yet more mixins."
    ),
)
def test_no_mixin_recomposed_god_object():
    """spec §12 anti-relocation gate (composed-surface arm) — no class may
    recompose local mixins into a >threshold per-instance method surface.

    Closes the loophole the body-count gate leaves open: sharding a
    monolith across N mixins and reassembling it with multiple inheritance
    keeps every file/class small while the runtime object stays a
    god-object.
    """
    offenders = _composed_god_objects()
    if offenders:
        raise AssertionError(
            f"mixin-recomposed god-object(s) — composed method surface "
            f"> {MAX_CLASS_METHODS}:\n"
            + "\n".join(
                f"  {rel}::{cls}: {m} distinct methods across local MRO"
                for rel, cls, m in offenders
            )
        )
