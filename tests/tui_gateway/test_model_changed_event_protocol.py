from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway import server
from tui_gateway.services import run_control
from tui_gateway.services.runtime_event_protocol import (
    is_durable_conversation_event,
)


class _RecordingTransport:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, frame: dict) -> bool:
        self.frames.append(frame)
        return True


def test_model_changed_is_durable_without_an_active_run(tmp_path, monkeypatch):
    run_control._reset_for_tests()
    db = open_cli_session_store(tmp_path / "model-changed.db")
    conversation_session_id = "conversation-model-switch"
    execution_session_id = "runtime-model-switch"
    db.sessions.create(
        conversation_session_id,
        source="tui",
        title="Model switch",
    )
    transport = _RecordingTransport()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    server._sessions[execution_session_id] = {
        "session_key": conversation_session_id,
        "running": False,
        "transport": transport,
    }

    try:
        server._emit(
            "session.model.changed",
            execution_session_id,
            {
                "conversation_session_id": conversation_session_id,
                "session_revision": 2,
                "selection": {
                    "provider_id": "kimi-coding",
                    "model_id": "kimi-k3",
                },
            },
        )

        stored_events = db.runs.list_events(
            conversation_session_id,
            event_types=("session.model.changed",),
        )
        delivered_events = [
            frame["params"]
            for frame in transport.frames
            if frame.get("method") == "event"
            and (frame.get("params") or {}).get("type")
            == "session.model.changed"
        ]

        assert is_durable_conversation_event("session.model.changed") is True
        assert len(stored_events) == 1
        assert len(delivered_events) == 1
        assert stored_events[0]["run_id"] == ""
        assert stored_events[0]["seq"] > 0
        assert stored_events[0]["runtime_source_seq"] > 0
        assert stored_events[0].get("transient") is not True
        assert delivered_events[0]["seq"] == stored_events[0]["seq"]
        assert (
            delivered_events[0]["runtime_source_seq"]
            == stored_events[0]["runtime_source_seq"]
        )
        assert delivered_events[0].get("transient") is not True
    finally:
        server._sessions.pop(execution_session_id, None)
        db.close()
        run_control._reset_for_tests()
