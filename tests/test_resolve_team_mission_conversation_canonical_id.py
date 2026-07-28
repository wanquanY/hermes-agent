from __future__ import annotations

import importlib
from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway


def _seed_team_conversation(
    db: CliSessionStore,
    *,
    conversation_id: str = "conversation-1",
    conversation_session_id: str = "team-session-1",
    team_id: str = "team-1",
    mission_id: str = "mission-1",
) -> None:
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        team_id=team_id,
        title="团队会话",
        active_mission_id=mission_id,
    )
    db.upsert_team_mission(
        mission_id=mission_id,
        conversation_id=conversation_id,
        team_id=team_id,
        title="团队任务",
        objective="验证 canonical conversation id",
        status="active",
        leader_session_id=conversation_session_id,
        metadata={"conversationTeamSessionId": conversation_session_id},
    )


def _assert_canonical_conversation(conversation: dict, *, conversation_id: str, conversation_session_id: str, team_id: str) -> None:
    assert conversation["conversation_id"] == conversation_id
    assert conversation["conversation_session_id"] == conversation_session_id
    assert conversation["team_id"] == team_id


def test_resolve_normal_team_mission_returns_canonical_conversation_id(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    _seed_team_conversation(db)

    resolved = db.resolve_team_mission_conversation("conversation-1")

    _assert_canonical_conversation(
        resolved["conversation"],
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
        team_id="team-1",
    )
    assert resolved["mission"]["mission_id"] == "mission-1"


def test_resolve_member_chat_only_mission_returns_conversation_id(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="member-chat-mission-1",
        conversation_id="team-conversation-member-1",
        team_id="team-1",
        title="成员私聊容器",
        objective="隐藏 member chat 容器",
        status="active",
        leader_session_id="team-session-member-1",
        metadata={"member_chat_only": True, "conversationTeamSessionId": "team-session-member-1"},
    )

    resolved = db.resolve_team_mission_conversation("team-conversation-member-1")

    _assert_canonical_conversation(
        resolved["conversation"],
        conversation_id="team-conversation-member-1",
        conversation_session_id="team-session-member-1",
        team_id="team-1",
    )
    assert resolved["mission"] == {}


def test_resolve_with_conversation_session_id_input_returns_conversation_id(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    _seed_team_conversation(db)

    resolved = db.resolve_team_mission_conversation("team-session-1")

    _assert_canonical_conversation(
        resolved["conversation"],
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
        team_id="team-1",
    )


def test_resolve_with_team_conversation_prefix_identifier_returns_id(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    _seed_team_conversation(
        db,
        conversation_id="team-conversation-abc123",
        conversation_session_id="team-session-team-conversation-abc123",
        team_id="team-prefix",
        mission_id="mission-prefix",
    )

    resolved = db.resolve_team_mission_conversation("team-conversation-abc123")

    _assert_canonical_conversation(
        resolved["conversation"],
        conversation_id="team-conversation-abc123",
        conversation_session_id="team-session-team-conversation-abc123",
        team_id="team-prefix",
    )


def test_resolve_missing_conversation_returns_explicit_error_not_silent_empty(monkeypatch, tmp_path: Path):
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["team_mission.conversation.resolve"](
        1,
        {"identifier": "missing-conversation"},
    )

    assert response["error"]["code"] == 4040
    assert response["error"]["message"] == "team mission conversation not found"


def test_render_snapshot_rejects_resolve_response_with_empty_conversation_session_id(monkeypatch):
    from tui_gateway import server

    importlib.import_module("tui_gateway.methods.conversation_render_snapshot")

    def _bad_resolve(rid, params):
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "conversation": {
                    "conversation_id": "team-conversation-bad",
                    "conversation_session_id": "",
                    "team_id": "team-bad",
                },
                "mission": {},
                "graph": {},
            },
        }

    monkeypatch.setitem(server._methods, "team_mission.conversation.resolve", _bad_resolve)

    response = server._methods["team_mission.conversation.render"](
        1,
        {"identifier": "team-conversation-bad"},
    )

    assert response["error"]["code"] == 5008
    assert response["error"]["message"] == (
        "team_mission resolve returned conversation without conversation_session_id"
    )
