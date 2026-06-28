from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from hermes_state import SessionDB
from hermes_team_mission.runtime.activity_command_bridge import record_legacy_activity_command
from tui_gateway import server
from tui_gateway.services import run_control
from tui_gateway.services.activity_reconciler import ActivityReconciler

activity_methods = importlib.import_module("tui_gateway.methods.activity")


class _CaptureTransport:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, obj: dict[str, Any]) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        return None


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SessionDB]:
    state = SessionDB(tmp_path / "state.db")
    try:
        yield state
    finally:
        state.close()


@pytest.fixture(autouse=True)
def gateway_state(db: SessionDB) -> Iterator[None]:
    previous_db = server._db
    previous_db_error = server._db_error
    previous_db_by_home = dict(server._db_by_home)
    previous_db_error_by_home = dict(server._db_error_by_home)
    server._db = db
    server._db_error = None
    server._db_by_home = {}
    server._db_error_by_home = {}
    with run_control._lock:
        run_control._subscriptions_by_id.clear()
        run_control._subscription_ids_by_session.clear()
        run_control._subscription_ids_by_activity.clear()
        run_control._subscription_ids_by_transport.clear()
    try:
        yield
    finally:
        with run_control._lock:
            run_control._subscriptions_by_id.clear()
            run_control._subscription_ids_by_session.clear()
            run_control._subscription_ids_by_activity.clear()
            run_control._subscription_ids_by_transport.clear()
        server._db = previous_db
        server._db_error = previous_db_error
        server._db_by_home = previous_db_by_home
        server._db_error_by_home = previous_db_error_by_home


def _call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    transport: _CaptureTransport | None = None,
) -> dict[str, Any]:
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": f"test-{method}-{time.time_ns()}",
            "method": method,
            "params": params or {},
        },
        transport=transport,
    )
    assert isinstance(response, dict)
    return response


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return response["result"]


def _record_activity_event(
    db: SessionDB,
    *,
    activity_id: str = "act-test-1",
    stored_session_id: str = "session-activity-1",
    event_type: str = "activity.command.created",
    command_id: str = "cmd-test-1",
    publish: bool = False,
) -> dict[str, Any]:
    frame = {
        "type": event_type,
        "session_id": stored_session_id,
        "stored_session_id": stored_session_id,
        "activity_id": activity_id,
        "seq": run_control.next_event_seq(stored_session_id, db=db),
        "payload": {
            "activity_id": activity_id,
            "command_id": command_id,
            "state": "satisfied",
        },
    }
    if publish:
        run_control.publish_recorded_event(frame, db=db)
    else:
        run_control.record_event(frame, db=db)
    return frame


def _event_frames(transport: _CaptureTransport) -> list[dict[str, Any]]:
    return [
        frame.get("params") or {}
        for frame in transport.frames
        if frame.get("method") == "event"
    ]


def test_subscribe_rejects_missing_activity_id() -> None:
    response = _call("runtime.activity.subscribe", {})

    assert response["error"]["code"] == 4006


def test_subscribe_rejects_malformed_activity_id() -> None:
    response = _call("runtime.activity.subscribe", {"activity_id": "not-valid"})

    assert response["error"]["code"] == 4006


def test_subscribe_returns_5008_when_db_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity_methods._server, "_get_db", lambda: None)

    response = _call("runtime.activity.subscribe", {"activity_id": "act-test-missing-db"})

    assert response["error"]["code"] == 5008


def test_subscribe_returns_4040_when_activity_has_no_events() -> None:
    response = _call("runtime.activity.subscribe", {"activity_id": "act-test-empty"})

    assert response["error"]["code"] == 4040


def test_unsubscribe_returns_zero_when_subscription_missing() -> None:
    result = _assert_ok(
        _call("runtime.activity.unsubscribe", {"subscription_id": "missing-subscription"})
    )

    assert result == {"removed": 0}


def test_subscribe_returns_events_for_activity_id(db: SessionDB) -> None:
    _record_activity_event(db, activity_id="act-test-replay", command_id="cmd-replay-1")

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-replay"})
    )

    assert result["subscription_id"]
    assert [event["payload"]["command_id"] for event in result["events"]] == ["cmd-replay-1"]
    assert result["after_seq"] == result["events"][-1]["seq"]


def test_subscribe_after_seq_filters_correctly(db: SessionDB) -> None:
    first = _record_activity_event(db, activity_id="act-test-after", command_id="cmd-after-1")
    _record_activity_event(db, activity_id="act-test-after", command_id="cmd-after-2")

    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-after", "after_seq": first["seq"]},
        )
    )

    assert [event["payload"]["command_id"] for event in result["events"]] == ["cmd-after-2"]


def test_subscribe_limit_caps_replay_size(db: SessionDB) -> None:
    for index in range(3):
        _record_activity_event(
            db,
            activity_id="act-test-limit",
            command_id=f"cmd-limit-{index}",
        )

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-limit", "limit": 2})
    )

    assert len(result["events"]) == 2


def test_subscribe_returns_unique_subscription_id(db: SessionDB) -> None:
    _record_activity_event(db, activity_id="act-test-unique")

    first = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-unique"}, transport=_CaptureTransport())
    )
    second = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-unique"}, transport=_CaptureTransport())
    )

    assert first["subscription_id"] != second["subscription_id"]


def test_subscribe_then_unsubscribe_removes_registration(db: SessionDB) -> None:
    _record_activity_event(db, activity_id="act-test-lifecycle")
    result = _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-lifecycle"},
            transport=_CaptureTransport(),
        )
    )
    subscription_id = result["subscription_id"]

    removed = _assert_ok(
        _call("runtime.activity.unsubscribe", {"subscription_id": subscription_id})
    )

    assert removed == {"removed": 1}
    assert subscription_id not in run_control._subscriptions_by_id
    assert subscription_id not in run_control._subscription_ids_by_activity["act-test-lifecycle"]


def test_record_event_pushes_to_activity_subscribers(db: SessionDB) -> None:
    _record_activity_event(db, activity_id="act-test-live")
    transport = _CaptureTransport()
    _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": "act-test-live"},
            transport=transport,
        )
    )

    _record_activity_event(
        db,
        activity_id="act-test-live",
        command_id="cmd-live-2",
        publish=True,
    )

    delivered = _event_frames(transport)
    assert [event["payload"]["command_id"] for event in delivered] == ["cmd-live-2"]


def test_record_event_does_not_double_push_when_transport_subscribed_by_session_and_activity(
    db: SessionDB,
) -> None:
    stored_session_id = "session-double-subscribe"
    activity_id = "act-test-double"
    _record_activity_event(db, activity_id=activity_id, stored_session_id=stored_session_id)
    transport = _CaptureTransport()
    run_control.subscribe_session_with_id(
        stored_session_id=stored_session_id,
        transport=transport,
        db=db,
    )
    _assert_ok(
        _call(
            "runtime.activity.subscribe",
            {"activity_id": activity_id},
            transport=transport,
        )
    )

    _record_activity_event(
        db,
        activity_id=activity_id,
        stored_session_id=stored_session_id,
        command_id="cmd-double-live",
        publish=True,
    )

    delivered = _event_frames(transport)
    assert [event["payload"]["command_id"] for event in delivered] == ["cmd-double-live"]


def test_subscribe_picks_up_events_emitted_by_reconciler(db: SessionDB) -> None:
    db.insert_activity_command(
        command_id="cmd-reconciler-create",
        activity_id="act-test-reconciler",
        kind="create",
        payload={
            "kind": "mission",
            "conversation_id": "conv-reconciler",
            "conversation_session_id": "session-reconciler",
        },
        metadata={"test": True},
    )
    ActivityReconciler(db).run_one_cycle()

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-reconciler"})
    )

    assert [event["type"] for event in result["events"]] == ["activity.command.created"]


def test_subscribe_picks_up_events_for_team_mission_create_via_legacy_bridge(
    db: SessionDB,
) -> None:
    command_id = record_legacy_activity_command(
        db,
        activity_id="mission:test-legacy",
        kind="create",
        payload={
            "kind": "mission",
            "conversation_id": "conversation-legacy",
            "conversation_session_id": "session-legacy",
        },
        source="team_mission.create",
    )
    assert command_id
    ActivityReconciler(db).run_one_cycle()

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "mission:test-legacy"})
    )

    assert [event["payload"]["command_id"] for event in result["events"]] == [command_id]


def test_subscribe_activity_id_filter_isolates_different_activities(db: SessionDB) -> None:
    _record_activity_event(db, activity_id="act-test-isolated-a", command_id="cmd-a")
    _record_activity_event(db, activity_id="act-test-isolated-b", command_id="cmd-b")

    result = _assert_ok(
        _call("runtime.activity.subscribe", {"activity_id": "act-test-isolated-a"})
    )

    assert [event["payload"]["command_id"] for event in result["events"]] == ["cmd-a"]


def test_list_run_events_by_activity_returns_only_matching_rows(db: SessionDB) -> None:
    _record_activity_event(
        db,
        activity_id="act-test-dao",
        stored_session_id="session-dao-a",
        command_id="cmd-dao-a",
    )
    _record_activity_event(
        db,
        activity_id="act-test-dao",
        stored_session_id="session-dao-b",
        command_id="cmd-dao-b",
    )
    _record_activity_event(
        db,
        activity_id="act-test-other",
        stored_session_id="session-dao-a",
        command_id="cmd-other",
    )

    events = db.list_run_events_by_activity("act-test-dao")

    assert [event["payload"]["command_id"] for event in events] == ["cmd-dao-a", "cmd-dao-b"]
    assert {event["stored_session_id"] for event in events} == {"session-dao-a", "session-dao-b"}


def test_list_run_events_by_activity_returns_empty_for_unknown(db: SessionDB) -> None:
    assert db.list_run_events_by_activity("act-test-unknown") == []
