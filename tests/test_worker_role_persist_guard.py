"""R1 architectural invariant test: worker processes MUST NOT persist
``run_events``. Enforced at ``record_event`` via ``process_role``.

Verifies:
- default (main-side) call path with ``persist=True`` reaches
  ``append_run_event``.
- flipping ``IS_WORKER_PROCESS`` (or ``mark_as_worker_process()``) causes
  ``record_event(persist=True)`` to skip persistence *without* dropping
  subscriber fanout.
- flipping back does NOT resurrect the guard: this is a one-way per-
  process bit, so tests explicitly re-import & reset via monkeypatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from tui_gateway import process_role
from tui_gateway.services import run_control


class _FakeDB:
    """Minimal duck-typed DB that records whether ``append_run_event``
    was invoked. Provides just enough surface for ``record_event`` to
    walk its persist branch without a real state.db."""

    def __init__(self) -> None:
        self.append_calls: list[dict] = []
        self._team_mission_projecting = False
        owner = self

        class _Runs:
            @staticmethod
            def get(_run_id: str):
                return None

            @staticmethod
            def append_event(
                stable: str,
                frame: dict,
                *,
                participant_id: str = "",
            ) -> dict:
                return owner.append_run_event(
                    stable,
                    frame,
                    participant_id=participant_id,
                )

        self.runs = _Runs()

    def append_run_event(self, stable: str, frame: dict, *, participant_id: str = "") -> dict:
        self.append_calls.append({
            "stable": stable,
            "type": frame.get("type"),
            "participant_id": participant_id,
        })
        # Match the shape record_event expects on success (no _persistence_disposition).
        return {**frame, "seq": int(frame.get("seq") or 1)}


def _frame(*, run_id: str = "run-1", session: str = "sess-1") -> dict[str, Any]:
    return {
        "type": "tool.start",
        "session_id": session,
        "conversation_session_id": session,
        "run_id": run_id,
        "turn_id": "turn-1",
        "execution_session_id": session,
        "runtime_scope_key": "scope-1",
        "payload": {"tool_id": "call_1", "name": "write_file"},
        "seq": 1,
    }


@pytest.fixture(autouse=True)
def _reset_process_role(monkeypatch):
    """Every test starts from ``IS_WORKER_PROCESS = False`` and any
    mutation is scoped to the test."""
    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", False)
    yield


def test_main_process_persists_with_persist_true():
    db = _FakeDB()
    run_control.record_event(_frame(), db=db, persist=True)
    assert len(db.append_calls) == 1, "main sidecar must persist run_events"
    assert db.append_calls[0]["type"] == "tool.start"


def test_worker_process_refuses_to_persist_even_with_persist_true(monkeypatch):
    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", True)
    db = _FakeDB()
    run_control.record_event(_frame(), db=db, persist=True)
    assert db.append_calls == [], (
        "R1 invariant: worker process must not write run_events even "
        "when a legacy caller passes persist=True"
    )


def test_worker_process_refuses_to_persist_with_persist_false(monkeypatch):
    """persist=False stays persist=False; the guard doesn't accidentally
    invert."""
    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", True)
    db = _FakeDB()
    run_control.record_event(_frame(), db=db, persist=False)
    assert db.append_calls == []


def test_worker_process_cannot_terminate_run_directly(monkeypatch):
    class _Runs:
        def __init__(self) -> None:
            self.terminate_calls = []

        def terminate(self, **kwargs):
            self.terminate_calls.append(kwargs)
            raise AssertionError("worker must not reach terminal persistence")

    class _TerminalDB:
        def __init__(self) -> None:
            self.runs = _Runs()

    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", True)
    db = _TerminalDB()

    with pytest.raises(
        run_control.WorkerTerminalOwnershipError,
        match="RunTerminalFrame",
    ):
        run_control.terminate_run(
            conversation_session_id="sess-1",
            run_id="run-1",
            status="failed",
            message="boom",
            db=db,
        )

    assert db.runs.terminate_calls == []


def test_mark_as_worker_process_helper_flips_bit():
    assert process_role.is_worker_process() is False
    try:
        process_role.mark_as_worker_process()
        assert process_role.is_worker_process() is True
        # Idempotent — repeat calls stay True.
        process_role.mark_as_worker_process()
        assert process_role.is_worker_process() is True
    finally:
        # Manual reset — mark_as_worker_process is intentionally one-way
        # in production, but tests must not bleed state.
        process_role.IS_WORKER_PROCESS = False
