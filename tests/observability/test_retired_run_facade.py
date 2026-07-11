from __future__ import annotations

from pathlib import Path

from hermes_agent.domain.run_event_maintenance_service import (
    RunEventMaintenanceService,
)
from hermes_agent.domain.run_service import RunService
from hermes_agent.read_models.tool_events import ToolEventProjectionReadModel
from hermes_agent.storage.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_run_facade_module_is_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "run_facade.py"
    ).exists()


def test_legacy_state_store_does_not_compose_run_facade() -> None:
    source = (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").read_text()
    assert "RunStateMixin" not in source
    assert "run_facade" not in source


def test_cli_store_composes_explicit_run_owners(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.runs, RunService)
        assert isinstance(store.run_event_maintenance, RunEventMaintenanceService)
        assert isinstance(store.tool_event_projection, ToolEventProjectionReadModel)
        assert not hasattr(store, "upsert_run")
        assert not hasattr(store, "append_run_event")
        assert not hasattr(store, "list_run_events")
    finally:
        store.close()
