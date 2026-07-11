from __future__ import annotations

from hermes_agent.storage.cli_session_store import open_cli_session_store


def test_update_source_projects_session_and_index(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", source="tui")

        changed = db.sessions.update_source("session-1", "team_mission")

        assert changed == 1
        assert db.sessions.get("session-1")["source"] == "team_mission"
        assert db.session_index.get("session-1")["source"] == "team_mission"
        assert db.session_index.get("session-1")["conversation_kind"] == "team"
    finally:
        db.close()


def test_scoped_system_prompt_isolated_from_conversation_prompt(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", source="team_mission")
        db.sessions.update_system_prompt("session-1", "conversation prompt")

        db.sessions.update_scoped_system_prompt(
            "session-1",
            "member-chat:session-1:frontend",
            "member prompt",
        )

        assert (
            db.sessions.get_scoped_system_prompt(
                "session-1",
                "member-chat:session-1:frontend",
            )
            == "member prompt"
        )
        assert db.sessions.get("session-1")["system_prompt"] == "conversation prompt"
    finally:
        db.close()
