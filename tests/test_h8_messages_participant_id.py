from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from typing import Any

from hermes_state import SCHEMA_VERSION, SessionDB
from tui_gateway.services import run_control


def _db(tmp_path: Path, session_id: str = "team-session-1") -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id, source="team_mission", transient=False)
    return db


def _message_event(
    *,
    run_id: str = "run-worker-1",
    participant_id: str = "member:writer",
    stored_session_id: str = "worker-session-1",
) -> dict[str, Any]:
    return {
        "type": "message.complete",
        "session_id": stored_session_id,
        "stored_session_id": stored_session_id,
        "run_id": run_id,
        "turn_id": "turn-worker-1",
        "seq": 1,
        "participant_id": participant_id,
        "payload": {
            "text": "final answer",
            "status": "complete",
            "participant_id": participant_id,
        },
    }


def test_append_message_stores_participant_id(tmp_path: Path):
    db = _db(tmp_path)
    try:
        message_id = db.append_message(
            "team-session-1",
            role="assistant",
            content="hello",
            participant_id="member:alice",
        )

        row = db._conn.execute(  # noqa: SLF001
            "SELECT participant_id FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        assert row["participant_id"] == "member:alice"
    finally:
        db.close()


def test_get_messages_returns_participant_id(tmp_path: Path):
    db = _db(tmp_path)
    try:
        db.append_message(
            "team-session-1",
            role="assistant",
            content="hello",
            participant_id="member:alice",
        )

        raw = db.get_messages("team-session-1")[0]
        conversation = db.get_messages_as_conversation("team-session-1")[0]
        page = db.get_messages_page_as_conversation("team-session-1")["messages"][0]

        assert raw["participant_id"] == "member:alice"
        assert conversation["participant_id"] == "member:alice"
        assert page["participant_id"] == "member:alice"
    finally:
        db.close()


class _MirrorDb:
    def __init__(self) -> None:
        self.appended_messages: list[dict[str, Any]] = []
        self.appended_events: list[dict[str, Any]] = []

    def append_run_event(self, session_id: str, event: dict[str, Any], participant_id: str = "") -> dict[str, Any]:
        saved = {
            **event,
            "stored_session_id": session_id,
            "participant_id": participant_id or event.get("participant_id", ""),
            "seq": event.get("seq") or len(self.appended_events) + 1,
        }
        payload = dict(saved.get("payload") or {})
        if saved["participant_id"]:
            payload["participant_id"] = saved["participant_id"]
            saved["payload"] = payload
        self.appended_events.append(saved)
        return saved

    def append_team_mission_event_for_run(self, *, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return {}

    def reduce_team_mission_run_event(self, *, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return {"mission_id": "mission-1"}

    def get_team_mission_run_binding(self, run_id: str) -> dict[str, Any]:
        return {
            "mission_id": "mission-1",
            "node_id": "node-final",
            "session_id": "worker-session-1",
            "role": "synthesis",
            "metadata": {"run_context": {"participant_id": "member:fallback"}},
        }

    def get_team_mission_graph(self, mission_id: str) -> dict[str, Any]:
        return {
            "mission": {
                "mission_id": mission_id,
                "leader_session_id": "team-session-1",
                "metadata": {"conversation_session_id": "team-session-1"},
            },
            "nodes": [{"node_id": "node-final", "kind": "synthesis"}],
        }

    def latest_team_mission_deliverable_for_run(self, run_id: str) -> dict[str, Any]:
        return {}

    def get_session(self, session_id: str) -> dict[str, Any]:
        return {"id": session_id}

    def create_session(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"id": args[0] if args else kwargs.get("session_id", "")}

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        return []

    def append_message(self, session_id: str, role: str, content: str, **kwargs: Any) -> int:
        self.appended_messages.append(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                **kwargs,
            }
        )
        return len(self.appended_messages)


def test_record_event_preserves_participant_id_on_run_event_without_writing_transcript():
    db = _MirrorDb()

    run_control.record_event(_message_event(), db=db)

    assert db.appended_messages == []
    assert db.appended_events
    event = db.appended_events[0]
    assert event["participant_id"] == "member:writer"
    assert event["payload"]["participant_id"] == "member:writer"


def _create_legacy_v29_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (29);

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT,
                platform_message_id TEXT,
                metadata_json TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_messages_schema_migration_idempotent(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_v29_db(db_path)

    first = SessionDB(db_path)
    try:
        columns = {
            row["name"]: row
            for row in first._conn.execute("PRAGMA table_info(messages)").fetchall()  # noqa: SLF001
        }
        assert columns["participant_id"]["type"] == "TEXT"
        assert columns["participant_id"]["notnull"] == 1
        assert columns["participant_id"]["dflt_value"] == "''"
        assert first._conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION  # noqa: SLF001
    finally:
        first.close()

    second = SessionDB(db_path)
    try:
        participant_columns = [
            row["name"]
            for row in second._conn.execute("PRAGMA table_info(messages)").fetchall()  # noqa: SLF001
            if row["name"] == "participant_id"
        ]
        assert participant_columns == ["participant_id"]
    finally:
        second.close()


def test_team_conversation_render_history_messages_have_participant_id(tmp_path: Path, monkeypatch):
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")

    db = _db(tmp_path)
    try:
        db.append_message(
            "team-session-1",
            role="assistant",
            content="history reply",
            participant_id="member:renderer",
            metadata={"run_id": "run-render", "turn_id": "turn-render"},
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
                        "stable_session_id": "team-session-1",
                    },
                    "mission": {},
                    "team": {},
                    "graph": {},
                },
            },
        )

        response = server._methods["team_mission.conversation.render"](
            1,
            {"conversation_id": "conversation-1", "includeRunEvents": False},
        )

        message = response["result"]["messages"][0]
        assert message["participant_id"] == "member:renderer"
        assert message["participantId"] == "member:renderer"
    finally:
        db.close()
