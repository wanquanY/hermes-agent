from __future__ import annotations

import importlib
from pathlib import Path

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway import server


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _setup_gateway_db(monkeypatch, tmp_path: Path) -> CliSessionStore:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = _db(tmp_path)
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    monkeypatch.setattr(session_methods, "_SESSION_INDEX_RECONCILED", False)
    return db


def _index_rows(db: CliSessionStore) -> dict[str, dict]:
    return {
        item["session_id"]: item
        for item in db.session_index.list(include_transient=True)["sessions"]
    }


def _gateway_index_rows() -> dict[str, dict]:
    response = server._methods["session.index.list"](
        "conversation-kind-authoritative",
        {"includeTransient": True},
    )
    assert "error" not in response
    return {item["id"]: item for item in response["result"]["sessions"]}


def test_session_index_conversation_kind_direct_for_plain_session(tmp_path: Path) -> None:
    db = _db(tmp_path)

    db.session_index.upsert(
        session_id="plain-session",
        source="cli",
        title="Plain",
        conversation_kind="direct",
        started_at=1,
        updated_at=1,
    )

    assert _index_rows(db)["plain-session"]["conversation_kind"] == "direct"


def test_session_index_conversation_kind_team_for_team_mission(tmp_path: Path) -> None:
    db = _db(tmp_path)

    db.upsert_team_mission_conversation(
        conversation_id="conversation-team",
        conversation_session_id="team-session",
        team_id="team-1",
        title="Team",
        active_mission_id="mission-1",
        created_at=1,
        updated_at=2,
    )

    row = _index_rows(db)["team-session"]
    assert row["conversation_kind"] == "team"
    assert row["active_mission_id"] == "mission-1"


def test_conversation_kind_field_decoupled_from_active_mission_id(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-with-mission",
        conversation_session_id="separate-team-session",
        team_id="team-1",
        title="Team",
        active_mission_id="mission-active",
        created_at=1,
        updated_at=2,
    )
    db.session_index.upsert(
        session_id="direct-session",
        source="cli",
        session_kind="hermes_session",
        conversation_kind="direct",
        conversation_id="conversation-with-mission",
        title="Direct",
        started_at=3,
        updated_at=3,
    )

    row = _index_rows(db)["direct-session"]
    assert row["conversation_kind"] == "direct"
    assert row["active_mission_id"] == "mission-active"


def test_team_conversation_with_no_active_mission_still_kind_team(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)

    db.upsert_team_mission_conversation(
        conversation_id="conversation-no-active",
        conversation_session_id="team-session-no-active",
        team_id="team-1",
        title="Team",
        active_mission_id="",
        created_at=1,
        updated_at=2,
    )

    row = _index_rows(db)["team-session-no-active"]
    assert row["conversation_kind"] == "team"
    assert row["active_mission_id"] == ""


def test_direct_conversation_never_returns_kind_team_even_if_active_mission_set(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = _setup_gateway_db(monkeypatch, tmp_path)
    db.session_index.upsert(
        session_id="direct-with-mission",
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="direct",
        team_id="team-1",
        mission_id="mission-active",
        title="Direct",
        preview="Still direct",
        started_at=1,
        updated_at=1,
    )

    row = _gateway_index_rows()["direct-with-mission"]
    assert row["conversation_kind"] == "direct"
    assert row["mission_id"] == "mission-active"
    assert "team" not in row


def test_session_index_list_uses_conversation_kind_for_filtering(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = _setup_gateway_db(monkeypatch, tmp_path)
    db.session_index.upsert(
        session_id="direct-row",
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="direct",
        team_id="team-1",
        title="Direct",
        preview="direct",
        started_at=1,
        updated_at=1,
    )
    db.session_index.upsert(
        session_id="team-row",
        source="cli",
        session_kind="hermes_session",
        conversation_kind="team",
        title="Team",
        preview="team",
        started_at=2,
        updated_at=2,
    )

    direct_response = server._methods["session.index.list"](
        "conversation-kind-filter-direct",
        {"conversationKind": "direct", "includeTransient": True},
    )
    assert "error" not in direct_response
    assert [item["id"] for item in direct_response["result"]["sessions"]] == ["direct-row"]

    team_response = server._methods["session.index.list"](
        "conversation-kind-filter-team",
        {"conversation_kind": "team", "includeTransient": True},
    )
    assert "error" not in team_response
    assert [item["id"] for item in team_response["result"]["sessions"]] == ["team-row"]


def test_render_snapshot_does_not_infer_team_from_conversation_id_only(
    monkeypatch,
) -> None:
    captured: dict[str, dict] = {}

    def session_messages(_rid, params):
        captured["params"] = params
        return {
            "result": {
                "messages": [{"role": "user", "content": "hello"}],
                "runEvents": [],
                "pageInfo": {},
                "branchInfo": None,
            }
        }

    monkeypatch.setitem(server._methods, "session.messages", session_messages)

    response = server._methods["conversation.render_snapshot"](
        "conversation-kind-render",
        {"conversation_id": "direct-conversation"},
    )

    assert "error" not in response
    assert response["result"]["kind"] == "ordinary"
    assert captured["params"]["session_id"] == "direct-conversation"
