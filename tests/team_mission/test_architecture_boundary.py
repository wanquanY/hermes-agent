from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_team_mission_legacy_entrypoints_are_absent() -> None:
    assert not list(ROOT.glob("hermes_team_mission*.py"))
    assert not (ROOT / "hermes_state_team_missions.py").exists()
    assert not (ROOT / "tui_gateway" / "methods" / "team_mission.py").exists()
    assert not (ROOT / "tui_gateway" / "methods" / "team_mission_history.py").exists()
    assert not list((ROOT / "tui_gateway" / "services").glob("team_mission_*.py"))


def test_team_mission_production_code_does_not_import_legacy_boundaries() -> None:
    markers = (
        "tui_gateway.methods.team_mission",
        "tui_gateway.services.team_mission",
        'sys.modules.get("tools.team_mission',
    )
    for directory in ("dovie_extension", "hermes_team_mission", "tools", "tui_gateway"):
        for path in (ROOT / directory).rglob("*.py"):
            text = path.read_text()
            for marker in markers:
                assert marker not in text, f"{path.relative_to(ROOT)} still references {marker}"


def test_team_mission_tool_loaders_do_not_hold_business_logic() -> None:
    loaders = sorted((ROOT / "tools").glob("team_mission_*_tools.py"))
    assert loaders
    for path in loaders:
        lines = path.read_text().splitlines()
        assert len(lines) <= 10, f"{path.relative_to(ROOT)} must stay a thin registry loader"
        assert any("from hermes_team_mission.tools." in line for line in lines)


def test_team_mission_package_files_stay_below_file_size_boundary() -> None:
    for path in (ROOT / "hermes_team_mission").rglob("*.py"):
        line_count = len(path.read_text().splitlines())
        assert line_count <= 2000, f"{path.relative_to(ROOT)} has {line_count} lines"
