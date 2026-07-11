from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway.services.activity_reconciler import ActivityReconciler


@pytest.fixture()
def db(tmp_path: Path) -> CliSessionStore:
    state = open_cli_session_store(tmp_path / "state.db")
    try:
        yield state
    finally:
        state.close()


def _activity(db: CliSessionStore, activity_id: str = "act-1", *, status: str = "pending") -> dict:
    return db.activities.create(
        activity_id=activity_id,
        conversation_id=f"conv-{activity_id}",
        kind="mission",
        status=status,
    )


def _insert(
    db: CliSessionStore,
    command_id: str,
    *,
    activity_id: str = "act-1",
    kind: str = "create",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return db.activities.insert_command(
        command_id=command_id,
        activity_id=activity_id,
        kind=kind,
        payload=payload
        or {
            "kind": "mission",
            "conversation_id": f"conv-{activity_id}",
            "conversation_session_id": f"conv-{activity_id}",
        },
        metadata={"test": True},
    )


def _set_intent_at(db: CliSessionStore, command_id: str, value: float) -> None:
    with db._lock:
        db._conn.execute(
            "UPDATE activity_commands SET intent_at = ? WHERE command_id = ?",
            (value, command_id),
        )


def _command(db: CliSessionStore, command_id: str) -> dict[str, Any]:
    row = db.activities.get_command(command_id)
    assert row
    return row


def _events(db: CliSessionStore, session_id: str, *, activity_id: str = "") -> list[dict[str, Any]]:
    return db.runs.list_events(session_id, activity_id=activity_id)


def _event_types(db: CliSessionStore, session_id: str, *, activity_id: str = "") -> list[str]:
    return [event["type"] for event in _events(db, session_id, activity_id=activity_id)]


def test_reconciler_disabled_via_env_does_nothing(db: CliSessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOVIE_ACTIVITY_RECONCILER_DISABLED", "true")
    _insert(db, "cmd-create")
    reconciler = ActivityReconciler(db)

    asyncio.run(reconciler.start())

    assert reconciler._task is None
    assert _command(db, "cmd-create")["state"] == "accepted"


def test_run_one_cycle_processes_no_commands_when_empty(db: CliSessionStore) -> None:
    assert ActivityReconciler(db).run_one_cycle() == {
        "processed": 0,
        "satisfied": 0,
        "failed": 0,
        "skipped": 0,
    }


def test_run_one_cycle_returns_summary_dict(db: CliSessionStore) -> None:
    _insert(db, "cmd-create")

    summary = ActivityReconciler(db).run_one_cycle()

    assert set(summary) == {"processed", "satisfied", "failed", "skipped"}
    assert summary["processed"] == 1
    assert summary["satisfied"] == 1


def test_create_command_creates_activity_row(db: CliSessionStore) -> None:
    _insert(db, "cmd-create", activity_id="act-create")

    ActivityReconciler(db).run_one_cycle()

    activity = db.activities.get("act-create")
    assert activity
    assert activity["kind"] == "mission"
    assert activity["conversation_id"] == "conv-act-create"


def test_create_command_emits_activity_command_created(db: CliSessionStore) -> None:
    _insert(db, "cmd-create", activity_id="act-create")

    ActivityReconciler(db).run_one_cycle()

    assert _event_types(db, "conv-act-create") == ["activity.command.created"]


def test_create_command_transitions_to_satisfied(db: CliSessionStore) -> None:
    _insert(db, "cmd-create", activity_id="act-create")

    ActivityReconciler(db).run_one_cycle()

    assert _command(db, "cmd-create")["state"] == "satisfied"


def test_create_command_idempotent_when_activity_already_exists(db: CliSessionStore) -> None:
    _activity(db, "act-create")
    _insert(db, "cmd-create", activity_id="act-create")
    reconciler = ActivityReconciler(db)

    first = reconciler.run_one_cycle()
    second = reconciler.run_one_cycle()

    assert first["satisfied"] == 1
    assert second["processed"] == 0
    assert len(db.activities.list("conv-act-create")) == 1
    assert _event_types(db, "conv-act-create", activity_id="act-create") == [
        "activity.command.created"
    ]


def test_create_command_failed_db_marks_command_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_db = open_cli_session_store(tmp_path / "state.db")

    def fail_create(**_kwargs) -> dict:
        raise RuntimeError("create failed")

    monkeypatch.setattr(failing_db.activities, "create", fail_create)
    try:
        _insert(failing_db, "cmd-create", activity_id="act-create")

        summary = ActivityReconciler(failing_db).run_one_cycle()

        assert summary["failed"] == 1
        command = _command(failing_db, "cmd-create")
        assert command["state"] == "failed"
        assert "create failed" in command["error_reason"]
        assert _event_types(failing_db, "conv-act-create") == [
            "activity.command.run.failed"
        ]
    finally:
        failing_db.close()


def test_start_command_emits_start_accepted(db: CliSessionStore) -> None:
    _activity(db, "act-start")
    _insert(db, "cmd-start", activity_id="act-start", kind="start")

    ActivityReconciler(db).run_one_cycle()

    assert _event_types(db, "conv-act-start", activity_id="act-start") == [
        "activity.command.start.accepted"
    ]


def test_start_command_transitions_to_dispatched(db: CliSessionStore) -> None:
    _activity(db, "act-start")
    _insert(db, "cmd-start", activity_id="act-start", kind="start")

    ActivityReconciler(db).run_one_cycle()

    assert _command(db, "cmd-start")["state"] == "dispatched"


def test_start_command_does_not_spawn_worker() -> None:
    src = Path("tui_gateway/services/activity_reconciler.py").read_text()
    tree = ast.parse(src)
    forbidden = {
        "_proxy_run_submit_via_worker",
        "_submit_run_via_worker_with_response",
        "primary_dispatch",
        "worker_supervisor",
        "worker_runtime",
        "RunCancelFrame",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            violations.append(node.id)
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            violations.append(node.attr)
    assert not violations


def test_start_command_dispatched_state_timeout_marks_failed(db: CliSessionStore) -> None:
    _activity(db, "act-start")
    _insert(db, "cmd-start", activity_id="act-start", kind="start")
    db.activities.update_command_state("cmd-start", next_state="dispatched")
    _set_intent_at(db, "cmd-start", time.time() - 20)

    summary = ActivityReconciler(db, command_timeout_s=5).run_one_cycle()

    assert summary["failed"] == 1
    assert _command(db, "cmd-start")["state"] == "failed"
    assert _event_types(db, "conv-act-start", activity_id="act-start") == [
        "activity.command.run.failed"
    ]


def test_start_command_dispatched_within_timeout_stays_pending(db: CliSessionStore) -> None:
    _activity(db, "act-start")
    _insert(db, "cmd-start", activity_id="act-start", kind="start")
    db.activities.update_command_state("cmd-start", next_state="dispatched")

    summary = ActivityReconciler(db, command_timeout_s=60).run_one_cycle()

    assert summary["processed"] == 1
    assert summary["skipped"] == 1
    assert _command(db, "cmd-start")["state"] == "dispatched"
    assert _events(db, "conv-act-start") == []


def test_cancel_command_calls_mark_activity_cancelled(db: CliSessionStore) -> None:
    _activity(db, "act-cancel")
    _insert(db, "cmd-cancel", activity_id="act-cancel", kind="cancel")

    ActivityReconciler(db).run_one_cycle()

    assert db.activities.get("act-cancel")["status"] == "cancelled"


def test_cancel_command_emits_activity_command_cancelled(db: CliSessionStore) -> None:
    _activity(db, "act-cancel")
    _insert(db, "cmd-cancel", activity_id="act-cancel", kind="cancel")

    ActivityReconciler(db).run_one_cycle()

    assert _event_types(db, "conv-act-cancel", activity_id="act-cancel") == [
        "activity.command.cancelled"
    ]


def test_cancel_command_transitions_to_satisfied(db: CliSessionStore) -> None:
    _activity(db, "act-cancel")
    _insert(db, "cmd-cancel", activity_id="act-cancel", kind="cancel")

    ActivityReconciler(db).run_one_cycle()

    assert _command(db, "cmd-cancel")["state"] == "satisfied"


def test_cancel_command_already_terminal_still_satisfies(db: CliSessionStore) -> None:
    _activity(db, "act-cancel", status="cancelled")
    _insert(db, "cmd-cancel", activity_id="act-cancel", kind="cancel")

    ActivityReconciler(db).run_one_cycle()

    assert _command(db, "cmd-cancel")["state"] == "satisfied"
    assert db.activities.get("act-cancel")["status"] == "cancelled"


def test_complete_command_updates_activities_status_to_completed(db: CliSessionStore) -> None:
    _activity(db, "act-complete")
    _insert(db, "cmd-complete", activity_id="act-complete", kind="complete")

    ActivityReconciler(db).run_one_cycle()

    assert db.activities.get("act-complete")["status"] == "completed"


def test_complete_command_emits_activity_command_completed(db: CliSessionStore) -> None:
    _activity(db, "act-complete")
    _insert(db, "cmd-complete", activity_id="act-complete", kind="complete")

    ActivityReconciler(db).run_one_cycle()

    assert _event_types(db, "conv-act-complete", activity_id="act-complete") == [
        "activity.command.completed"
    ]


def test_complete_command_transitions_to_satisfied(db: CliSessionStore) -> None:
    _activity(db, "act-complete")
    _insert(db, "cmd-complete", activity_id="act-complete", kind="complete")

    ActivityReconciler(db).run_one_cycle()

    assert _command(db, "cmd-complete")["state"] == "satisfied"


def test_complete_command_persists_completed_at(db: CliSessionStore) -> None:
    _activity(db, "act-complete")
    _insert(db, "cmd-complete", activity_id="act-complete", kind="complete")

    ActivityReconciler(db).run_one_cycle()

    assert db.activities.get("act-complete")["completed_at"] is not None


def test_emit_event_rejects_event_type_outside_namespace(db: CliSessionStore) -> None:
    reconciler = ActivityReconciler(db)

    with pytest.raises(ValueError):
        reconciler._emit_event(
            "activity.completed",
            activity_id="act-1",
            command_id="cmd-1",
            conversation_session_id="conv-1",
        )


def test_emit_event_writes_into_run_events_table(db: CliSessionStore) -> None:
    reconciler = ActivityReconciler(db)

    reconciler._emit_event(
        "activity.command.created",
        activity_id="act-1",
        command_id="cmd-1",
        conversation_session_id="conv-1",
    )

    assert _event_types(db, "conv-1") == ["activity.command.created"]


def test_emit_event_carries_activity_id_in_frame(db: CliSessionStore) -> None:
    reconciler = ActivityReconciler(db)

    reconciler._emit_event(
        "activity.command.created",
        activity_id="act-1",
        command_id="cmd-1",
        conversation_session_id="conv-1",
    )

    event = _events(db, "conv-1")[0]
    assert event["activity_id"] == "act-1"
    assert event["payload"]["activity_id"] == "act-1"


def test_reconciler_processes_multiple_pending_commands_in_order(db: CliSessionStore) -> None:
    _insert(db, "cmd-late", activity_id="act-late")
    _insert(db, "cmd-early", activity_id="act-early")
    _set_intent_at(db, "cmd-late", 20.0)
    _set_intent_at(db, "cmd-early", 10.0)

    summary = ActivityReconciler(db).run_one_cycle()

    assert summary["processed"] == 2
    assert summary["satisfied"] == 2
    events = _events(db, "conv-act-early") + _events(db, "conv-act-late")
    assert [event["payload"]["command_id"] for event in events] == [
        "cmd-early",
        "cmd-late",
    ]


def test_reconciler_one_bad_handler_does_not_poison_remaining(db: CliSessionStore) -> None:
    _insert(
        db,
        "cmd-bad",
        activity_id="act-bad",
        payload={
            "conversation_id": "conv-act-bad",
            "conversation_session_id": "conv-act-bad",
        },
    )
    _insert(db, "cmd-good", activity_id="act-good")

    summary = ActivityReconciler(db).run_one_cycle()

    assert summary["processed"] == 2
    assert summary["failed"] == 1
    assert summary["satisfied"] == 1
    assert _command(db, "cmd-bad")["state"] == "failed"
    assert _command(db, "cmd-good")["state"] == "satisfied"
    assert _event_types(db, "conv-act-bad") == ["activity.command.run.failed"]
    assert _event_types(db, "conv-act-good") == ["activity.command.created"]
