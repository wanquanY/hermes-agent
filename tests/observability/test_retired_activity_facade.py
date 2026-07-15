from __future__ import annotations

from pathlib import Path

from hermes_agent.application.activity_service import ActivityService
from hermes_agent.composition.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_activity_facade_module_is_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "activity_facade.py"
    ).exists()


def test_legacy_state_store_is_deleted() -> None:
    assert not (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").exists()


def test_cli_store_owns_activities_through_domain_service(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.activities, ActivityService)
        activity = store.activities.create(
            activity_id="activity-1",
            conversation_id="conversation-1",
            kind="chat",
            prompt_summary="Inspect architecture",
        )

        assert activity["activity_id"] == "activity-1"
        assert store.activities.get("activity-1")["prompt_summary"] == (
            "Inspect architecture"
        )
        assert not hasattr(store, "create_activity")
        assert not hasattr(store, "get_activity")
    finally:
        store.close()
