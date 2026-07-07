"""Phase I — silent-swallow AST lint.

The scanner flags ``except: pass`` and ``except Exception: pass`` under
``hermes_agent/`` — spec §J11 mandates zero occurrences after Phase I.
Legacy code paths (``hermes_state*.py``, ``tui_gateway/*.py``, root
``gateway/*.py``) are grandfathered until Phase D5 / J landing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_agent.observability.silent_swallow_lint import (
    SilentSwallowFinding,
    scan_paths,
    scan_source,
)


HERMES_AGENT_ROOT = Path(__file__).resolve().parents[2] / "hermes_agent"


def test_scanner_flags_bare_except_pass():
    src = """
def demo():
    try:
        risky()
    except:
        pass
"""
    findings = scan_source(src, filename="demo.py")
    assert len(findings) == 1
    assert findings[0].exception_type == "bare"
    assert findings[0].line == 5


def test_scanner_flags_broad_exception_pass():
    src = """
def demo():
    try:
        risky()
    except Exception:
        pass
"""
    findings = scan_source(src, filename="demo.py")
    assert len(findings) == 1
    assert findings[0].exception_type == "Exception"


def test_scanner_allows_recoverable_error_pass():
    """spec-approved swallow — RecoverableError is itself the signal."""
    src = """
from hermes_agent.observability import RecoverableError
def demo():
    try:
        risky()
    except RecoverableError:
        pass
"""
    findings = scan_source(src, filename="demo.py")
    assert findings == []


def test_scanner_ignores_handler_with_real_body():
    src = """
def demo():
    try:
        risky()
    except Exception:
        logger.warning("swallowed")
"""
    findings = scan_source(src, filename="demo.py")
    assert findings == []


def test_scanner_treats_double_pass_as_silent():
    src = """
def demo():
    try:
        risky()
    except ValueError:
        pass
        pass
"""
    findings = scan_source(src, filename="demo.py")
    assert len(findings) == 1


def test_hermes_agent_root_stays_silent_swallow_free():
    """Guard rail — new code under ``hermes_agent/`` must not add silent swallow.

    Legacy code lives outside this root and remains untouched until D5 / J.
    """
    assert HERMES_AGENT_ROOT.exists(), (
        "hermes_agent/ must exist — Phase 0 landing failed?"
    )
    findings = scan_paths([HERMES_AGENT_ROOT])
    if findings:
        formatted = "\n".join(
            f"  {f.file}:{f.line}  except {f.exception_type}: pass" for f in findings
        )
        pytest.fail(f"silent swallow inside hermes_agent/ (spec §J11):\n{formatted}")
