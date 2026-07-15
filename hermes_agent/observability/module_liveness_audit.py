"""Phase J prep — static liveness audit for the legacy ``gateway/`` tree.

Spec §12 Phase J plans to retire the root-level ``gateway/`` package (63
files at v3.0.2). Before deletion lands, we want a machine-checkable
inventory: for each ``gateway/**.py``, is it still imported by anything
under a "live" root (``tui_gateway/``, ``hermes_team_mission/``,
``dovie_extension/``, ``hermes_agent/``, ``agent/``, ``tools/``,
``plugins/``)?

Files that no live root imports are safe candidates for Phase J deletion.
Files that DO get imported must be relocated (spec suggests
``hermes_agent/gateway/platforms/``) before the parent directory can be
removed.

The audit does not delete anything — it just returns the classification.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


HERMES_AGENT_ROOT = Path(__file__).resolve().parents[2]

# Roots we consider "live" — anything imported by these still ships.
DEFAULT_LIVE_ROOTS = (
    "tui_gateway",
    "hermes_team_mission",
    "dovie_extension",
    "hermes_agent",
    "agent",
    "tools",
    "plugins",
    "acp_adapter",
    "acp_registry",
    "hermes_cli",
)

DEFAULT_TARGET_PACKAGE = "gateway"

# The target package itself is added to the importer scan so any lazy import
# ``from gateway.platforms.xxx import ...`` inside an entry-point module such
# as ``gateway/run.py`` marks the imported adapter as live. Without this, IM
# channel adapters wired via ``gateway/run.py`` at startup are misclassified
# as dead — see the invalidation warning in
# ``docs/audits/phase_j_gateway_liveness.md``.
DEFAULT_SELF_INCLUDE = True


@dataclass(frozen=True)
class ModuleLivenessReport:
    module: str                # dotted path, e.g. "hermes_gateway.channel_directory"
    file_path: str             # repo-relative path
    imported_by: tuple[str, ...]   # dotted paths of importers under live roots
    is_live: bool


@dataclass(frozen=True)
class LivenessAudit:
    target_package: str
    reports: tuple[ModuleLivenessReport, ...]

    @property
    def dead(self) -> tuple[ModuleLivenessReport, ...]:
        return tuple(r for r in self.reports if not r.is_live)

    @property
    def live(self) -> tuple[ModuleLivenessReport, ...]:
        return tuple(r for r in self.reports if r.is_live)

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.reports),
            "live": len(self.live),
            "dead": len(self.dead),
        }


def _iter_python_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return [
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def _path_to_module(path: Path, base: Path) -> str:
    rel = path.relative_to(base).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else ""


def _extract_imports(source: str) -> set[str]:
    """Return every dotted module string that appears in an import statement.

    Walks the full AST so imports inside function bodies, conditional
    branches, and lazy loaders are picked up too. The original regex-only
    implementation only matched module-top ``from`` / ``import`` lines and
    therefore missed lazy imports — most importantly
    ``gateway/run.py:6328-6401`` where the IM channel adapters
    (telegram / discord / whatsapp / slack / signal / homeassistant /
    email / sms / …) are loaded inside a function body based on runtime
    configuration.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # ``from a.b import c`` — record ``a.b`` (the target package)
            # AND ``a.b.c`` (each imported symbol as a potential submodule).
            module = node.module or ""
            if module:
                result.add(module)
                for alias in node.names:
                    if alias.name and alias.name != "*":
                        result.add(f"{module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    result.add(alias.name)
    return result


def _module_matches_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def audit_liveness(
    root: Path | None = None,
    *,
    target_package: str = DEFAULT_TARGET_PACKAGE,
    live_roots: tuple[str, ...] = DEFAULT_LIVE_ROOTS,
    include_self: bool = DEFAULT_SELF_INCLUDE,
) -> LivenessAudit:
    """Audit ``target_package`` for imports coming from ``live_roots`` code.

    ``include_self=True`` (default) also treats every file *inside*
    ``target_package`` as a possible importer. This catches entry-point
    modules like ``gateway/run.py`` that lazy-import their siblings — the
    child adapter is live because its parent entry point loads it, even
    though no external ``live_root`` references it directly.
    """

    base = Path(root) if root is not None else HERMES_AGENT_ROOT
    target_root = base / target_package
    target_files = list(_iter_python_files(target_root))

    target_modules = {
        _path_to_module(f, base): f for f in target_files
    }

    # Filter out empty module strings (defensive).
    target_modules = {m: f for m, f in target_modules.items() if m}

    live_files: list[Path] = []
    for live_root in live_roots:
        live_root_path = base / live_root
        live_files.extend(_iter_python_files(live_root_path))
    if include_self:
        live_files.extend(target_files)

    # Map: target_module → set(live_importer_dotted)
    reverse_index: dict[str, set[str]] = {m: set() for m in target_modules}

    for lf in live_files:
        try:
            source = lf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        importer_module = _path_to_module(lf, base)
        if not importer_module:
            continue
        # A module can't count as its own importer.
        for imported in _extract_imports(source):
            for tm in target_modules:
                if importer_module == tm:
                    continue
                if _module_matches_prefix(imported, tm):
                    reverse_index[tm].add(importer_module)

    reports = [
        ModuleLivenessReport(
            module=m,
            file_path=str(target_modules[m].relative_to(base)),
            imported_by=tuple(sorted(reverse_index[m])),
            is_live=bool(reverse_index[m]),
        )
        for m in sorted(target_modules.keys())
    ]

    return LivenessAudit(
        target_package=target_package,
        reports=tuple(reports),
    )
