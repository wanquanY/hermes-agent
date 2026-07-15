"""Phase M — verify the drop-runtime_source_seq migration stays parked.

The migration MUST refuse to run while ``PENDING_FRONTEND_H3`` is True. Once
frontend Phase H3 lands, this test is intentionally the marker to flip: the
last commit that lands H3 will set the flag to False, at which point this
guard reverses (see the test body).
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


def _load_module():
    path = (
        Path(__file__).resolve().parents[1].parent
        / "hermes_agent"
        / "storage"
        / "migrations"
        / "0047_drop_runtime_source_seq.py"
    )
    spec = importlib.util.spec_from_file_location("phase_m_0047", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m0047 = _load_module()


def test_migration_is_versioned_47():
    assert m0047.version == 47


def test_migration_description_mentions_phase_m():
    assert "runtime_source_seq" in m0047.description
    assert "H3" in m0047.description or "DEBT-2" in m0047.description


def test_pending_flag_defaults_true_until_frontend_h3_lands():
    """The parking flag must default to True — turning it False before H3
    activates a destructive column drop against a still-consuming frontend.
    """
    assert m0047.PENDING_FRONTEND_H3 is True, (
        "PENDING_FRONTEND_H3 must remain True until frontend v3.1 Phase H3 "
        "lands (spec §12 Phase M). Flipping this without the frontend patch "
        "would silently break replay hydration."
    )


def test_apply_raises_frozen_migration_error_while_parked():
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(m0047.FrozenMigrationError, match="parked"):
            m0047.apply(conn.cursor())
    finally:
        conn.close()
