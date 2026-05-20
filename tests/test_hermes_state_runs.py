"""Persistent run and event-log storage tests."""

from tests.hermes_state_fixtures import db


def test_run_event_roundtrip_tracks_active_and_terminal_status(db):
    db.append_run_event(
        "stored-1",
        {
            "type": "message.start",
            "session_id": "runtime-1",
            "stored_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"run_id": "run-1", "turn_id": "turn-1"},
        },
    )

    status = db.get_session_run_status("stored-1")
    assert status["running"] is True
    assert status["active_run_id"] == "run-1"
    assert status["last_event_seq"] == 1

    replay = db.list_run_events("stored-1")
    assert [event["type"] for event in replay] == ["message.start"]

    db.append_run_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "stored_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"status": "complete", "run_id": "run-1", "turn_id": "turn-1"},
        },
    )

    status = db.get_session_run_status("stored-1")
    assert status["running"] is False
    assert status["last_event_seq"] == 2
    assert db.get_run("run-1")["status"] == "completed"
    assert db.list_run_events("stored-1", after_seq=1)[0]["type"] == "message.complete"


def test_cancelled_run_event_uses_cancelled_terminal_status(db):
    db.append_run_event(
        "stored-cancel",
        {
            "type": "message.start",
            "session_id": "runtime-cancel",
            "stored_session_id": "stored-cancel",
            "run_id": "run-cancel",
            "turn_id": "turn-cancel",
            "seq": 1,
            "payload": {"run_id": "run-cancel", "turn_id": "turn-cancel"},
        },
    )
    db.append_run_event(
        "stored-cancel",
        {
            "type": "message.complete",
            "session_id": "runtime-cancel",
            "stored_session_id": "stored-cancel",
            "run_id": "run-cancel",
            "turn_id": "turn-cancel",
            "seq": 2,
            "payload": {"status": "cancelled", "run_id": "run-cancel", "turn_id": "turn-cancel"},
        },
    )

    assert db.get_run("run-cancel")["status"] == "cancelled"


def test_active_only_replay_is_backed_by_persisted_run_state(db):
    db.append_run_event(
        "stored-2",
        {
            "type": "message.start",
            "session_id": "runtime-2",
            "stored_session_id": "stored-2",
            "run_id": "run-active",
            "turn_id": "turn-active",
            "seq": 1,
            "payload": {"run_id": "run-active", "turn_id": "turn-active"},
        },
    )
    db.append_run_event(
        "stored-2",
        {
            "type": "message.start",
            "session_id": "runtime-3",
            "stored_session_id": "stored-2",
            "run_id": "run-done",
            "turn_id": "turn-done",
            "seq": 2,
            "payload": {"run_id": "run-done", "turn_id": "turn-done"},
        },
    )
    db.append_run_event(
        "stored-2",
        {
            "type": "message.complete",
            "session_id": "runtime-3",
            "stored_session_id": "stored-2",
            "run_id": "run-done",
            "turn_id": "turn-done",
            "seq": 3,
            "payload": {"status": "complete", "run_id": "run-done", "turn_id": "turn-done"},
        },
    )

    replay = db.list_run_events("stored-2", active_only=True)
    assert [event["run_id"] for event in replay] == ["run-active"]


def test_run_events_do_not_require_session_row_before_first_persisted_message(db):
    db.upsert_run(run_id="early-run", session_id="future-session", status="running")
    db.append_run_event(
        "future-session",
        {
            "type": "tool.start",
            "session_id": "runtime-early",
            "stored_session_id": "future-session",
            "run_id": "early-run",
            "seq": 1,
            "payload": {"name": "tool"},
        },
    )

    assert db.get_session("future-session") is None
    assert db.get_session_run_status("future-session")["running"] is True


def test_event_created_run_preserves_runtime_scope_key(db):
    db.append_run_event(
        "stored-scope",
        {
            "type": "message.start",
            "session_id": "runtime-scope",
            "stored_session_id": "stored-scope",
            "run_id": "run-scope",
            "turn_id": "turn-scope",
            "runtime_scope_key": "profile:alpha:version:v1",
            "seq": 1,
            "payload": {"run_id": "run-scope", "turn_id": "turn-scope"},
        },
    )

    run = db.get_run("run-scope")
    assert run["runtime_scope_key"] == "profile:alpha:version:v1"
    assert [item["run_id"] for item in db.list_runs(runtime_scope_key="profile:alpha:version:v1")] == [
        "run-scope"
    ]


def test_run_event_replay_filters_by_runtime_scope_key(db):
    db.append_run_event(
        "stored-scope",
        {
            "type": "message.start",
            "session_id": "runtime-alpha",
            "stored_session_id": "stored-scope",
            "run_id": "run-alpha",
            "turn_id": "turn-alpha",
            "runtime_scope_key": "profile:alpha:version:v1",
            "seq": 1,
            "payload": {"run_id": "run-alpha", "turn_id": "turn-alpha"},
        },
    )
    db.append_run_event(
        "stored-scope",
        {
            "type": "message.start",
            "session_id": "runtime-beta",
            "stored_session_id": "stored-scope",
            "run_id": "run-beta",
            "turn_id": "turn-beta",
            "runtime_scope_key": "profile:beta:version:v1",
            "seq": 2,
            "payload": {"run_id": "run-beta", "turn_id": "turn-beta"},
        },
    )

    alpha_events = db.list_run_events(
        "stored-scope",
        runtime_scope_key="profile:alpha:version:v1",
    )

    assert [event["run_id"] for event in alpha_events] == ["run-alpha"]


def test_create_run_if_session_idle_rejects_second_active_run(db):
    first = db.create_run_if_session_idle(
        run_id="run-first",
        session_id="stored-busy",
        runtime_scope_key="profile:alpha:version:v1",
        turn_id="turn-first",
    )
    second = db.create_run_if_session_idle(
        run_id="run-second",
        session_id="stored-busy",
        runtime_scope_key="profile:alpha:version:v1",
        turn_id="turn-second",
    )

    assert first["run"]["run_id"] == "run-first"
    assert first["created"] is True
    assert second["run"] is None
    assert second["conflict"]["run_id"] == "run-first"


def test_create_run_if_session_idle_allows_next_run_after_terminal(db):
    db.create_run_if_session_idle(run_id="run-old", session_id="stored-next")
    db.upsert_run(
        run_id="run-old",
        session_id="stored-next",
        status="completed",
    )

    next_run = db.create_run_if_session_idle(
        run_id="run-next",
        session_id="stored-next",
    )

    assert next_run["created"] is True
    assert next_run["run"]["run_id"] == "run-next"


def test_waiting_approval_counts_as_active_run(db):
    db.upsert_run(
        run_id="run-approval",
        session_id="stored-approval",
        status="waiting_approval",
    )

    status = db.get_session_run_status("stored-approval")
    second = db.create_run_if_session_idle(
        run_id="run-second",
        session_id="stored-approval",
    )

    assert status["running"] is True
    assert status["active_run_id"] == "run-approval"
    assert second["conflict"]["run_id"] == "run-approval"


def test_list_runs_filters_by_runtime_scope_and_status(db):
    db.upsert_run(
        run_id="run-a",
        session_id="session-a",
        runtime_scope_key="profile:alpha",
        status="running",
    )
    db.upsert_run(
        run_id="run-b",
        session_id="session-b",
        runtime_scope_key="profile:beta",
        status="completed",
    )

    runs = db.list_runs(runtime_scope_key="profile:alpha", statuses=["running"])

    assert [run["run_id"] for run in runs] == ["run-a"]


def test_prune_run_events_archives_terminal_events_and_preserves_active(db):
    old_ts = 1000.0
    now = old_ts + 40 * 86400
    db.append_run_event(
        "stored-prune",
        {
            "type": "message.start",
            "session_id": "runtime-old",
            "stored_session_id": "stored-prune",
            "run_id": "run-old",
            "turn_id": "turn-old",
            "seq": 1,
            "timestamp": old_ts,
            "payload": {"run_id": "run-old", "turn_id": "turn-old"},
        },
    )
    db.append_run_event(
        "stored-prune",
        {
            "type": "message.complete",
            "session_id": "runtime-old",
            "stored_session_id": "stored-prune",
            "run_id": "run-old",
            "turn_id": "turn-old",
            "seq": 2,
            "timestamp": old_ts + 1,
            "payload": {"status": "complete", "run_id": "run-old", "turn_id": "turn-old"},
        },
    )
    db.append_run_event(
        "stored-prune",
        {
            "type": "message.start",
            "session_id": "runtime-active",
            "stored_session_id": "stored-prune",
            "run_id": "run-active",
            "turn_id": "turn-active",
            "seq": 3,
            "timestamp": old_ts,
            "payload": {"run_id": "run-active", "turn_id": "turn-active"},
        },
    )

    result = db.prune_run_events(session_id="stored-prune", retention_days=14, now=now)

    assert result["deleted_events"] == 2
    assert [event["run_id"] for event in db.list_run_events("stored-prune")] == ["run-active"]
    archives = db._conn.execute(
        "SELECT * FROM run_event_archives WHERE session_id = ?",
        ("stored-prune",),
    ).fetchall()
    assert len(archives) == 1
    assert archives[0]["run_id"] == "run-old"
    assert archives[0]["event_count"] == 2


def test_fail_orphaned_active_runs_marks_dead_owner_failed(db):
    db.upsert_run(
        run_id="run-dead",
        session_id="stored-dead",
        runtime_scope_key="profile:dead",
        runtime_session_id="runtime-dead",
        status="running",
        metadata={"gateway_pid": 99999999, "gateway_instance_id": "dead"},
    )

    failed = db.fail_orphaned_active_runs(
        live_runtime_session_ids=set(),
        current_pid=12345,
        current_gateway_instance_id="current",
        stale_after_seconds=300,
    )

    assert failed == 1
    run = db.get_run("run-dead")
    assert run["status"] == "failed"
    assert "runtime owner" in run["error"]
    events = db.list_run_events("stored-dead")
    assert len(events) == 1
    assert events[0]["type"] == "message.complete"
    assert events[0]["run_id"] == "run-dead"
    assert events[0]["payload"]["status"] == "failed"
    assert events[0]["payload"]["recovery"] is True
    status = db.get_session_run_status("stored-dead")
    assert status["running"] is False
    assert status["last_event_seq"] == events[0]["seq"]


def test_fail_orphaned_active_runs_preserves_live_runtime(db):
    db.upsert_run(
        run_id="run-live",
        session_id="stored-live",
        runtime_scope_key="profile:live",
        runtime_session_id="runtime-live",
        status="running",
        metadata={"gateway_pid": 99999999, "gateway_instance_id": "dead"},
    )

    failed = db.fail_orphaned_active_runs(
        live_runtime_session_ids={"runtime-live"},
        current_pid=12345,
        current_gateway_instance_id="current",
        stale_after_seconds=0,
    )

    assert failed == 0
    assert db.get_run("run-live")["status"] == "running"
    assert db.list_run_events("stored-live") == []


def test_fail_orphaned_active_runs_stales_legacy_ownerless_rows(db):
    db.upsert_run(
        run_id="run-legacy",
        session_id="stored-legacy",
        runtime_scope_key="profile:legacy",
        runtime_session_id="runtime-legacy",
        status="running",
        started_at=1000.0,
        updated_at=1000.0,
    )

    failed = db.fail_orphaned_active_runs(
        live_runtime_session_ids=set(),
        current_pid=12345,
        current_gateway_instance_id="current",
        stale_after_seconds=0,
    )

    assert failed == 1
    assert db.get_run("run-legacy")["status"] == "failed"
