from __future__ import annotations

from pathlib import Path

import pytest

from hermes_agent.storage.cli_session_store import open_cli_session_store
from tui_gateway.services.runtime_state import rebase_team_mission_workspace_paths


def _seed_workspace_state(db, *, workspace_path: str) -> None:
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Rebase workspace",
        workspace_path=workspace_path,
        status="running",
        leader_session_id="team-session-1",
    )
    db.ensure_team_mission_conversation(
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
        mission_id="mission-1",
        team_id="team-1",
        title="Conversation",
        workspace_path=workspace_path,
    )


def test_workspace_rebase_updates_mission_and_conversation_atomically(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        _seed_workspace_state(db, workspace_path="/old/workspace")

        result = rebase_team_mission_workspace_paths(
            old_path="/old/workspace",
            new_path="/new/workspace",
            db=db,
        )

        assert result == {
            "updates": {
                "team_missions": 1,
                "team_mission_conversations": 1,
            },
            "changed": 2,
        }
        assert db.team_mission_graphs.get_team_mission_graph("mission-1")["mission"]["workspace_path"] == "/new/workspace"
        assert db.get_team_mission_conversation("conversation-1")["workspace_path"] == "/new/workspace"
    finally:
        db.close()


def test_workspace_rebase_rolls_back_both_aggregates_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        _seed_workspace_state(db, workspace_path="/old/workspace")
        repository = db.team_mission_maintenance._repository  # noqa: SLF001 - transaction contract.
        original = repository.rebase_workspace_paths

        def fail_after_updates(old_path: str, new_path: str) -> dict[str, int]:
            original(old_path, new_path)
            raise RuntimeError("forced rebase failure")

        monkeypatch.setattr(repository, "rebase_workspace_paths", fail_after_updates)

        with pytest.raises(RuntimeError, match="forced rebase failure"):
            db.team_mission_maintenance.rebase_workspace_paths(
                "/old/workspace",
                "/new/workspace",
            )

        assert db.team_mission_graphs.get_team_mission_graph("mission-1")["mission"]["workspace_path"] == "/old/workspace"
        assert db.get_team_mission_conversation("conversation-1")["workspace_path"] == "/old/workspace"
    finally:
        db.close()
