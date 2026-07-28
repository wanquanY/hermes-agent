from __future__ import annotations

import importlib
import json

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway import server


def test_transport_cap_lowers_cursor_before_omitted_run_events() -> None:
    snapshot = importlib.import_module(
        "tui_gateway.methods.conversation_render_snapshot"
    )
    result = {
        "kind": "ordinary",
        "schemaVersion": "test",
        "renderReady": True,
        "messages": [],
        "runEvents": [
            {
                "seq": 10,
                "type": "message.delta",
                "payload": {"text": "a" * 20_000},
            },
            {
                "seq": 11,
                "type": "message.delta",
                "payload": {"text": "tail"},
            },
        ],
        "last_event_seq": 11,
        "lastEventSeq": 11,
        "pageInfo": {},
        "projection": {"source": "conversation.render_snapshot"},
    }

    capped = snapshot._cap_render_result(result, max_bytes=2_048)  # noqa: SLF001

    assert len(json.dumps(capped, ensure_ascii=False).encode("utf-8")) <= 2_048
    assert capped["transportTruncated"] is True
    assert capped["last_event_seq"] == 9
    assert capped["lastEventSeq"] == 9
    assert [event["seq"] for event in capped["runEvents"]] == [11]


def test_active_run_baseline_page_resumes_from_first_omitted_suffix(
    tmp_path,
    monkeypatch,
) -> None:
    snapshot = importlib.import_module(
        "tui_gateway.methods.conversation_render_snapshot"
    )
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = open_cli_session_store(tmp_path / "state.db")
    session_id = "ordinary-baseline-page-1"
    run_id = "run-baseline-page-1"
    turn_id = "turn-baseline-page-1"
    try:
        db.sessions.create(session_id=session_id, source="tui")
        db.runs.upsert(
            run_id=run_id,
            session_id=session_id,
            runtime_scope_key="profile:agent-default",
            turn_id=turn_id,
            execution_session_id="runtime-baseline-page-1",
            status="running",
        )
        for index, text in enumerate(("first", "second", "third")):
            db.runs.append_event(
                session_id,
                {
                    "type": "message.delta",
                    "session_id": "runtime-baseline-page-1",
                    "conversation_session_id": session_id,
                    "run_id": run_id,
                    "turn_id": turn_id,
                    "runtime_scope_key": "profile:agent-default",
                    "payload": {
                        "mode": "append",
                        "text": text,
                        "client_message_id": f"{turn_id}:assistant-segment:{index}",
                        "participant_id": "agent:agent-default",
                    },
                },
            )
        monkeypatch.setattr(snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setattr(
            snapshot,
            "_RENDER_RUN_BASELINE_LIMIT_PER_RUN",
            2,
        )

        response = server._methods["conversation.render_snapshot"](
            1,
            {
                "session_id": session_id,
                "limit": 50,
                "includeRunEvents": True,
                "runEventsLimit": 1,
            },
        )

        result = response["result"]
        recovered = [
            event for event in result["runEvents"]
            if event["run_id"] == run_id
        ]
        assert [event["payload"]["text"] for event in recovered] == [
            "first",
            "second",
        ]
        assert result["last_event_seq"] == recovered[-1]["seq"]
        assert result["lastEventSeq"] == recovered[-1]["seq"]
        replayed = db.runs.list_events(
            session_id,
            after_seq=result["last_event_seq"],
        )
        assert [event["payload"]["text"] for event in replayed] == ["third"]
    finally:
        db.close()
