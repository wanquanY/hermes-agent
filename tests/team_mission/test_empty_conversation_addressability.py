from __future__ import annotations

import importlib

from hermes_agent.composition.cli_session_store import open_cli_session_store


def _assert_ok(response: dict) -> dict:
    assert "error" not in response, response.get("error")
    result = response.get("result")
    assert isinstance(result, dict)
    return result


def test_ensured_empty_conversation_is_addressable_before_history_visibility(
    monkeypatch,
    tmp_path,
):
    from tui_gateway import server

    conversation_methods = importlib.import_module(
        "hermes_team_mission.gateway.conversation_methods"
    )
    render_methods = importlib.import_module(
        "tui_gateway.methods.conversation_render_snapshot"
    )
    run_methods = importlib.import_module("tui_gateway.methods.run")
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(conversation_methods, "_get_db", lambda: db)
    monkeypatch.setattr(render_methods, "_get_db", lambda: db)
    monkeypatch.setattr(run_methods, "_get_db", lambda: db)
    monkeypatch.setattr(run_methods, "_db_for_stable_session", lambda _session_id: db)

    ensured = _assert_ok(
        server._methods["team_mission.conversation.ensure"](
            "ensure-empty-conversation",
            {
                "team_id": "team-product",
                "objective": "",
                "workspace": {
                    "workspace_id": "workspace-1",
                    "workspace_path": str(tmp_path),
                },
                "metadata": {"teamName": "Product Team"},
            },
        )
    )
    conversation_session_id = ensured["conversation_session_id"]

    # Addressability and history discoverability are intentionally different:
    # the stable identity is readable immediately, but an abandoned empty
    # shell does not pollute the user's conversation history.
    listed_before_message = _assert_ok(
        server._methods["team_mission.conversation.list"](
            "list-empty-conversations",
            {"team_id": "team-product", "lightweight": True},
        )
    )
    assert listed_before_message["conversations"] == []

    resolved = _assert_ok(
        server._methods["team_mission.conversation.resolve"](
            "resolve-empty-conversation",
            {"identifier": conversation_session_id},
        )
    )
    assert (
        resolved["conversation"]["conversation_session_id"] == conversation_session_id
    )
    assert resolved["messages"] == []

    rendered = _assert_ok(
        server._methods["conversation.render_snapshot"](
            "render-empty-conversation",
            {
                "stored_session_id": conversation_session_id,
                "session_id": conversation_session_id,
                "direction": "tail",
                "limit": 50,
            },
        )
    )
    assert rendered["kind"] == "team_mission"
    assert rendered["renderReady"] is True
    assert rendered["conversation_session_id"] == conversation_session_id
    assert rendered["messages"] == []

    subscribed = _assert_ok(
        server._methods["events.subscribe"](
            "subscribe-empty-conversation",
            {"conversation_session_id": conversation_session_id, "after_seq": 0},
        )
    )
    assert subscribed["conversation_session_id"] == conversation_session_id
    assert subscribed["events"] == []

    db.messages.append(conversation_session_id, "user", "First team message")
    listed_after_message = _assert_ok(
        server._methods["team_mission.conversation.list"](
            "list-nonempty-conversations",
            {"team_id": "team-product", "lightweight": True},
        )
    )
    assert [
        item["conversation_session_id"]
        for item in listed_after_message["conversations"]
    ] == [conversation_session_id]
