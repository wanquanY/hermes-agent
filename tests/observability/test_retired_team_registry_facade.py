from __future__ import annotations

from pathlib import Path

from hermes_agent.repositories.team_registry_repo import TeamRegistryRepo
from hermes_agent.storage.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_team_registry_facade_module_is_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "team_registry_facade.py"
    ).exists()


def test_legacy_state_store_does_not_compose_team_registry_facade() -> None:
    source = (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").read_text()
    assert "TeamRegistryStateMixin" not in source
    assert "team_registry_facade" not in source


def test_cli_store_owns_team_registry_through_repository(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.teams, TeamRegistryRepo)
        assert not hasattr(store, "get_agent_team")
        assert not hasattr(store, "upsert_agent_team")
    finally:
        store.close()
