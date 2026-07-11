from __future__ import annotations

from pathlib import Path

from hermes_agent.domain.team_capability_service import TeamCapabilityService
from hermes_agent.storage.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_team_capability_facade_module_is_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "team_capability_facade.py"
    ).exists()


def test_legacy_state_store_does_not_compose_team_capability_facade() -> None:
    source = (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").read_text()
    assert "TeamCapabilityStateMixin" not in source
    assert "team_capability_facade" not in source


def test_cli_store_owns_team_capabilities_through_domain_service(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.team_capabilities, TeamCapabilityService)
        assert not hasattr(store, "get_team_capability_snapshot")
        assert not hasattr(store, "bind_team_capability_snapshot")
    finally:
        store.close()
