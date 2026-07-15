"""PR-6: observability split + orphan-recovery main-only + silent-degradation logging.

Tests for the three source-code changes in ``tui_gateway/services/run_control.py``:

S1 — ``terminal-event-not-persisted`` label split:
    * Worker side (persist forced False by R1 invariant) → DEBUG label
      ``terminal-event-persist-deferred-to-main`` (benign, no error metric).
    * Main side (db method missing) → ERROR label
      ``terminal-event-dropped-no-db`` (genuine data loss).

S8 — orphan-recovery main-only:
    * ``_recover_orphaned_active_runs`` short-circuits in a worker process
      (``is_worker_process() == True``) and never calls the DB scan method.

S9 — canonical projection observability:
    * The retired live-mirror branch is absent, while remaining projection
      failures emit ``logger.error`` with contextual fields.

Tests use monkeypatch / mock only — no source-code mutation.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tui_gateway.services import run_control


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_terminal_frame(*, session_id: str = "sess-1", run_id: str = "run-1") -> dict:
    """Build a minimal terminal event frame that ``record_event`` accepts."""
    return {
        "type": "message.complete",
        "session_id": session_id,
        "conversation_session_id": session_id,
        "run_id": run_id,
        "turn_id": "turn-1",
        "payload": {"status": "completed"},
        "seq": 0,
    }


class _StubDB:
    """A DB stub whose ``__class__.__module__`` is NOT ``unittest.mock``.

    ``_db_method`` skips mocks, so we need a real class to wire up methods.
    By default no ``append_run_event`` is provided, which drives the
    ``elif terminal_event`` branch (no persist path).
    """

    db_path = "stub-db"

    def __init__(self, **methods):
        self.runs = SimpleNamespace()
        for name, fn in methods.items():
            setattr(self, name, fn)
            run_method_name = {
                "append_run_event": "append_event",
                "fail_orphaned_active_runs": "fail_orphaned",
            }.get(name)
            if run_method_name:
                setattr(self.runs, run_method_name, fn)


# ---------------------------------------------------------------------------
# S1: terminal-event label split
# ---------------------------------------------------------------------------

class TestS1TerminalEventLabelSplit:
    """Worker side → DEBUG ``terminal-event-persist-deferred-to-main``;
    main side → ERROR ``terminal-event-dropped-no-db``."""

    def test_worker_side_emits_debug_not_error(self, caplog):
        """In a worker process, a terminal event with no DB persist path
        emits DEBUG with the deferred-to-main label, NOT ERROR."""
        frame = _make_terminal_frame()

        with patch("tui_gateway.process_role.is_worker_process", return_value=True):
            # db has no append_run_event → will_persist is False → elif branch
            db = _StubDB()
            with caplog.at_level(logging.DEBUG, logger="tui_gateway.services.run_control"):
                run_control.record_event(frame, db=db)

        # Must have the DEBUG deferred label
        debug_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.DEBUG
            and "terminal-event-persist-deferred-to-main" in r.message
        ]
        assert len(debug_msgs) == 1, (
            f"expected 1 DEBUG deferred-to-main log, got {debug_msgs}"
        )

        # Must NOT have any ERROR with the dropped-no-db label
        error_dropped = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "terminal-event-dropped-no-db" in r.message
        ]
        assert error_dropped == [], (
            "worker side must not emit ERROR terminal-event-dropped-no-db"
        )

    def test_main_side_emits_error_not_debug(self, caplog):
        """In the main process, a terminal event with no DB persist path
        emits ERROR with the dropped-no-db label."""
        frame = _make_terminal_frame()

        with patch("tui_gateway.process_role.is_worker_process", return_value=False):
            # db=None → _db_method returns None → no append_run_event → elif
            with caplog.at_level(logging.DEBUG, logger="tui_gateway.services.run_control"):
                run_control.record_event(frame, db=None)

        error_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.ERROR
            and "terminal-event-dropped-no-db" in r.message
        ]
        assert len(error_msgs) == 1, (
            f"expected 1 ERROR dropped-no-db log, got {error_msgs}"
        )

        # Must NOT have the DEBUG deferred label
        debug_deferred = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG
            and "terminal-event-persist-deferred-to-main" in r.message
        ]
        assert debug_deferred == [], (
            "main side must not emit DEBUG terminal-event-persist-deferred-to-main"
        )

    def test_main_side_broadcast_of_atomically_persisted_terminal_is_not_data_loss(self, caplog):
        frame = _make_terminal_frame()

        with patch("tui_gateway.process_role.is_worker_process", return_value=False):
            with caplog.at_level(logging.DEBUG, logger="tui_gateway.services.run_control"):
                run_control.record_event(frame, db=_StubDB(), persist=False)

        assert any(
            "terminal-event-broadcast-without-repersist" in record.message
            for record in caplog.records
        )
        assert not any(
            record.levelno == logging.ERROR
            and "terminal-event-dropped-no-db" in record.message
            for record in caplog.records
        )

    def test_worker_debug_carries_context(self, caplog):
        """The DEBUG log must include event_type, session_id, run_id."""
        frame = _make_terminal_frame(session_id="ctx-sess", run_id="ctx-run")

        with patch("tui_gateway.process_role.is_worker_process", return_value=True):
            db = _StubDB()
            with caplog.at_level(logging.DEBUG, logger="tui_gateway.services.run_control"):
                run_control.record_event(frame, db=db)

        debug_msg = next(
            r.message for r in caplog.records
            if "terminal-event-persist-deferred-to-main" in r.message
        )
        assert "ctx-sess" in debug_msg
        assert "ctx-run" in debug_msg
        assert "message.complete" in debug_msg

    def test_main_error_carries_context(self, caplog):
        """The ERROR log must include event_type, session_id, run_id."""
        frame = _make_terminal_frame(session_id="m-sess", run_id="m-run")

        with patch("tui_gateway.process_role.is_worker_process", return_value=False):
            with caplog.at_level(logging.DEBUG, logger="tui_gateway.services.run_control"):
                run_control.record_event(frame, db=None)

        error_msg = next(
            r.message for r in caplog.records
            if "terminal-event-dropped-no-db" in r.message
        )
        assert "m-sess" in error_msg
        assert "m-run" in error_msg
        assert "message.complete" in error_msg


# ---------------------------------------------------------------------------
# S8: orphan-recovery main-only
# ---------------------------------------------------------------------------

class TestS8OrphanRecoveryMainOnly:
    """``_recover_orphaned_active_runs`` must short-circuit in worker processes."""

    def test_worker_process_skips_scan(self):
        """When ``is_worker_process()`` is True, the DB scan method is
        never called and the function returns 0 immediately."""
        scan_called = []

        def _fake_fail_orphaned(**kwargs):
            scan_called.append(kwargs)
            return 0

        db = _StubDB(fail_orphaned_active_runs=_fake_fail_orphaned)

        with patch("tui_gateway.process_role.is_worker_process", return_value=True):
            result = run_control._recover_orphaned_active_runs(db)

        assert result == 0
        assert scan_called == [], (
            "worker process must NOT call fail_orphaned_active_runs"
        )

    def test_main_process_runs_scan(self):
        """When ``is_worker_process()`` is False, the DB scan method IS
        called (the guard must not block main-side recovery)."""
        scan_called = []

        def _fake_fail_orphaned(**kwargs):
            scan_called.append(kwargs)
            return 2  # pretend 2 orphaned runs were recovered

        db = _StubDB(fail_orphaned_active_runs=_fake_fail_orphaned)

        with patch("tui_gateway.process_role.is_worker_process", return_value=False):
            result = run_control._recover_orphaned_active_runs(db)

        assert result == 2
        assert len(scan_called) == 1, (
            "main process must call fail_orphaned_active_runs exactly once"
        )

    def test_worker_returns_zero_even_with_db(self):
        """Even with a valid DB + method, a worker process returns 0
        without touching the DB."""
        def _fake_fail_orphaned(**kwargs):  # pragma: no cover — must not run
            raise AssertionError("scan must not run in worker")

        db = _StubDB(fail_orphaned_active_runs=_fake_fail_orphaned)

        with patch("tui_gateway.process_role.is_worker_process", return_value=True):
            result = run_control._recover_orphaned_active_runs(
                db, current_gateway_instance_id="gw-1"
            )

        assert result == 0


# ---------------------------------------------------------------------------
# S9: silent-degradation logging
# ---------------------------------------------------------------------------

class TestS9SilentDegradationLogging:
    """Canonical team-mission projection failures must emit logger.error."""

    def test_live_conversation_mirror_branch_is_retired(self):
        source = inspect.getsource(run_control.record_event)
        assert "mirror_event_to_conversation" not in source
        assert "team-mission-mirror-failed" not in source

    def test_status_append_failure_emits_error(self, caplog):
        """When ``append_team_mission_conversation_status_event`` raises,
        record_event must emit an ERROR (was bare ``pass`` before)."""
        frame = _make_terminal_frame()

        def _fake_append_run_event(stable, frame, **kw):
            return {"seq": 1}

        def _fake_reduce_team_mission_run_event(*, run_id, event):
            return {"mission_id": "mission-2"}

        def _fake_append_team_mission_event_for_run(*, run_id, event):
            return {"seq": 1, "type": "message.complete"}

        def _fake_get_team_mission_run_binding(run_id):
            return {"mission_id": "mission-2"}

        def _fake_status_appender(**kw):
            raise RuntimeError("status boom")

        db = _StubDB(
            append_run_event=_fake_append_run_event,
            reduce_team_mission_run_event=_fake_reduce_team_mission_run_event,
            append_team_mission_event_for_run=_fake_append_team_mission_event_for_run,
            get_team_mission_run_binding=_fake_get_team_mission_run_binding,
            append_team_mission_conversation_status_event=_fake_status_appender,
        )

        with patch("tui_gateway.process_role.is_worker_process", return_value=False):
            with caplog.at_level(logging.DEBUG, logger="tui_gateway.services.run_control"):
                run_control.record_event(frame, db=db)

        error_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.ERROR
            and "team-mission-conversation-status-append-failed" in r.message
        ]
        assert len(error_msgs) == 1, (
            f"expected 1 ERROR status-append-failed log, got {error_msgs}"
        )
        assert "status boom" in error_msgs[0]

    def test_mission_event_append_failure_emits_error(self, caplog):
        """When ``append_team_mission_event_for_run`` raises, record_event
        must emit an ERROR (was silent ``candidate_event = {}`` before)."""
        frame = _make_terminal_frame()

        def _fake_append_run_event(stable, frame, **kw):
            return {"seq": 1}

        def _fake_reduce_team_mission_run_event(*, run_id, event):
            return {"mission_id": "mission-3"}

        def _fake_append_team_mission_event_for_run(*, run_id, event):
            raise RuntimeError("mission append boom")

        def _fake_get_team_mission_run_binding(run_id):
            return {"mission_id": "mission-3"}

        db = _StubDB(
            append_run_event=_fake_append_run_event,
            reduce_team_mission_run_event=_fake_reduce_team_mission_run_event,
            append_team_mission_event_for_run=_fake_append_team_mission_event_for_run,
            get_team_mission_run_binding=_fake_get_team_mission_run_binding,
        )

        with patch("tui_gateway.process_role.is_worker_process", return_value=False):
            with caplog.at_level(logging.DEBUG, logger="tui_gateway.services.run_control"):
                run_control.record_event(frame, db=db)

        error_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.ERROR
            and "team-mission-event-append-failed" in r.message
        ]
        assert len(error_msgs) == 1, (
            f"expected 1 ERROR event-append-failed log, got {error_msgs}"
        )
        assert "mission append boom" in error_msgs[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
