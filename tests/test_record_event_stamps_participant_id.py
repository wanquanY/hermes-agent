from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from hermes_agent.storage.migrations import CURRENT_SCHEMA_VERSION
from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.services import run_control


def _db(tmp_path: Path, session_id: str = "conv-1") -> CliSessionStore:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(session_id, source="team_mission", transient=False)
    return db


def _context(
    *,
    session_id: str = "conv-1",
    participant_id: str = "member:alice",
) -> RunContext:
    return RunContext(
        conversation_session_id=session_id,
        participant_id=participant_id,
        activity_id="act-member_chat:conv-1:alice",
        activity_kind="member_chat",
        execution_scope_key="member-chat:conv-1:alice",
        control_home="/tmp/hermes-control",
        execution_home="/tmp/hermes-execution",
    )


def _event(run_id: str = "run-1", **extra: Any) -> dict[str, Any]:
    payload = {"text": "hello", "status": "complete"}
    payload.update(extra.pop("payload", {}))
    return {
        "type": "message.complete",
        "session_id": "conv-1",
        "conversation_session_id": "conv-1",
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "seq": 1,
        "payload": payload,
        **extra,
    }


def test_run_events_schema_has_participant_id_column(tmp_path: Path):
    db = _db(tmp_path)
    try:
        columns = {
            row["name"]: row
            for row in db._conn.execute("PRAGMA table_info(run_events)").fetchall()  # noqa: SLF001
        }
        assert columns["participant_id"]["type"] == "TEXT"
        assert columns["participant_id"]["notnull"] == 1
        version = db._conn.execute("SELECT version FROM schema_version").fetchone()["version"]  # noqa: SLF001
        assert version == CURRENT_SCHEMA_VERSION
    finally:
        db.close()


def test_append_run_event_writes_participant_id(tmp_path: Path):
    db = _db(tmp_path)
    try:
        db.runs.append_event("conv-1", _event(), participant_id="member:alice")

        row = db._conn.execute(  # noqa: SLF001
            "SELECT participant_id, event_json FROM run_events WHERE session_id = ?",
            ("conv-1",),
        ).fetchone()
        assert row["participant_id"] == "member:alice"
        assert json.loads(row["event_json"])["participant_id"] == "member:alice"
        assert db.runs.list_events("conv-1")[0]["payload"]["participant_id"] == "member:alice"
    finally:
        db.close()


def test_record_event_extracts_participant_id_from_run_context(tmp_path: Path):
    db = _db(tmp_path)
    try:
        run_control.record_event(_event(), db=db, run_context=_context(participant_id="member:alice"))

        stored = db.runs.list_events("conv-1", run_id="run-1")[0]
        assert stored["participant_id"] == "member:alice"
        assert stored["participantId"] == "member:alice"
        assert stored["payload"]["participant_id"] == "member:alice"
    finally:
        db.close()


def test_record_event_falls_back_to_scope_member_id_for_member_chat(tmp_path: Path):
    db = _db(tmp_path)
    try:
        run_control.record_event(
            _event(
                "run-member",
                runtime_scope_key="member-chat:conv-1:member-bob",
                payload={"runtime_scope_key": "member-chat:conv-1:member-bob"},
            ),
            db=db,
        )

        stored = db.runs.list_events("conv-1", run_id="run-member")[0]
        assert stored["participant_id"] == "member:member-bob"
        assert stored["payload"]["participant_id"] == "member:member-bob"
    finally:
        db.close()


def test_publish_recorded_event_includes_participant_id_in_payload(monkeypatch):
    delivered: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []

    class _Transport:
        def write(self, obj: dict) -> bool:
            return True

        def close(self) -> None:
            return None

    transport = _Transport()

    def _record_event(params: dict[str, Any], **_kwargs: Any) -> list[Any]:
        recorded.append(params)
        return [transport]

    def _write_event(_transport: Any, event: dict[str, Any]) -> bool:
        delivered.append(event)
        return True

    monkeypatch.setattr(run_control, "record_event", _record_event)
    monkeypatch.setattr(run_control, "_write_event", _write_event)

    run_control.publish_recorded_event(
        _event("run-publish"),
        run_context=_context(participant_id="member:publisher"),
    )

    assert recorded[0]["participant_id"] == "member:publisher"
    assert recorded[0]["payload"]["participant_id"] == "member:publisher"
    assert delivered[0]["participant_id"] == "member:publisher"
    assert delivered[0]["participantId"] == "member:publisher"
    assert delivered[0]["payload"]["participant_id"] == "member:publisher"


def test_render_snapshot_messages_include_message_row_participant_id(tmp_path: Path, monkeypatch):
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")

    db = _db(tmp_path, "team-session-1")
    try:
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="member reply",
            participant_id="member:renderer",
            metadata={"run_id": "run-render", "turn_id": "turn-render"},
        )
        db.runs.append_event(
            "team-session-1",
            {
                **_event("run-render"),
                "session_id": "team-session-1",
                "conversation_session_id": "team-session-1",
                "turn_id": "turn-render",
            },
            participant_id="member:renderer",
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setitem(
            server._methods,
            "team_mission.conversation.resolve",
            lambda rid, params: {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "conversation": {
                        "conversation_id": "conversation-1",
                        "conversation_session_id": "team-session-1",
                    },
                    "mission": {},
                    "team": {},
                    "graph": {},
                },
            },
        )

        response = server._methods["team_mission.conversation.render"](
            1,
            {"conversation_id": "conversation-1", "includeRunEvents": True},
        )

        message = response["result"]["messages"][0]
        assert message["participant_id"] == "member:renderer"
        assert message["participantId"] == "member:renderer"
    finally:
        db.close()


def test_legacy_event_without_participant_id_still_renders(tmp_path: Path, monkeypatch):
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")

    db = _db(tmp_path, "team-session-legacy")
    try:
        db.messages.append(
            "team-session-legacy",
            role="assistant",
            content="legacy reply",
            metadata={"run_id": "run-legacy", "turn_id": "turn-legacy"},
        )
        db.runs.append_event(
            "team-session-legacy",
            {
                **_event("run-legacy"),
                "session_id": "team-session-legacy",
                "conversation_session_id": "team-session-legacy",
                "turn_id": "turn-legacy",
            },
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setitem(
            server._methods,
            "team_mission.conversation.resolve",
            lambda rid, params: {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "conversation": {
                        "conversation_id": "conversation-legacy",
                        "conversation_session_id": "team-session-legacy",
                    },
                    "mission": {},
                    "team": {},
                    "graph": {},
                },
            },
        )

        response = server._methods["team_mission.conversation.render"](
            1,
            {"conversation_id": "conversation-legacy", "includeRunEvents": True},
        )

        message = response["result"]["messages"][0]
        assert message["text"] == "legacy reply"
        assert not message.get("participant_id")
        assert not message.get("teamMission", {}).get("participantId")
    finally:
        db.close()
