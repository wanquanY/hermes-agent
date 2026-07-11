from __future__ import annotations

from pathlib import Path

from hermes_agent.repositories.agent_profile_repo import AgentProfileRepoImpl
from hermes_agent.storage.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_agent_profile_facade_module_is_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "agent_profile_facade.py"
    ).exists()


def test_legacy_state_store_does_not_compose_agent_profile_facade() -> None:
    source = (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").read_text()
    assert "AgentProfileStateMixin" not in source
    assert "agent_profile_facade" not in source


def test_cli_store_owns_agent_profiles_through_repository(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.profiles, AgentProfileRepoImpl)
        assert not hasattr(store, "get_agent_profile")
        assert not hasattr(store, "upsert_agent_profile")
    finally:
        store.close()
