from __future__ import annotations

from types import SimpleNamespace

from tui_gateway import server
from tui_gateway.methods import session_history


class _Sessions:
    @staticmethod
    def get(session_id: str) -> dict:
        return {"id": session_id}

    @staticmethod
    def get_by_title(_title: str) -> None:
        return None


class _Messages:
    @staticmethod
    def page_as_conversation(*_args, **_kwargs) -> dict:
        return {"messages": [], "pageInfo": {}}


class _Branches:
    @staticmethod
    def get_session_branch_info(_session_id: str) -> dict:
        return {}


def test_session_messages_forwards_before_seq_to_run_event_read_model(
    monkeypatch,
) -> None:
    db = SimpleNamespace(
        sessions=_Sessions(),
        messages=_Messages(),
        branches=_Branches(),
    )
    calls: list[dict] = []

    def list_events(_db, session_id: str, **options) -> list[dict]:
        assert _db is db
        assert session_id == "session-1"
        calls.append(options)
        return [{"seq": 1}, {"seq": 2}]

    monkeypatch.setattr(session_history, "_get_db", lambda: db)
    monkeypatch.setattr(session_history, "list_runtime_events", list_events)

    response = server._methods["session.messages"](
        1,
        {
            "session_id": "session-1",
            "include_run_events": True,
            "before_seq": 3,
            "run_events_limit": 2,
        },
    )

    assert "error" not in response
    assert calls == [
        {
            "after_seq": 0,
            "before_seq": 3,
            "runtime_scope_key": "",
            "activity_id": "",
            "limit": 2,
        }
    ]
    assert response["result"]["runEvents"] == [{"seq": 1}, {"seq": 2}]
    assert response["result"]["maxSeq"] == 2


def test_session_messages_backfills_team_projection_before_paging(monkeypatch) -> None:
    order: list[str] = []

    class TeamSessions(_Sessions):
        @staticmethod
        def get(session_id: str) -> dict:
            return {"id": session_id, "conversation_kind": "team"}

    class TeamMessages(_Messages):
        @staticmethod
        def page_as_conversation(*_args, **_kwargs) -> dict:
            order.append("page")
            return {"messages": [], "pageInfo": {}}

    class TeamTranscriptProjections:
        @staticmethod
        def backfill(session_ids: list[str]) -> dict[str, int]:
            order.append("backfill")
            assert session_ids == ["team-session-1"]
            return {"projected_messages": 1, "merged_artifacts": 0}

    db = SimpleNamespace(
        sessions=TeamSessions(),
        messages=TeamMessages(),
        branches=_Branches(),
        team_transcript_projections=TeamTranscriptProjections(),
    )
    monkeypatch.setattr(session_history, "_get_db", lambda: db)

    response = server._methods["session.messages"](
        1,
        {"session_id": "team-session-1"},
    )

    assert "error" not in response
    assert order == ["backfill", "page"]
