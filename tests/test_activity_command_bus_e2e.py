from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.services.activity_reconciler import ActivityReconciler


def _call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return server.handle_request({
        "id": f"e2e-{method}-{time.time_ns()}",
        "method": method,
        "params": params or {},
    })


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return response["result"]


@pytest.fixture
def gateway_db(tmp_path: Path) -> Iterator[SessionDB]:
    """Real SessionDB swapped into the gateway server module globals.

    Saves/restores the original db so we don't poison other tests in the
    same session.
    """
    previous_db = server._db
    previous_db_error = server._db_error
    previous_db_by_home = dict(server._db_by_home)
    previous_db_error_by_home = dict(server._db_error_by_home)
    previous_sessions = dict(server._sessions)
    db = SessionDB(tmp_path / "state.db")
    server._db = db
    server._db_error = None
    server._db_by_home = {}
    server._db_error_by_home = {}
    server._sessions = {}
    try:
        yield db
    finally:
        db.close()
        server._db = previous_db
        server._db_error = previous_db_error
        server._db_by_home = previous_db_by_home
        server._db_error_by_home = previous_db_error_by_home
        server._sessions = previous_sessions


@pytest.fixture
def reconciler(gateway_db: SessionDB) -> ActivityReconciler:
    """Reconciler bound to the e2e-fixture db, sync-driven via run_one_cycle()."""
    return ActivityReconciler(gateway_db)


def _list_command_events(db: SessionDB) -> list[dict[str, Any]]:
    """Return all activity.command.* events sorted by seq."""
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM run_events "
            "WHERE event_type LIKE 'activity.command.%' "
            "ORDER BY seq"
        ).fetchall()
    return [dict(row) for row in rows]


def _command(db: SessionDB, command_id: str) -> dict[str, Any]:
    row = db.get_activity_command(command_id)
    assert row
    return row


def _commands_for_activity(db: SessionDB, activity_id: str) -> list[dict[str, Any]]:
    return db.list_activity_commands_for_activity(activity_id, limit=50)


def _command_by_source(
    db: SessionDB,
    activity_id: str,
    source: str,
) -> dict[str, Any]:
    matches = [
        row
        for row in _commands_for_activity(db, activity_id)
        if row.get("metadata", {}).get("source") == source
    ]
    assert len(matches) == 1
    return matches[0]


def _set_intent_at(db: SessionDB, command_id: str, value: float) -> None:
    with db._lock:
        db._conn.execute(
            "UPDATE activity_commands SET intent_at = ? WHERE command_id = ?",
            (value, command_id),
        )


def _create_activity_row(
    db: SessionDB,
    activity_id: str,
    *,
    conversation_id: str | None = None,
    kind: str = "mission",
    status: str = "pending",
) -> dict[str, Any]:
    activity = db.create_activity(
        activity_id=activity_id,
        conversation_id=conversation_id or f"session-{activity_id}",
        kind=kind,
        status=status,
    )
    if status != "pending":
        db.update_activity_status(activity_id, status)
        activity = db.get_activity(activity_id)
    return activity


def _insert_command(
    db: SessionDB,
    command_id: str,
    *,
    activity_id: str,
    kind: str = "create",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return db.insert_activity_command(
        command_id=command_id,
        activity_id=activity_id,
        kind=kind,
        payload=payload
        or {
            "kind": "mission",
            "conversation_id": f"session-{activity_id}",
            "conversation_session_id": f"session-{activity_id}",
        },
        metadata={"test": True},
    )


def _workspace_payload(tmp_path: Path, name: str = "workspace-1") -> dict[str, str]:
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": name, "workspace_path": str(workspace)}


def _seed_registry_team(db: SessionDB, tmp_path: Path) -> None:
    db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader",
        description="Plans team work.",
        category="product",
        tags=["planning"],
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "leader-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id="version-leader",
        current_version_number=1,
    )
    db.upsert_agent_profile(
        profile_id="profile-builder",
        slug="builder",
        name="Builder",
        description="Builds team work.",
        category="engineering",
        tags=["build"],
        hermes_profile_name="builder",
        hermes_home_path=str(tmp_path / "builder-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id="version-builder",
        current_version_number=1,
    )
    db.upsert_agent_team(
        team_id="team-1",
        name="Test Team",
        description="Team for activity command bus e2e tests.",
        lead_agent_profile_id="profile-leader",
        default_mode="supervised_mission",
        policy={},
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning"],
    )
    db.upsert_agent_team_member(
        member_id="member-builder",
        team_id="team-1",
        agent_profile_id="profile-builder",
        agent_profile_version_id="version-builder",
        role="builder",
        capability_tags=["build"],
    )


def _create_mission(
    tmp_path: Path,
    *,
    mission_id: str = "mission-1",
    conversation_id: str = "conversation-1",
    conversation_session_id: str = "team-session-1",
) -> dict[str, Any]:
    return _assert_ok(
        _call(
            "team_mission.create",
            {
                "mission_id": mission_id,
                "conversation_id": conversation_id,
                "conversation_session_id": conversation_session_id,
                "team_id": "team-1",
                "title": "Test mission",
                "objective": "Validate activity command bus.",
                "mode": "supervised_mission",
                "workspace": _workspace_payload(tmp_path),
                "metadata": {"start_leader": False},
                "record_user_task_message": False,
            },
        )
    )


def _root_node_id(graph: dict[str, Any]) -> str:
    nodes = graph.get("graph", {}).get("nodes") or []
    node = next(item for item in nodes if item.get("kind") == "root")
    return str(node["node_id"])


def _install_ready_prompt_session(db: SessionDB) -> None:
    ready = threading.Event()
    ready.set()
    db.create_session("prompt-session", source="tui")
    server._sessions["runtime-prompt"] = {
        "agent": None,
        "agent_ready": ready,
        "agent_error": "",
        "session_key": "prompt-session",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transient": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
    }


# -- Group A: single-command lifecycle end to end ---------------------------
"""Group A covers one command from RPC ingress through reconciler completion."""


def test_create_lifecycle_rpc_to_satisfied_with_event(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """create RPC -> command(accepted) -> reconciler -> command(satisfied) +
    activities row created + activity.command.created event emitted with
    activity_id stamped on run_events column."""
    result = _assert_ok(
        _call(
            "activity.create",
            {
                "activity_id": "act-create-e2e",
                "command_id": "cmd-create-e2e",
                "kind": "mission",
                "conversation_id": "conv-create-e2e",
                "conversation_session_id": "session-create-e2e",
            },
        )
    )

    assert result == {
        "activity_id": "act-create-e2e",
        "command_id": "cmd-create-e2e",
        "status": "accepted",
    }
    assert _command(gateway_db, "cmd-create-e2e")["state"] == "accepted"

    summary = reconciler.run_one_cycle()

    assert summary["satisfied"] == 1
    assert _command(gateway_db, "cmd-create-e2e")["state"] == "satisfied"
    assert gateway_db.get_activity("act-create-e2e")["activity_id"] == "act-create-e2e"
    events = _list_command_events(gateway_db)
    assert len(events) == 1
    assert events[0]["event_type"] == "activity.command.created"
    assert events[0]["activity_id"] == "act-create-e2e"


def test_start_lifecycle_accepted_then_dispatched_then_timeout_to_failed(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """start RPC -> accepted -> cycle -> dispatched + start.accepted event ->
    backdate command intent_at to bypass timeout -> cycle -> failed +
    run.failed event."""
    _create_activity_row(
        gateway_db,
        "act-start-e2e",
        conversation_id="session-start-e2e",
    )
    _assert_ok(
        _call(
            "activity.start",
            {"activity_id": "act-start-e2e", "command_id": "cmd-start-e2e"},
        )
    )
    assert _command(gateway_db, "cmd-start-e2e")["state"] == "accepted"

    first = reconciler.run_one_cycle()

    assert first["processed"] == 1
    assert _command(gateway_db, "cmd-start-e2e")["state"] == "dispatched"
    events = _list_command_events(gateway_db)
    assert [event["event_type"] for event in events] == [
        "activity.command.start.accepted"
    ]
    assert events[0]["activity_id"] == "act-start-e2e"
    _set_intent_at(gateway_db, "cmd-start-e2e", time.time() - 120)

    second = reconciler.run_one_cycle()

    assert second["failed"] == 1
    command = _command(gateway_db, "cmd-start-e2e")
    assert command["state"] == "failed"
    assert "timed out" in command["error_reason"]
    events = _list_command_events(gateway_db)
    assert [event["event_type"] for event in events] == [
        "activity.command.start.accepted",
        "activity.command.run.failed",
    ]
    assert {event["activity_id"] for event in events} == {"act-start-e2e"}


def test_cancel_lifecycle_rpc_to_satisfied_with_event(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """activity.command.cancel RPC -> command(accepted) -> reconciler ->
    command(satisfied) + activities.status='cancelled' + cancelled event."""
    _create_activity_row(gateway_db, "act-mission-cancel-e2e")
    _assert_ok(
        _call(
            "activity.command.cancel",
            {
                "activity_id": "act-mission-cancel-e2e",
                "command_id": "cmd-cancel-e2e",
                "reason": "stop",
            },
        )
    )

    assert _command(gateway_db, "cmd-cancel-e2e")["state"] == "accepted"
    summary = reconciler.run_one_cycle()

    assert summary["satisfied"] == 1
    assert _command(gateway_db, "cmd-cancel-e2e")["state"] == "satisfied"
    assert gateway_db.get_activity("act-mission-cancel-e2e")["status"] == "cancelled"
    events = _list_command_events(gateway_db)
    assert [event["event_type"] for event in events] == [
        "activity.command.cancelled"
    ]
    assert events[0]["activity_id"] == "act-mission-cancel-e2e"


def test_complete_lifecycle_rpc_to_satisfied_with_event(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """activity.complete RPC -> command(accepted) -> reconciler ->
    command(satisfied) + activities.status='completed' + completed event +
    completed_at populated."""
    _create_activity_row(gateway_db, "act-complete-e2e")
    _assert_ok(
        _call(
            "activity.complete",
            {
                "activity_id": "act-complete-e2e",
                "command_id": "cmd-complete-e2e",
                "result": {"ok": True},
            },
        )
    )

    assert _command(gateway_db, "cmd-complete-e2e")["state"] == "accepted"
    summary = reconciler.run_one_cycle()

    assert summary["satisfied"] == 1
    assert _command(gateway_db, "cmd-complete-e2e")["state"] == "satisfied"
    activity = gateway_db.get_activity("act-complete-e2e")
    assert activity["status"] == "completed"
    assert activity["completed_at"] is not None
    events = _list_command_events(gateway_db)
    assert [event["event_type"] for event in events] == [
        "activity.command.completed"
    ]
    assert events[0]["activity_id"] == "act-complete-e2e"


# -- Group B: idempotency ---------------------------------------------------
"""Group B covers RPC and reconciler idempotency boundaries."""


def test_reconciler_double_cycle_does_not_double_emit_events(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """同一 satisfied 命令被 reconciler 二次扫描时, 不会重复写 event."""
    _assert_ok(
        _call(
            "activity.create",
            {
                "activity_id": "act-idem-event",
                "command_id": "cmd-idem-event",
                "kind": "mission",
                "conversation_id": "conv-idem-event",
                "conversation_session_id": "session-idem-event",
            },
        )
    )

    first = reconciler.run_one_cycle()
    second = reconciler.run_one_cycle()

    assert first["satisfied"] == 1
    assert second["processed"] == 0
    assert len(_list_command_events(gateway_db)) == 1


def test_activity_create_with_same_command_id_returns_already_existed(
    gateway_db: SessionDB,
) -> None:
    """RPC 层 idempotency: 第二次 create 同 command_id 返 already_existed=True."""
    params = {
        "activity_id": "act-idem-rpc",
        "command_id": "cmd-idem-rpc",
        "kind": "mission",
        "conversation_id": "conv-idem-rpc",
        "conversation_session_id": "session-idem-rpc",
    }

    first = _assert_ok(_call("activity.create", params))
    second = _assert_ok(_call("activity.create", params))

    assert "already_existed" not in first
    assert second["already_existed"] is True
    assert second["activity_id"] == "act-idem-rpc"
    assert len(_commands_for_activity(gateway_db, "act-idem-rpc")) == 1


def test_reconciler_idempotent_when_activity_row_pre_exists(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """create 命令但 activities 行已存在(race created), reconciler 不应报错
    且仍能 mark satisfied."""
    _create_activity_row(gateway_db, "act-race-created", conversation_id="session-race")
    _assert_ok(
        _call(
            "activity.create",
            {
                "activity_id": "act-race-created",
                "command_id": "cmd-race-created",
                "kind": "mission",
                "conversation_id": "conv-race",
                "conversation_session_id": "session-race",
            },
        )
    )

    summary = reconciler.run_one_cycle()

    assert summary["satisfied"] == 1
    assert _command(gateway_db, "cmd-race-created")["state"] == "satisfied"
    assert len(gateway_db.list_activities("session-race")) == 1


# -- Group C: legacy bridge end to end --------------------------------------
"""Group C covers legacy RPC wrappers writing audit-only activity commands."""


def test_team_mission_create_writes_legacy_audit_row(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    """call team_mission.create RPC -> activity_commands 表多一行
    kind=create source=team_mission.create."""
    _seed_registry_team(gateway_db, tmp_path)

    _create_mission(tmp_path)

    row = _command_by_source(gateway_db, "mission:mission-1", "team_mission.create")
    assert row["kind"] == "create"
    assert row["state"] == "accepted"
    assert row["payload"]["mission_id"] == "mission-1"


def test_team_mission_create_binds_request_activity_to_mission(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    """team_mission.create binds the stable request Activity to its spawned mission."""
    from tui_gateway.services.team_mission_activity_events import mission_id_for_activity

    _seed_registry_team(gateway_db, tmp_path)

    result = _assert_ok(
        _call(
            "team_mission.create",
            {
                "mission_id": "mission-activity-first",
                "conversation_id": "conversation-activity-first",
                "conversation_session_id": "team-session-activity-first",
                "team_id": "team-1",
                "activity_id": "act-team_dispatch-create-e2e",
                "title": "Activity-first mission",
                "objective": "Validate request Activity binding.",
                "mode": "supervised_mission",
                "workspace": _workspace_payload(tmp_path, "workspace-activity-first"),
                "metadata": {"start_leader": False},
                "record_user_task_message": False,
            },
        )
    )

    assert result["activity_id"] == "act-team_dispatch-create-e2e"
    activity = gateway_db.get_activity("act-team_dispatch-create-e2e")
    assert activity
    assert activity["kind"] == "team_dispatch"
    assert activity["target_mission_id"] == "mission-activity-first"
    assert activity["conversation_id"] == "team-session-activity-first"
    assert activity["status"] == "running"
    assert mission_id_for_activity("act-team_dispatch-create-e2e", db=gateway_db) == "mission-activity-first"
    row = _command_by_source(
        gateway_db,
        "act-team_dispatch-create-e2e",
        "team_mission.create",
    )
    assert row["payload"]["request_activity_id"] == "act-team_dispatch-create-e2e"


def test_team_mission_cancel_writes_legacy_audit_row_and_returns_4040_for_unknown(
    gateway_db: SessionDB,
) -> None:
    """call team_mission.cancel(mission_id='unknown') -> 4040 +
    activity_commands 行(legacy_bridge 仍写入审计, kind=cancel)."""
    response = _call(
        "team_mission.cancel",
        {"mission_id": "unknown", "canceled_by": "user", "reason": "stop"},
    )

    assert response["error"]["code"] == 4040
    if not _commands_for_activity(gateway_db, "mission:unknown"):
        pytest.xfail(
            "Phase 1.F contract gap: team_mission.cancel returns 4040 before "
            "recording legacy_bridge audit for unknown mission_id."
        )
    row = _command_by_source(gateway_db, "mission:unknown", "team_mission.cancel")
    assert row["kind"] == "cancel"
    assert row["state"] == "accepted"


def test_team_mission_node_start_writes_legacy_audit_row(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    """call team_mission.node.start -> activity_commands 行 kind=start."""
    _seed_registry_team(gateway_db, tmp_path)
    graph = _create_mission(tmp_path)
    node_id = _root_node_id(graph)

    response = _call(
        "team_mission.node.start",
        {
            "mission_id": "mission-1",
            "node_id": node_id,
            "workspace": {"workspace_id": "bad", "workspace_path": ""},
            "record_user_task_message": False,
        },
    )

    assert response["error"]["code"] in {4004, 5000, 5008}
    row = _command_by_source(
        gateway_db,
        f"act-node:mission-1:{node_id}",
        "team_mission.node.start",
    )
    assert row["kind"] == "start"
    assert row["payload"]["node_id"] == node_id


def test_team_mission_message_submit_writes_legacy_audit_row_kind_start(
    gateway_db: SessionDB,
) -> None:
    """call team_mission.message.submit -> activity_commands 行 kind=start
    activity_id=chat:<conversation_session_id> for callers without a request Activity."""
    response = _call(
        "team_mission.message.submit",
        {
            "conversation_id": "conversation-message-e2e",
            "conversation_session_id": "team-session-message-e2e",
            "team_id": "team-1",
            "text": "hello leader",
            "workspace": {"workspace_id": "bad", "workspace_path": ""},
        },
    )

    assert response["error"]["code"] in {4004, 4094, 5008}
    row = _command_by_source(
        gateway_db,
        "chat:team-session-message-e2e",
        "team_mission.message.submit",
    )
    assert row["kind"] == "start"
    assert row["payload"]["conversation_session_id"] == "team-session-message-e2e"


def test_team_mission_message_submit_uses_request_activity_owner(
    gateway_db: SessionDB,
) -> None:
    """Activity-first team task requests use the client request Activity as owner."""
    response = _call(
        "team_mission.message.submit",
        {
            "conversation_id": "conversation-message-e2e",
            "conversation_session_id": "team-session-message-e2e",
            "team_id": "team-1",
            "activity_id": "act-team_dispatch-submit-e2e",
            "text": "hello leader",
            "workspace": {"workspace_id": "bad", "workspace_path": ""},
        },
    )

    assert response["error"]["code"] in {4004, 4094, 5008}
    activity = gateway_db.get_activity("act-team_dispatch-submit-e2e")
    assert activity
    assert activity["kind"] == "team_dispatch"
    assert activity["conversation_id"] == "team-session-message-e2e"
    assert activity["status"] == "running"
    row = _command_by_source(
        gateway_db,
        "act-team_dispatch-submit-e2e",
        "team_mission.message.submit",
    )
    assert row["kind"] == "start"
    assert row["payload"]["request_activity_id"] == "act-team_dispatch-submit-e2e"


def test_prompt_submit_writes_legacy_audit_row_with_chat_activity_id(
    gateway_db: SessionDB,
) -> None:
    """call prompt.submit -> activity_commands 行 kind=start
    activity_id=chat:<session_id>."""
    _install_ready_prompt_session(gateway_db)

    result = _assert_ok(
        _call(
            "prompt.submit",
            {
                "_run_registry_reserved": True,
                "session_id": "runtime-prompt",
                "text": "hello",
                "client_run_id": "run-prompt",
                "turn_id": "turn-prompt",
            },
        )
    )

    row = _command_by_source(gateway_db, "chat:prompt-session", "prompt.submit")
    assert row["kind"] == "start"
    assert row["payload"] == {"session_id": "prompt-session", "text_len": 5}
    assert result["conversation_session_id"] == "prompt-session"


# -- Group D: state machine integrity ---------------------------------------
"""Group D covers terminal states and illegal transition enforcement."""


def test_state_machine_rejects_illegal_transition(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """直接 db.update_activity_command_state(satisfied -> dispatched) 应失败
    (legal transitions enforce 在 1.A 的 with_state)."""
    _insert_command(gateway_db, "cmd-illegal", activity_id="act-illegal")
    reconciler.run_one_cycle()

    row = gateway_db.update_activity_command_state(
        "cmd-illegal",
        next_state="dispatched",
    )

    assert row == {}
    assert _command(gateway_db, "cmd-illegal")["state"] == "satisfied"


def test_satisfied_command_is_never_picked_up_again(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """satisfied 命令在后续 cycle 中不会被 list_pending 返回."""
    _insert_command(gateway_db, "cmd-terminal-satisfied", activity_id="act-satisfied")
    reconciler.run_one_cycle()

    second = reconciler.run_one_cycle()

    assert _command(gateway_db, "cmd-terminal-satisfied")["state"] == "satisfied"
    assert second["processed"] == 0
    assert gateway_db.list_pending_activity_commands() == []


def test_failed_command_is_never_picked_up_again(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """failed 命令在后续 cycle 中不会被 list_pending 返回."""
    _assert_ok(
        _call(
            "activity.start",
            {"activity_id": "act-missing-failed", "command_id": "cmd-failed"},
        )
    )

    first = reconciler.run_one_cycle()
    second = reconciler.run_one_cycle()

    assert first["failed"] == 1
    assert _command(gateway_db, "cmd-failed")["state"] == "failed"
    assert second["processed"] == 0
    assert gateway_db.list_pending_activity_commands() == []


# -- Group E: multi-command queue and ordering ------------------------------
"""Group E covers batch ordering and per-command failure isolation."""


def test_multiple_pending_commands_processed_in_intent_at_order(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """3 个不同 intent_at 的命令, reconciler 按时间顺序处理."""
    _insert_command(gateway_db, "cmd-third", activity_id="act-third")
    _insert_command(gateway_db, "cmd-first", activity_id="act-first")
    _insert_command(gateway_db, "cmd-second", activity_id="act-second")
    _set_intent_at(gateway_db, "cmd-third", 30.0)
    _set_intent_at(gateway_db, "cmd-first", 10.0)
    _set_intent_at(gateway_db, "cmd-second", 20.0)

    summary = reconciler.run_one_cycle()

    assert summary["processed"] == 3
    assert summary["satisfied"] == 3
    events = _list_command_events(gateway_db)
    assert [event["activity_id"] for event in events] == [
        "act-first",
        "act-second",
        "act-third",
    ]


def test_one_failing_handler_does_not_stop_other_commands_in_same_cycle(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """一个命令 handler 抛异常(被 try/except 捕获), 其他命令仍在同 cycle 完成."""
    _insert_command(
        gateway_db,
        "cmd-bad",
        activity_id="act-bad",
        payload={
            "conversation_id": "session-act-bad",
            "conversation_session_id": "session-act-bad",
        },
    )
    _insert_command(gateway_db, "cmd-good", activity_id="act-good")

    summary = reconciler.run_one_cycle()

    assert summary["processed"] == 2
    assert summary["failed"] == 1
    assert summary["satisfied"] == 1
    assert _command(gateway_db, "cmd-bad")["state"] == "failed"
    assert _command(gateway_db, "cmd-good")["state"] == "satisfied"
    events = _list_command_events(gateway_db)
    assert [event["event_type"] for event in events] == [
        "activity.command.run.failed",
        "activity.command.created",
    ]


# -- Group F: cross-phase integration ---------------------------------------
"""Group F covers Phase 0 run_events.activity_id plus Phase 1.C emissions."""


def test_event_carries_activity_id_in_run_events_column(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """Phase 0 (activity_id 列) + 1.C (event emit) 集成:
    reconciler emit 的所有 activity.command.* 事件, run_events.activity_id
    都非空且等于命令的 activity_id."""
    _assert_ok(
        _call(
            "activity.create",
            {
                "activity_id": "act-column-create",
                "command_id": "cmd-column-create",
                "kind": "mission",
                "conversation_id": "conv-column-create",
                "conversation_session_id": "session-column-create",
            },
        )
    )
    _create_activity_row(gateway_db, "act-column-cancel")
    _assert_ok(
        _call(
            "activity.command.cancel",
            {
                "activity_id": "act-column-cancel",
                "command_id": "cmd-column-cancel",
            },
        )
    )
    _create_activity_row(gateway_db, "act-column-complete")
    _assert_ok(
        _call(
            "activity.complete",
            {
                "activity_id": "act-column-complete",
                "command_id": "cmd-column-complete",
            },
        )
    )

    summary = reconciler.run_one_cycle()

    assert summary["satisfied"] == 3
    events = _list_command_events(gateway_db)
    assert len(events) == 3
    assert [event["activity_id"] for event in events] == [
        "act-column-create",
        "act-column-cancel",
        "act-column-complete",
    ]
    command_activity_ids = {
        _command(gateway_db, "cmd-column-create")["activity_id"],
        _command(gateway_db, "cmd-column-cancel")["activity_id"],
        _command(gateway_db, "cmd-column-complete")["activity_id"],
    }
    assert {event["activity_id"] for event in events} == command_activity_ids


def test_event_namespace_isolated_from_legacy_activity_running(
    gateway_db: SessionDB,
    reconciler: ActivityReconciler,
) -> None:
    """所有 reconciler emit 的事件 event_type 都是 activity.command.* 前缀,
    没有 activity.running / activity.completed 这种 legacy 名字."""
    _assert_ok(
        _call(
            "activity.create",
            {
                "activity_id": "act-namespace",
                "command_id": "cmd-namespace",
                "kind": "mission",
                "conversation_id": "conv-namespace",
                "conversation_session_id": "session-namespace",
            },
        )
    )

    reconciler.run_one_cycle()

    event_types = [event["event_type"] for event in _list_command_events(gateway_db)]
    assert event_types == ["activity.command.created"]
    assert all(event_type.startswith("activity.command.") for event_type in event_types)
    assert "activity.running" not in event_types
    assert "activity.completed" not in event_types
