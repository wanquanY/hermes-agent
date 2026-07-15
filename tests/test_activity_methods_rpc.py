from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway import server


def _call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return server.handle_request({
        "id": "activity-rpc-test",
        "method": method,
        "params": params or {},
    })


@pytest.fixture()
def gateway_db(tmp_path: Path) -> CliSessionStore:
    previous_db = server._db
    previous_db_error = server._db_error
    previous_db_by_home = dict(server._db_by_home)
    previous_db_error_by_home = dict(server._db_error_by_home)

    db = open_cli_session_store(tmp_path / "state.db")
    server._db = db
    server._db_error = None
    server._db_by_home = {}
    server._db_error_by_home = {}
    try:
        yield db
    finally:
        db.close()
        server._db = previous_db
        server._db_error = previous_db_error
        server._db_by_home = previous_db_by_home
        server._db_error_by_home = previous_db_error_by_home


def _base_create_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "kind": "mission",
        "conversation_session_id": "session-activity-rpc",
        "conversation_id": "conversation-activity-rpc",
    }
    params.update(overrides)
    return params


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response
    return response["result"]


def _assert_validation(response: dict[str, Any], message: str) -> None:
    assert response["error"]["code"] == 4006
    assert response["error"]["message"] == message


def _command(db: CliSessionStore, command_id: str) -> dict[str, Any]:
    row = db.activities.get_command(command_id)
    assert row
    return row


def _run_event_count(db: CliSessionStore) -> int:
    with db._lock:
        row = db._conn.execute("SELECT COUNT(*) AS count FROM run_events").fetchone()
    assert row is not None
    return int(row["count"])


def _activity_source_tree() -> ast.AST:
    src = Path("tui_gateway/methods/activity.py").read_text()
    return ast.parse(src)


def _assert_no_forbidden_symbols(forbidden: set[str]) -> None:
    """Verify no Activity Command bus handler references forbidden symbols.

    Activity-command-bus handlers are the functions whose names start with
    ``activity_command_`` (e.g. activity_command_cancel). The legacy
    handler ``activity_cancel`` is preserved on the same module for
    frontend back-compat and may legitimately reference worker_runtime /
    mark_activity_cancelled / RunCancelFrame. Narrow AST traversal to
    command-bus handlers only.
    """
    violations: list[str] = []
    tree = _activity_source_tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("activity_command_"):
            continue
        for descendant in ast.walk(node):
            if isinstance(descendant, ast.Name) and descendant.id in forbidden:
                violations.append(f"{node.name}:{descendant.id}")
            if isinstance(descendant, ast.Attribute) and descendant.attr in forbidden:
                violations.append(f"{node.name}:{descendant.attr}")
    assert not violations, sorted(set(violations))


def test_activity_create_returns_activity_id_and_command_id(
    gateway_db: CliSessionStore,
) -> None:
    result = _assert_ok(_call("activity.create", _base_create_params()))

    assert result["activity_id"].startswith("act-mission-")
    assert result["command_id"].startswith("cmd-")
    assert result["status"] == "accepted"


def test_activity_create_uses_caller_supplied_activity_id(
    gateway_db: CliSessionStore,
) -> None:
    result = _assert_ok(
        _call("activity.create", _base_create_params(activity_id="act-caller"))
    )

    assert result["activity_id"] == "act-caller"


def test_activity_create_persists_into_activity_commands_table(
    gateway_db: CliSessionStore,
) -> None:
    result = _assert_ok(
        _call(
            "activity.create",
            _base_create_params(activity_id="act-create", command_id="cmd-create"),
        )
    )

    row = _command(gateway_db, result["command_id"])
    assert row["activity_id"] == "act-create"
    assert row["kind"] == "create"
    assert row["state"] == "accepted"
    assert row["metadata"] == {"source": "activity.create"}


def test_activity_create_preserves_full_payload(gateway_db: CliSessionStore) -> None:
    params = _base_create_params(
        activity_id="act-create-payload",
        command_id="cmd-create-payload",
        parent_activity_id="act-parent",
        metadata={"priority": "high"},
    )
    _assert_ok(_call("activity.create", params))

    assert _command(gateway_db, "cmd-create-payload")["payload"] == params


def test_activity_start_returns_command_id(gateway_db: CliSessionStore) -> None:
    result = _assert_ok(_call("activity.start", {"activity_id": "act-start"}))

    assert result["command_id"].startswith("cmd-")
    assert result["status"] == "accepted"
    assert "activity_id" not in result


def test_activity_start_persists_command(gateway_db: CliSessionStore) -> None:
    _assert_ok(
        _call(
            "activity.start",
            {"activity_id": "act-start", "command_id": "cmd-start"},
        )
    )

    row = _command(gateway_db, "cmd-start")
    assert row["activity_id"] == "act-start"
    assert row["kind"] == "start"
    assert row["metadata"] == {"source": "activity.start"}


def test_activity_cancel_returns_command_id(gateway_db: CliSessionStore) -> None:
    result = _assert_ok(
        _call("activity.command.cancel", {"activity_id": "act-mission-cancel"})
    )

    assert result["command_id"].startswith("cmd-")
    assert result["status"] == "accepted"


def test_activity_cancel_preserves_reason_in_payload(gateway_db: CliSessionStore) -> None:
    _assert_ok(
        _call(
            "activity.command.cancel",
            {
                "activity_id": "act-mission-cancel",
                "command_id": "cmd-cancel",
                "reason": "user requested stop",
            },
        )
    )

    row = _command(gateway_db, "cmd-cancel")
    assert row["kind"] == "cancel"
    assert row["payload"]["reason"] == "user requested stop"
    assert row["metadata"] == {"source": "activity.command.cancel"}


def test_activity_complete_returns_command_id(gateway_db: CliSessionStore) -> None:
    result = _assert_ok(_call("activity.complete", {"activity_id": "act-complete"}))

    assert result["command_id"].startswith("cmd-")
    assert result["status"] == "accepted"


def test_activity_complete_preserves_result_in_payload(gateway_db: CliSessionStore) -> None:
    result_payload = {"summary": "done", "ok": True}
    _assert_ok(
        _call(
            "activity.complete",
            {
                "activity_id": "act-complete",
                "command_id": "cmd-complete",
                "result": result_payload,
            },
        )
    )

    row = _command(gateway_db, "cmd-complete")
    assert row["kind"] == "complete"
    assert row["payload"]["result"] == result_payload
    assert row["metadata"] == {"source": "activity.complete"}


def test_activity_create_rerun_same_command_id_returns_already_existed(
    gateway_db: CliSessionStore,
) -> None:
    params = _base_create_params(
        activity_id="act-idem-create", command_id="cmd-idem-create"
    )
    first = _assert_ok(_call("activity.create", params))
    second = _assert_ok(_call("activity.create", params))

    assert "already_existed" not in first
    assert second["already_existed"] is True
    assert second["activity_id"] == "act-idem-create"


def test_activity_start_rerun_same_command_id_returns_already_existed(
    gateway_db: CliSessionStore,
) -> None:
    params = {"activity_id": "act-idem-start", "command_id": "cmd-idem-start"}
    first = _assert_ok(_call("activity.start", params))
    second = _assert_ok(_call("activity.start", params))

    assert "already_existed" not in first
    assert second["already_existed"] is True


def test_activity_cancel_rerun_same_command_id_returns_already_existed(
    gateway_db: CliSessionStore,
) -> None:
    params = {
        "activity_id": "act-mission-idem-cancel",
        "command_id": "cmd-idem-cancel",
    }
    first = _assert_ok(_call("activity.command.cancel", params))
    second = _assert_ok(_call("activity.command.cancel", params))

    assert "already_existed" not in first
    assert second["already_existed"] is True


def test_activity_complete_rerun_same_command_id_returns_already_existed(
    gateway_db: CliSessionStore,
) -> None:
    params = {"activity_id": "act-idem-complete", "command_id": "cmd-idem-complete"}
    first = _assert_ok(_call("activity.complete", params))
    second = _assert_ok(_call("activity.complete", params))

    assert "already_existed" not in first
    assert second["already_existed"] is True


def test_activity_create_rejects_missing_kind(gateway_db: CliSessionStore) -> None:
    _assert_validation(
        _call(
            "activity.create",
            {
                "conversation_session_id": "session-activity-rpc",
                "conversation_id": "conversation-activity-rpc",
            },
        ),
        "kind required",
    )


def test_activity_create_rejects_unknown_kind(gateway_db: CliSessionStore) -> None:
    response = _call("activity.create", _base_create_params(kind="unknown"))

    assert response["error"]["code"] == 4006
    assert response["error"]["message"].startswith("kind must be one of ")


def test_activity_create_rejects_missing_conversation_session_id(
    gateway_db: CliSessionStore,
) -> None:
    _assert_validation(
        _call(
            "activity.create",
            {"kind": "mission", "conversation_id": "conversation-activity-rpc"},
        ),
        "conversation_session_id required",
    )


def test_activity_create_rejects_missing_conversation_id(gateway_db: CliSessionStore) -> None:
    _assert_validation(
        _call(
            "activity.create",
            {"kind": "mission", "conversation_session_id": "session-activity-rpc"},
        ),
        "conversation_id required",
    )


def test_activity_start_rejects_missing_activity_id(gateway_db: CliSessionStore) -> None:
    _assert_validation(_call("activity.start", {}), "activity_id required")


def test_activity_start_rejects_empty_activity_id(gateway_db: CliSessionStore) -> None:
    _assert_validation(
        _call("activity.start", {"activity_id": ""}),
        "activity_id required",
    )


def test_activity_cancel_rejects_missing_activity_id(gateway_db: CliSessionStore) -> None:
    _assert_validation(_call("activity.command.cancel", {}), "activity_id required")


def test_activity_command_cancel_rejects_unprefixed_activity_id(
    gateway_db: CliSessionStore,
) -> None:
    response = _call(
        "activity.command.cancel",
        {"activity_id": "conversation-id-without-prefix"},
    )

    assert response["error"]["code"] == 4006
    assert "NOT a conversation_id or mission_id directly" in response["error"]["message"]


def test_legacy_activity_cancel_keeps_unprefixed_activity_id_compatibility(
    gateway_db: CliSessionStore,
) -> None:
    response = _call("activity.cancel", {"activity_id": "conversation-id-without-prefix"})

    assert response["result"] == {"ok": False, "reason": "already_terminal"}


def test_activity_complete_rejects_missing_activity_id(gateway_db: CliSessionStore) -> None:
    _assert_validation(_call("activity.complete", {}), "activity_id required")


def test_activity_create_returns_5008_when_db_missing() -> None:
    previous_get_db = server._get_db
    server._get_db = lambda: None
    try:
        response = _call("activity.create", _base_create_params())
    finally:
        server._get_db = previous_get_db

    assert response["error"] == {"code": 5008, "message": "state.db unavailable"}


def test_activity_create_does_not_emit_run_events(gateway_db: CliSessionStore) -> None:
    before = _run_event_count(gateway_db)
    _assert_ok(
        _call("activity.create", _base_create_params(activity_id="act-no-events"))
    )

    assert _run_event_count(gateway_db) == before
    _assert_no_forbidden_symbols({"record_event", "append_run_event"})


def test_activity_start_does_not_spawn_worker(gateway_db: CliSessionStore) -> None:
    _assert_ok(_call("activity.start", {"activity_id": "act-no-spawn"}))

    _assert_no_forbidden_symbols({
        "primary_dispatch",
        "_proxy_run_submit_via_worker",
        "_submit_run_via_worker_with_response",
        "worker_supervisor",
        "worker_frame_router",
    })


def test_activity_cancel_does_not_kill_workers(gateway_db: CliSessionStore) -> None:
    gateway_db.activities.create(
        activity_id="act-mission-stays-running",
        conversation_id="conversation-activity-rpc",
        kind="agent_dispatch",
    )
    gateway_db.activities.update_status(
        "act-mission-stays-running",
        "running",
        started_at=1.0,
    )

    _assert_ok(
        _call(
            "activity.command.cancel",
            {
                "activity_id": "act-mission-stays-running",
                "command_id": "cmd-no-kill",
            },
        )
    )

    assert gateway_db.activities.get("act-mission-stays-running")["status"] == "running"
    _assert_no_forbidden_symbols({
        "cancel_run",
        "RunCancelFrame",
        "mark_activity_cancelled",
    })
