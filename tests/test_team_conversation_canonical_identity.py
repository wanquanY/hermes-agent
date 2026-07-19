from pathlib import Path

from tests.team_mission_gateway_test_support import team_mission_gateway


def test_team_conversation_recall_uses_only_canonical_session_identity(
    monkeypatch,
    tmp_path: Path,
):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    db.sessions.create("team-session-1", source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="legacy-team-container-1",
        team_id="team-1",
        conversation_session_id="team-session-1",
        title="Canonical identity",
    )

    def recall_session(rid, params):
        assert params["session_id"] == "team-session-1"
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "recalled",
                "conversation_session_id": "team-session-1",
                "turn_id": params["turn_id"],
                "messages": [],
            },
        }

    monkeypatch.setitem(server._methods, "session.recall_turn", recall_session)

    response = server._methods["team_mission.conversation.recall_turn"](
        1,
        {
            "conversation_session_id": "team-session-1",
            "turn_id": "turn-1",
        },
    )

    assert "error" not in response
    result = response["result"]
    assert result["conversation_session_id"] == "team-session-1"
    assert "conversation_id" not in result
    assert result["recalled"]["messages"] == []
