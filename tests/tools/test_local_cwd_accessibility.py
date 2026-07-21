"""Regression tests for inaccessible local terminal working directories."""

import os

from tools.environments.local import _cwd_usable, _resolve_safe_cwd


def test_resolve_safe_cwd_skips_existing_but_unusable_directory(monkeypatch, tmp_path):
    denied = tmp_path / "rootlike"
    denied.mkdir()
    real_access = os.access

    def fake_access(candidate, mode):
        if os.fspath(candidate) == os.fspath(denied) and mode == os.X_OK:
            return False
        return real_access(candidate, mode)

    monkeypatch.setattr(os, "access", fake_access)

    assert _cwd_usable(str(denied)) is False
    assert _resolve_safe_cwd(str(denied)) == str(tmp_path)


def test_resolve_safe_cwd_preserves_accessible_directory(tmp_path):
    assert _cwd_usable(str(tmp_path)) is True
    assert _resolve_safe_cwd(str(tmp_path)) == str(tmp_path)
