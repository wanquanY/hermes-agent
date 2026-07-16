from __future__ import annotations

from pathlib import Path

from tools import hook_output_spill


def test_hook_config_defaults_and_overrides(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "hooks": {
                "output_spill": {
                    "max_chars": 50,
                    "preview_head": 7,
                    "preview_tail": 8,
                    "max_files": 4,
                    "max_age_hours": 2,
                    "directory": str(tmp_path),
                }
            }
        },
    )
    config = hook_output_spill.get_spill_config()
    assert config == {
        "enabled": True,
        "max_chars": 50,
        "preview_head": 7,
        "preview_tail": 8,
        "max_files": 4,
        "max_age_hours": 2,
        "directory": str(tmp_path),
    }


def test_disabled_hook_spill_is_explicit_passthrough(tmp_path: Path) -> None:
    raw = "x" * 500
    result = hook_output_spill.spill_if_oversized(
        raw,
        session_id="session",
        config={"enabled": False, "directory": str(tmp_path)},
    )
    assert result == raw


def test_enabled_hook_spill_returns_bounded_preview(tmp_path: Path) -> None:
    raw = "A" * 40 + "B" * 40
    result = hook_output_spill.spill_if_oversized(
        raw,
        session_id="session",
        config={
            "enabled": True,
            "max_chars": 20,
            "preview_head": 5,
            "preview_tail": 5,
            "max_files": 10,
            "max_age_hours": 1,
            "directory": str(tmp_path),
        },
    )
    assert result != raw
    assert "A" * 5 in result and "B" * 5 in result
    files = list((tmp_path / "session").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == raw
