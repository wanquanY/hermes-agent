from __future__ import annotations

import threading
from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_run_event_listener_observes_committed_event(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    observed: list[tuple[dict, list[dict]]] = []
    try:
        db.runs.register_event_listener(
            "test-listener",
            lambda event: observed.append(
                (event, db.runs.list_events("session-1"))
            ),
        )

        saved = db.runs.append_event(
            "session-1",
            {
                "type": "message.delta",
                "run_id": "run-1",
                "payload": {"text": "hello"},
            },
        )

        assert len(observed) == 1
        event, replay = observed[0]
        assert event["seq"] == saved["seq"] == 1
        assert [item["seq"] for item in replay] == [1]
        assert replay[0]["payload"]["text"] == "hello"
    finally:
        db.close()


def test_run_event_listener_registration_is_keyed_and_removable(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    first: list[int] = []
    replacement: list[int] = []
    try:
        db.runs.register_event_listener(
            "activity-subscriptions",
            lambda event: first.append(int(event["seq"])),
        )
        db.runs.register_event_listener(
            "activity-subscriptions",
            lambda event: replacement.append(int(event["seq"])),
        )

        db.runs.append_event(
            "session-1",
            {"type": "message.delta", "run_id": "run-1", "payload": {}},
        )
        assert first == []
        assert replacement == [1]

        assert db.runs.unregister_event_listener("activity-subscriptions") is True
        assert db.runs.unregister_event_listener("activity-subscriptions") is False
        db.runs.append_event(
            "session-1",
            {"type": "message.complete", "run_id": "run-1", "payload": {}},
        )
        assert replacement == [1]
    finally:
        db.close()


def test_run_event_listener_preserves_same_journal_append_order(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    first_listener_started = threading.Event()
    release_first_listener = threading.Event()
    second_append_finished = threading.Event()
    observed: list[int] = []

    def listener(event: dict) -> None:
        seq = int(event["seq"])
        if seq == 1:
            first_listener_started.set()
            assert release_first_listener.wait(timeout=2)
        observed.append(seq)

    def append(event_type: str, finished: threading.Event | None = None) -> None:
        db.runs.append_event(
            "team:mission:ordered:events",
            {"type": event_type, "payload": {}},
        )
        if finished is not None:
            finished.set()

    try:
        db.runs.register_event_listener("ordered-activity-journal", listener)
        first = threading.Thread(target=append, args=("message.delta",))
        second = threading.Thread(
            target=append,
            args=("tool.start", second_append_finished),
        )
        first.start()
        assert first_listener_started.wait(timeout=2)
        second.start()

        # The second append may not overtake the first listener notification.
        assert not second_append_finished.wait(timeout=0.1)
        assert observed == []

        release_first_listener.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert observed == [1, 2]
    finally:
        release_first_listener.set()
        db.close()
