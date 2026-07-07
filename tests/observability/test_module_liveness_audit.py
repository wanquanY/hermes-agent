"""Phase J prep — gateway/ liveness audit tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hermes_agent.observability import (
    LivenessAudit,
    ModuleLivenessReport,
    audit_liveness,
)


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_audit_marks_unimported_target_module_as_dead():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "gateway" / "orphan.py", "# nobody imports me\n")
        _write(root / "tui_gateway" / "server.py", "from tui_gateway.session import x\n")

        audit = audit_liveness(root, live_roots=("tui_gateway",))
        assert isinstance(audit, LivenessAudit)
        assert audit.summary() == {"total": 1, "live": 0, "dead": 1}
        dead_names = {r.module for r in audit.dead}
        assert "gateway.orphan" in dead_names


def test_audit_marks_imported_target_module_as_live():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "gateway" / "channel.py", "def register(): pass\n")
        _write(
            root / "tui_gateway" / "server.py",
            "from gateway.channel import register\n",
        )

        audit = audit_liveness(root, live_roots=("tui_gateway",))
        assert audit.summary() == {"total": 1, "live": 1, "dead": 0}
        live = audit.live[0]
        assert live.module == "gateway.channel"
        assert live.imported_by == ("tui_gateway.server",)


def test_audit_handles_nested_gateway_subpackages():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "channels" / "platforms" / "__init__.py", "")
        _write(root / "channels" / "platforms" / "telegram.py", "def send(): pass\n")
        _write(root / "channels" / "dead.py", "# no importer\n")
        _write(
            root / "hermes_agent" / "consumer.py",
            "from channels.platforms.telegram import send\n",
        )

        audit = audit_liveness(
            root,
            target_package="channels",
            live_roots=("hermes_agent",),
        )
        by_module = {r.module: r for r in audit.reports}
        # __init__ collapses to the package name.
        assert by_module["channels.platforms.telegram"].is_live is True
        assert by_module["channels.platforms"].is_live is True  # via prefix match
        assert by_module["channels.dead"].is_live is False


def test_audit_target_missing_root_returns_empty_report():
    with tempfile.TemporaryDirectory() as td:
        audit = audit_liveness(Path(td), live_roots=("tui_gateway",))
        assert audit.summary() == {"total": 0, "live": 0, "dead": 0}


def test_audit_ignores_pycache_and_non_python_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "gateway" / "real.py", "x = 1\n")
        _write(root / "gateway" / "__pycache__" / "cached.py", "# should be skipped\n")
        _write(root / "gateway" / "note.txt", "not python\n")
        _write(root / "tui_gateway" / "user.py", "from gateway.real import x\n")

        audit = audit_liveness(root, live_roots=("tui_gateway",))
        assert audit.summary()["total"] == 1
        assert audit.reports[0].module == "gateway.real"


def test_audit_live_module_via_from_style_import():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "gateway" / "utils" / "__init__.py", "def helper(): pass\n")
        _write(
            root / "hermes_agent" / "consumer.py",
            "from gateway.utils import helper\n",
        )

        audit = audit_liveness(root, live_roots=("hermes_agent",))
        by_module = {r.module: r for r in audit.reports}
        assert by_module["gateway.utils"].is_live is True


def test_audit_detects_function_body_lazy_import():
    """Regression — scanner used to miss imports inside function bodies.

    The Phase J audit was invalidated in loop iteration 13 because the old
    regex-based scanner ignored ``from channels.platforms.telegram import ...``
    when it lived inside an ``if platform.enabled:`` branch. This test
    locks in the AST walker's coverage of function-body imports.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "gateway" / "telegram.py", "def send(): pass\n")
        _write(
            root / "gateway" / "run.py",
            "def start_platforms():\n"
            "    if True:\n"
            "        from gateway.telegram import send\n"
            "        send()\n",
        )
        audit = audit_liveness(
            root, live_roots=(), include_self=True
        )
        by_module = {r.module: r for r in audit.reports}
        assert by_module["gateway.telegram"].is_live is True
        assert by_module["gateway.telegram"].imported_by == ("gateway.run",)


def test_audit_self_include_off_matches_legacy_behaviour():
    """With ``include_self=False`` the scanner reverts to external-only importers."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "gateway" / "helper.py", "def h(): pass\n")
        _write(
            root / "gateway" / "run.py",
            "from gateway.helper import h\n",
        )
        # Without self-include, run.py's import doesn't count.
        audit = audit_liveness(root, live_roots=(), include_self=False)
        assert audit.summary()["dead"] == 2

        audit_self = audit_liveness(root, live_roots=(), include_self=True)
        # helper is live via run, run itself has no importer.
        by_module = {r.module: r for r in audit_self.reports}
        assert by_module["gateway.helper"].is_live is True
        assert by_module["gateway.run"].is_live is False


def test_audit_on_real_repo_produces_report_without_errors():
    """Smoke — the audit runs cleanly against the real repo tree.

    We don't assert specific numbers because the tree drifts; we just want
    to know the tool doesn't crash on the actual code paths.
    """
    audit = audit_liveness()
    assert isinstance(audit, LivenessAudit)
    summary = audit.summary()
    assert summary["total"] >= 1
    assert summary["live"] + summary["dead"] == summary["total"]
    # Every report is well-formed.
    for report in audit.reports:
        assert isinstance(report, ModuleLivenessReport)
        assert report.file_path.startswith("gateway/")
        assert report.module.startswith("gateway")
