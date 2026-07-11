from __future__ import annotations

from pathlib import Path

from hermes_agent.domain.session_branch_service import SessionBranchService
from hermes_agent.storage.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_branch_facade_module_is_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "branch_facade.py"
    ).exists()


def test_legacy_state_store_does_not_compose_branch_facade() -> None:
    source = (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").read_text()
    assert "BranchStateMixin" not in source
    assert "branch_facade" not in source


def test_cli_store_owns_branching_through_domain_service(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.branches, SessionBranchService)
        assert not hasattr(store, "get_session_branch_info")
        assert not hasattr(store, "branch_session")
    finally:
        store.close()
