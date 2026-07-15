from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway import server
from hermes_agent.orchestration import worker_runtime


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return server.handle_request({
        "id": "1",
        "method": method,
        "params": params or {},
    })


def _seed_activities(db: CliSessionStore) -> None:
    db.activities.create(activity_id="conv1-pending", conversation_id="conv-1", kind="chat")
    db.activities.create(activity_id="conv1-running", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.create(activity_id="conv2-running", conversation_id="conv-2", kind="team_dispatch")
    db.activities.update_status("conv1-running", "running", started_at=10.0)
    db.activities.update_status("conv2-running", "running", started_at=20.0)


def test_activity_methods_registered() -> None:
    for method_name in (
        "activity.list",
        "activity.get",
        "activity.cancel",
        "activity.mark_read",
    ):
        assert method_name in server._methods
        assert server._methods[method_name].__module__ == "tui_gateway.methods.activity"


def test_activity_list_returns_activities_for_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    _seed_activities(db)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = _call("activity.list", {"conversation_id": "conv-1"})

    assert "error" not in response
    assert [row["activity_id"] for row in response["result"]] == [
        "conv1-pending",
        "conv1-running",
    ]


def test_activity_list_filters_by_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    _seed_activities(db)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = _call("activity.list", {"conversation_id": "conv-1", "status": "running"})

    assert "error" not in response
    assert [row["activity_id"] for row in response["result"]] == ["conv1-running"]


def test_activity_get_returns_single_activity_or_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="agent_dispatch")
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = _call("activity.get", {"activity_id": "act-1"})

    assert "error" not in response
    assert response["result"]["activity_id"] == "act-1"
    assert response["result"]["conversation_id"] == "conv-1"


def test_activity_cancel_marks_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str, Any]] = []

    class _Router:
        def lookup_activity_run(self, activity_id: str) -> Any:
            assert activity_id == "act-1"
            return SimpleNamespace(
                run_id="run-1",
                scope_key="profile:worker",
                conversation_id="conv-1",
            )

    class _Supervisor:
        async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
            sent.append((scope_key, conversation_id, frame))
            return True

    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.update_status("act-1", "running", started_at=10.0)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: _Router())
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: _Supervisor())

    response = _call("activity.cancel", {"activity_id": "act-1"})

    assert response["result"] == {"ok": True, "worker_signaled": True}
    assert sent[0][0:2] == ("profile:worker", "conv-1")
    assert sent[0][2].__class__.__name__ == "RunCancelFrame"
    assert sent[0][2].run_id == "run-1"
    row = db.activities.get("act-1")
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["completed_at"] is not None


def test_activity_cancel_terminal_activity_does_not_signal_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signaled = False

    class _Router:
        def lookup_activity_run(self, activity_id: str) -> Any:
            nonlocal signaled
            signaled = True
            return None

    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.mark_completed("act-1", result_summary="Done", result_json={})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: _Router())

    response = _call("activity.cancel", {"activity_id": "act-1"})

    assert response["result"] == {"ok": False, "reason": "already_terminal"}
    assert signaled is False
    assert db.activities.get("act-1")["status"] == "completed"


def test_activity_mark_read_sets_read_at_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.mark_completed("act-1", result_summary="Done", result_json={})
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = _call("activity.mark_read", {"activity_id": "act-1"})

    assert response["result"] == {"ok": True}
    row = db.activities.get("act-1")
    assert row is not None
    assert row["read_at"] is not None
    assert row["updated_at"] >= row["read_at"]


def test_activity_get_returns_null_for_missing_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = _call("activity.get", {"activity_id": "missing"})

    assert "error" not in response
    assert response["result"] is None


@pytest.mark.parametrize(
    ("method_name", "params", "missing_field"),
    [
        ("activity.list", {}, "conversation_id"),
        ("activity.get", {}, "activity_id"),
        ("activity.cancel", {}, "activity_id"),
        ("activity.mark_read", {}, "activity_id"),
    ],
)
def test_activity_methods_reject_missing_required_fields(
    method_name: str,
    params: dict[str, Any],
    missing_field: str,
) -> None:
    response = _call(method_name, params)

    assert response["error"]["code"] == -32602
    assert f"{missing_field} required" in response["error"]["message"]
